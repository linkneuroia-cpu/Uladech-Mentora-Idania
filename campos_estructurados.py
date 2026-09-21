"""
campos_estructurados.py
=========================
Índice de campos estructurados (campo -> valor) extraído de las tablas ya
vectorizadas de una colección, persistido en Postgres
(`coleccion_campos_estructurados`, ver db_migration_campos_estructurados.sql).

Reemplaza al router de metadata basado en listas de palabras clave
manuales que existía antes en strategies/rerank.py (_METADATA_FIELD_PATTERNS,
ya retirado) por un mecanismo genérico: los campos que existen se
descubren de los documentos reales de cada colección, no de una lista que
el desarrollador mantiene a mano.

No toca Qdrant en ningún momento — lee los chunks tabla/table_row ya
vectorizados (scroll de solo lectura) y escribe solo en esta tabla nueva
de Postgres. `rebuild_index()` borra e inserta de nuevo (DELETE + INSERT)
para una colección puntual — reconstrucción segura, no incremental, no
requiere tocar el resto del sistema.
"""
import math
import re
import unicodedata
from typing import Any, Dict, List, Optional

import psycopg2
import psycopg2.extras

import db
import definitions as d
from qdrant_admin import get_qdrant_admin
from settings import settings
import course_map as _course_map

TABLE_NAME = "coleccion_campos_estructurados"

# Cache en memoria del proceso: (collection_name, dense_strategy_name) ->
# {field_normalized: embedding}. Se llena una sola vez por colección (primer
# fallback semántico que ocurre) — evita re-embeber los ~150-200 nombres de
# campo de la colección en cada pregunta. rebuild_index() la invalida para
# la colección que reconstruye.
_field_embedding_cache: Dict[Any, Dict[str, List[float]]] = {}


# ═══════════════════════════════════════════════════════════════════
# CONEXIÓN — la Postgres real del entorno donde corre el proceso
# (settings.PG_HOST/etc., que ya resuelven DESARROLLO vs PRODUCCIÓN según
# settings.ENV — ver settings.py). Las consultas de solo lectura usan el
# pool ya compartido de db.py (mismo patrón que el resto del gestor); acá
# solo queda una conexión propia para rebuild_index(), que necesita un
# DELETE+INSERT atómico en una sola transacción explícita (el pool de
# db.py abre una transacción nueva por llamada, lo que rompería esa
# atomicidad si el DELETE y el INSERT fueran dos llamadas separadas).
# ═══════════════════════════════════════════════════════════════════

def _connect():
    return psycopg2.connect(
        host=settings.PG_HOST, port=settings.PG_PORT, user=settings.PG_USER,
        password=settings.PG_PASSWORD, dbname=settings.PG_DATABASE, connect_timeout=8,
    )


# ═══════════════════════════════════════════════════════════════════
# NORMALIZACIÓN
# ═══════════════════════════════════════════════════════════════════

def _strip_accents_lower(text: str) -> str:
    normalized = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode()
    return normalized.lower()


def normalize_field(raw: str) -> str:
    """'14. Docente' -> 'docente'. '4. Modalidad del programa' -> 'modalidad del programa'.
    Quita numeración de lista ("14.", "1.1") al inicio — no le da significado
    especial a ningún nombre de campo, es limpieza genérica de formato."""
    text = re.sub(r"^\d+(\.\d+)*\.?\s*", "", raw or "").strip()
    text = _strip_accents_lower(text).rstrip(":").strip()
    text = re.sub(r"\s+", " ", text)
    return text


def normalize_value(raw: str) -> str:
    text = _strip_accents_lower(raw or "").strip()
    text = re.sub(r"\s+", " ", text)
    return text


_QUERY_STOPWORDS = {
    "que", "cual", "cuales", "quien", "quienes", "es", "el", "la", "los", "las",
    "de", "del", "en", "un", "una", "para", "por", "con", "al", "y", "o",
    "tiene", "tienen", "este", "esta", "curso", "sobre", "hay", "como",
    "cuanto", "cuantos", "cuanta", "cuantas", "se", "su", "sus",
    "dice", "segun", "diga", "indica", "menciona", "dame", "dime",
}


def _singularize(word: str) -> str:
    if word.endswith(("ces",)) and len(word) > 5:
        return word[:-3] + "z"
    if word.endswith("es") and len(word) > 4:
        return word[:-2]
    if word.endswith("s") and len(word) > 3:
        return word[:-1]
    return word


def _content_words(text: str) -> set:
    norm = _strip_accents_lower(text or "")
    words = re.findall(r"[a-z0-9]+", norm)
    return {_singularize(w) for w in words if w not in _QUERY_STOPWORDS and len(w) > 2}


# ═══════════════════════════════════════════════════════════════════
# EXTRACCIÓN — desde chunks YA vectorizados en Qdrant (solo lectura,
# sin re-embeber nada).
# ═══════════════════════════════════════════════════════════════════

def _rows_from_table_row_chunk(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Un chunk chunk_type='table_row' ya viene formateado como
    'Campo: Valor' o 'Campo1: Valor1 | Campo2: Valor2' (ver
    chunk_structured_blocks) — solo hay que deshacer ese formato. Por
    construcción, estos chunks SOLO existen para tablas únicas (nunca una
    plantilla repetida), así que is_repeated_template siempre es False acá."""
    text = payload.get("text") or ""
    out = []
    for part in text.split(" | "):
        if ": " in part:
            field_raw, value_raw = part.split(": ", 1)
            out.append({"field_raw": field_raw.strip(), "value_raw": value_raw.strip()})
    return out


def retag_repeated_templates(collection_name: str) -> Dict[str, Any]:
    """Recorre TODOS los chunks chunk_type='table' ya guardados en Qdrant
    para esta colección, recalcula la firma de cada uno (table_signature)
    y marca is_repeated_template=True/False vía set_payload — no reembebe
    nada, no toca ningún vector, solo metadata.

    Existe SEPARADO de la vectorización en sí (a diferencia de casi todo
    lo demás en este módulo) porque la decisión de "esta tabla es una
    plantilla repetida" depende de ver TODOS los documentos del curso a
    la vez (ej. 13 sesiones semanales con la misma tabla 'Datos
    Generales' vacía) — información que no existe todavía cuando se
    vectoriza un archivo aislado. Por eso HybridVectorizationService
    (definitions.py) siempre sube con is_repeated_template=False y deja
    que esta función corrija con visión completa DESPUÉS. Es idempotente
    y barato (solo metadata) — se puede re-ejecutar en cualquier momento,
    conforme se van agregando/actualizando documentos del curso, sin
    volver a calcular nada de lo ya hecho desde cero.

    Los chunks chunk_type='table_row' no se tocan: por construcción solo
    existen para tablas que YA se determinaron únicas al momento de
    generarlas (ver chunk_structured_blocks) — si esta función encuentra
    que una tabla en realidad es repetida, sus table_row ya generados
    quedan igual (redundancia acotada, no causa respuestas incorrectas:
    resolve_field() sigue exigiendo un único valor entre fuentes de
    autoridad antes de confiar, ver docstring de resolve_field)."""
    import definitions as d

    admin = get_qdrant_admin()
    client = admin._client

    all_table_points = []
    offset = None
    while True:
        points, offset = client.scroll(
            collection_name=collection_name, limit=500, offset=offset,
            scroll_filter={"must": [{"key": "chunk_type", "match": {"value": "table"}}]},
            with_payload=["filename", "text"], with_vectors=False,
        )
        all_table_points.extend(points)
        if not offset:
            break
    if not all_table_points:
        return {"table_chunks": 0, "marked_repeated": 0, "marked_unique": 0}

    sig_docs: Dict[Any, set] = {}
    point_sig: Dict[Any, Any] = {}
    for pt in all_table_points:
        sig = d.table_signature(pt.payload.get("text") or "")
        point_sig[pt.id] = sig
        if sig is not None:
            # Identidad de documento SIN extensión: en Moodle es común que
            # el mismo archivo fuente (ej. el sílabo) esté subido dos veces,
            # una en .docx y otra en .pdf — mismo contenido real, dos
            # nombres de archivo distintos. Sin normalizar esto, ese ÚNICO
            # documento autoritativo (ej. el sílabo real con el docente
            # verdadero) se contaba como "2 documentos que comparten la
            # misma estructura de tabla" y se marcaba incorrectamente como
            # plantilla repetida de baja autoridad — encontrado con datos
            # reales en producción (Curso_1541: SILABO...docx +
            # SILABO...pdf, mismo docente, degradaba la respuesta a
            # insuficiente). Genérico: no depende del nombre de ningún
            # documento puntual, solo colapsa duplicados de formato del
            # MISMO nombre base.
            doc_identity = re.sub(r"\.[a-z0-9]{2,5}$", "", (pt.payload.get("filename") or "").strip().lower())
            sig_docs.setdefault(sig, set()).add(doc_identity)
    repeated = {sig for sig, docs in sig_docs.items() if len(docs) >= 2}

    updates_true, updates_false = [], []
    for pt in all_table_points:
        sig = point_sig[pt.id]
        (updates_true if (sig is not None and sig in repeated) else updates_false).append(pt.id)

    if updates_true:
        client.set_payload(collection_name=collection_name, payload={"is_repeated_template": True}, points=updates_true)
    if updates_false:
        client.set_payload(collection_name=collection_name, payload={"is_repeated_template": False}, points=updates_false)

    return {
        "table_chunks": len(all_table_points),
        "marked_repeated": len(updates_true),
        "marked_unique": len(updates_false),
    }


def sync_after_vectorization(collection_name: str) -> Dict[str, Any]:
    """Punto de entrada único a llamar al terminar de (re)vectorizar una
    colección hybrid (ver core.py AutoCore/SemiAutoCore/UpdateCore,
    confirm_and_vectorize): 1) retag_repeated_templates() (retroactivo,
    requiere ver la colección completa) 2) rebuild_index() (reconstruye
    el índice de Postgres ya con la clasificación corregida). El orden
    importa: retag primero, si no rebuild_index indexaría con
    is_repeated_template todavía sin corregir."""
    retag_summary = retag_repeated_templates(collection_name)
    index_summary = rebuild_index(collection_name)
    return {"retag": retag_summary, "index": index_summary}


def sync_general_section_fields(collection_name: str, course_id: int) -> Dict[str, Any]:
    """Extrae los campos ya rellenados de la sección 'General' del aula de
    Moodle (course_map.parse_general_section_fields — Docente/Correo/
    WhatsApp/Horario/Sede, lo que el curso tenga) y los persiste en el
    índice estructurado con source_type='seccion_general', para que
    queden disponibles por el mismo mecanismo genérico de consulta que ya
    usa el resto del sistema (detect_field/resolve_field), no solo por el
    query_router. Reemplaza (DELETE + INSERT) solo sus propias filas —
    nunca toca las que vienen de Qdrant (ver el filtro correspondiente en
    rebuild_index). Se llama automáticamente cuando se invalida el Course
    Map por un cambio de archivos (ledger.sync_curso_archivos) y también
    se puede disparar a mano desde el gestor."""
    fields = _course_map.parse_general_section_fields(course_id)

    conn = _connect()
    conn.autocommit = False
    try:
        cur = conn.cursor()
        cur.execute(
            f"DELETE FROM {TABLE_NAME} WHERE qdrant_collection_name = %s AND source_type = 'seccion_general'",
            (collection_name,),
        )
        if fields:
            row_id = f"{collection_name}::seccion_general::moodle"
            psycopg2.extras.execute_values(
                cur,
                f"""INSERT INTO {TABLE_NAME}
                    (qdrant_collection_name, document_filename, document_type, chunk_type,
                     field_raw, field_normalized, value_raw, value_normalized,
                     section_heading, page, table_signature, is_repeated_template, source_type, row_id)
                    VALUES %s""",
                [(
                    collection_name, "Sección General del aula (Moodle)", "moodle", "table_row",
                    label, normalize_field(label), value, normalize_value(value),
                    "Sección General", None, None, False, "seccion_general", row_id,
                ) for label, value in fields.items()],
            )
        conn.commit()
    finally:
        conn.close()

    for cache_key in [k for k in _field_embedding_cache if k[0] == collection_name]:
        _field_embedding_cache.pop(cache_key, None)

    return {"collection": collection_name, "fields_synced": list(fields.keys())}


def rebuild_index(collection_name: str) -> Dict[str, Any]:
    """Reconstruye el índice de una colección: borra sus filas actuales y
    vuelve a extraer desde los chunks tabla/table_row que YA están en
    Qdrant. No descarga nada de Moodle, no embebe nada — es pura lectura
    de Qdrant + escritura en Postgres (dev)."""
    admin = get_qdrant_admin()
    client = admin._client

    all_points = []
    for chunk_type in ("table", "table_row"):
        offset = None
        while True:
            points, offset = client.scroll(
                collection_name=collection_name, limit=500, offset=offset,
                scroll_filter={"must": [{"key": "chunk_type", "match": {"value": chunk_type}}]},
                with_payload=True, with_vectors=False,
            )
            all_points.extend((chunk_type, p) for p in points)
            if not offset:
                break

    records = []
    for point_index, (chunk_type, p) in enumerate(all_points):
        payload = p.payload
        # rows_grouped: lista de FILAS, cada una una lista de {field_raw,value_raw}
        # — necesario para asignar el mismo row_id a los campos de una misma
        # fila (ej. "Componente"+"Ponderación"+... de la fila "Examen sumativo").
        if chunk_type == "table":
            rows_grouped = d._parse_markdown_table_rows(payload.get("text") or "")
            rows_grouped = [
                [{"field_raw": f, "value_raw": v} for f, v in row.items() if f]
                for row in rows_grouped
            ]
        else:
            rows_grouped = [_rows_from_table_row_chunk(payload)]

        is_template = bool(payload.get("is_repeated_template")) if chunk_type == "table" else False
        sig = d.table_signature(payload.get("text") or "") if chunk_type == "table" else None
        for row_index, row in enumerate(rows_grouped):
            row_id = f"{collection_name}::{payload.get('filename')}::pt{point_index}::r{row_index}"
            for entry in row:
                field_norm = normalize_field(entry["field_raw"])
                if not field_norm:
                    continue
                records.append({
                    "qdrant_collection_name": collection_name,
                    "document_filename": payload.get("filename"),
                    "document_type": payload.get("file_type"),
                    "chunk_type": chunk_type,
                    "field_raw": entry["field_raw"],
                    "field_normalized": field_norm,
                    "value_raw": entry["value_raw"],
                    "value_normalized": normalize_value(entry["value_raw"]),
                    "section_heading": payload.get("contextual_header") or None,
                    "page": payload.get("page"),
                    "table_signature": repr(sig) if sig else None,
                    "is_repeated_template": is_template,
                    "source_type": payload.get("source_type"),
                    "row_id": row_id,
                })

    conn = _connect()
    conn.autocommit = False
    try:
        cur = conn.cursor()
        # Excluye source_type='seccion_general' — esas filas NO vienen de
        # Qdrant (ver sync_general_section_fields), así que reconstruir el
        # índice desde los chunks de Qdrant no debe borrarlas; se
        # mantienen sincronizadas por su propio flujo (ledger de Moodle).
        cur.execute(
            f"DELETE FROM {TABLE_NAME} WHERE qdrant_collection_name = %s "
            f"AND (source_type IS NULL OR source_type != 'seccion_general')",
            (collection_name,),
        )
        if records:
            psycopg2.extras.execute_values(
                cur,
                f"""INSERT INTO {TABLE_NAME}
                    (qdrant_collection_name, document_filename, document_type, chunk_type,
                     field_raw, field_normalized, value_raw, value_normalized,
                     section_heading, page, table_signature, is_repeated_template, source_type, row_id)
                    VALUES %s""",
                [(
                    r["qdrant_collection_name"], r["document_filename"], r["document_type"], r["chunk_type"],
                    r["field_raw"], r["field_normalized"], r["value_raw"], r["value_normalized"],
                    r["section_heading"], r["page"], r["table_signature"], r["is_repeated_template"],
                    r["source_type"], r["row_id"],
                ) for r in records],
            )
        conn.commit()
    finally:
        conn.close()

    for cache_key in [k for k in _field_embedding_cache if k[0] == collection_name]:
        _field_embedding_cache.pop(cache_key, None)

    return {
        "collection": collection_name,
        "table_chunks_scanned": sum(1 for ct, _ in all_points if ct == "table"),
        "table_row_chunks_scanned": sum(1 for ct, _ in all_points if ct == "table_row"),
        "field_records": len(records),
        "distinct_fields": len({r["field_normalized"] for r in records}),
    }


# ═══════════════════════════════════════════════════════════════════
# CONSULTA — descubrimiento genérico de campo + resolución por jerarquía
# de evidencia (NO "el valor minoritario gana").
# ═══════════════════════════════════════════════════════════════════

def list_known_fields(collection_name: str) -> List[Dict[str, Any]]:
    rows = db.fetch_all(
        f"""SELECT field_normalized, COUNT(*) AS n, COUNT(DISTINCT document_filename) AS docs
            FROM {TABLE_NAME} WHERE qdrant_collection_name = %s
            GROUP BY field_normalized ORDER BY n DESC""",
        (collection_name,),
    )
    return [{"field_normalized": r["field_normalized"], "occurrences": r["n"], "documents": r["docs"]} for r in rows]


def _cosine(a: List[float], b: List[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


async def _semantic_detect_field(
    collection_name: str,
    query_words: set,
    known_fields: List[Dict[str, Any]],
    field_word_map: Dict[str, set],
    common_words: set,
    dense_strategy_name: Optional[str],
    min_score: float = 0.55,
) -> Optional[str]:
    """Respaldo genérico para sinónimos que el matching léxico no puede ver
    (p.ej. 'profesor'/'maestro' vs. el campo real 'docente') — usa el MISMO
    modelo de embeddings denso ya configurado para esta colección/RD (nunca
    uno hardcodeado), sin ningún diccionario de sinónimos curado a mano: la
    similitud se apoya en el espacio semántico que el modelo ya aprendió.

    Mismo filtro de calidad que el camino léxico: un nombre de campo de 2+
    palabras donde menos de la mitad sobrevive tras sacar stopwords/palabras
    de fondo se descarta (probable fragmento mal extraído). CRÍTICO: se
    embebe el campo y la pregunta ya con las palabras "de fondo" (tema del
    curso, ej. 'odontología'/'filosofía') QUITADAS —no el texto crudo—
    porque un embedding SÍ capta similitud temática aunque la palabra en
    común esté excluida del Jaccard léxico: probado con datos reales, "qué
    relación tiene la odontología moderna con la ética profesional"
    (pregunta conceptual, sin campo real involucrado) matcheaba por texto
    crudo contra un campo llamado 'introducción a la odontología' con score
    más alto (0.62-0.65) que los sinónimos reales de docente (0.56-0.62) —
    mismo riesgo del caso A6 documentado en detect_field(), reaparecido acá
    porque el embedding no respeta el filtro de palabras de fondo a menos
    que se lo demos ya limpio. Al embeber solo las palabras que SOBREVIVEN
    el filtro (mismo common_words, calculado de los datos, no una lista
    fija) ese falso positivo desaparece y los sinónimos reales de campos
    cortos (ej. 'docente') se mantienen intactos. `min_score`: similitud
    coseno mínima — calibrado empíricamente con datos reales de
    Curso_1472/Curso_1541 (ver informe de pruebas), no un número
    arbitrario. Además descarta candidatos cuyo field_normalized tenga más
    de MAX_FIELD_WORDS palabras: un nombre de campo/columna real es corto
    (en ambos cursos probados, p90 de longitud real es 6-9 palabras) — lo
    que supera eso por mucho (visto con datos reales: fragmentos de hasta
    724 palabras) es texto de tabla mal parseado tratado como si fuera un
    'campo', y un embedding SÍ le da similitud alta a preguntas
    conceptuales largas sobre el mismo tema del curso porque es, en la
    práctica, un mini-documento sobre ese tema — el mismo riesgo A6 de
    nuevo, esta vez por longitud en vez de por palabra de fondo. El filtro
    léxico ya se autolimita para estos casos (Jaccard con una unión enorme
    casi nunca supera min_score), pero el semántico no, así que hace falta
    acá explícitamente."""
    from strategies import registry as strategy_registry

    MAX_FIELD_WORDS = 8

    eligible_fields: List[str] = []
    field_texts: Dict[str, str] = {}
    for entry in known_fields:
        field_normalized = entry["field_normalized"]
        raw_word_count = len(field_normalized.split())
        if raw_word_count > MAX_FIELD_WORDS:
            continue
        all_field_words = field_word_map[field_normalized]
        if raw_word_count >= 2 and len(all_field_words) < raw_word_count / 2:
            continue
        field_words = all_field_words - common_words
        if not field_words:
            continue
        eligible_fields.append(field_normalized)
        field_texts[field_normalized] = " ".join(sorted(field_words))
    if not eligible_fields:
        return None

    dense = strategy_registry.get_dense_strategy(dense_strategy_name)
    cache_key = (collection_name, dense_strategy_name or "__default__")
    cached = _field_embedding_cache.get(cache_key)
    if cached is None or set(eligible_fields) - set(cached):
        embeddings = await dense.embed([field_texts[f] for f in eligible_fields], is_query=False)
        cached = dict(zip(eligible_fields, embeddings))
        _field_embedding_cache[cache_key] = cached

    query_text = " ".join(sorted(query_words))
    query_embedding = (await dense.embed([query_text], is_query=True))[0]

    # Top-2, no solo el mejor: una sinonimia real de un campo único (ej.
    # 'profesor' -> 'docente') le gana con margen claro a cualquier otro
    # campo (margen real medido: 0.17-0.31), porque solo hay UN campo que
    # significa eso en la colección. Una pregunta conceptual que solo
    # comparte TEMA con varios campos a la vez (ej. 'odontología moderna'
    # vs. 'ética y sostenibilidad'/'responsabilidad social universitaria'/
    # 'orientación', todos sobre temas parecidos) nunca gana con margen así
    # — el score se reparte entre varios candidatos casi empatados (margen
    # real medido: 0.00-0.14). Exigir margen mínimo, no solo score mínimo,
    # es lo que de verdad separa sinonimia real de coincidencia temática —
    # probado con datos reales de ambos cursos (ver informe de pruebas),
    # umbral con margen de seguridad entre el peor caso correcto (0.1665) y
    # el peor caso adversarial (0.1435).
    def _best_match(embedding: List[float]) -> tuple:
        best_field, best_score, second_score = None, 0.0, 0.0
        for field_normalized in eligible_fields:
            score = _cosine(embedding, cached[field_normalized])
            if score > best_score:
                best_field, second_score, best_score = field_normalized, best_score, score
            elif score > second_score:
                second_score = score
        return best_field, best_score, second_score

    best_field, best_score, second_score = _best_match(query_embedding)
    if best_score >= min_score and best_score - second_score >= 0.15:
        return best_field

    # Respaldo: el bloque completo de palabras no encontró nada confidente
    # — puede ser porque una palabra "de atributo" genérica (ej. 'correo',
    # 'teléfono', 'contacto') sumada al sinónimo real diluye el embedding
    # conjunto lo suficiente para no alcanzar el margen. Probar cada
    # palabra de la pregunta POR SEPARADO contra los mismos campos ya
    # cacheados (sin re-embeber los campos, sea cual sea la palabra) —
    # si el sinónimo real ('profesor') sigue estando solo entre las
    # palabras de la pregunta, encuentra su campo igual que ya lo hace hoy
    # sin la palabra extra. Encontrado con datos reales en producción:
    # 'correo del profesor del curso' fallaba completo pero 'profesor'
    # solo (dentro de esa misma pregunta) sí encuentra 'docente' con
    # margen amplio — no es un caso curado, es el mismo mecanismo ya
    # validado, aplicado palabra por palabra en vez de al bloque entero.
    if len(query_words) <= 1:
        return None
    word_list = sorted(query_words)
    word_embeddings = await dense.embed(word_list, is_query=True)
    best_overall_field, best_overall_score = None, 0.0
    for word_embedding in word_embeddings:
        field, score, second = _best_match(word_embedding)
        if score >= min_score and score - second >= 0.15 and score > best_overall_score:
            best_overall_field, best_overall_score = field, score
    return best_overall_field


async def detect_field(
    collection_name: str,
    query: str,
    min_score: float = 0.34,
    dense_strategy_name: Optional[str] = None,
) -> Optional[str]:
    """Compara las palabras con contenido de la pregunta contra los campos
    REALES de esta colección (no una lista fija) — devuelve el
    field_normalized que mejor matchea, o None. `min_score`: fracción de
    las palabras del NOMBRE del campo que deben aparecer en la pregunta.

    Dos filtros de calidad, ambos calculados de los datos de la colección
    (nada hardcodeado):
    1. Si la mayoría de las palabras del campo son stopwords (ej. "de la
       odontología" -> tras filtrar solo queda "odontologia", 2 de 3
       palabras eran relleno), es más probable que sea un fragmento mal
       extraído de una tabla (celda partida, etc.) que un nombre de campo
       real.
    2. Palabras "de fondo": el tema del curso ("odontología", "filosofía"
       en otro curso...) aparece salpicado en decenas de nombres de campo
       distintos de la MISMA colección — no sirven para distinguir un
       campo de otro. Se calcula la frecuencia de cada palabra entre los
       nombres de campo de esta colección y se excluyen las que aparecen
       en muchos campos distintos (umbral relativo, no una lista fija).
       Sin esto, cualquier pregunta que mencione el tema del curso
       matchea con score perfecto un campo cualquiera que también lo
       mencione, aunque no tenga nada que ver — visto con datos reales
       (caso A6, ver informe)."""
    known_fields = list_known_fields(collection_name)
    if not known_fields:
        return None

    field_word_map: Dict[str, set] = {}
    word_doc_freq: Dict[str, int] = {}
    for entry in known_fields:
        fw = _content_words(entry["field_normalized"])
        field_word_map[entry["field_normalized"]] = fw
        for w in fw:
            word_doc_freq[w] = word_doc_freq.get(w, 0) + 1
    common_words = {
        w for w, c in word_doc_freq.items()
        if c > max(4, len(known_fields) * 0.03)
    }

    query_words = _content_words(query) - common_words
    if not query_words:
        return None

    best_field, best_score = None, 0.0
    for entry in known_fields:
        field_normalized = entry["field_normalized"]
        raw_word_count = len(field_normalized.split())
        all_field_words = field_word_map[field_normalized]
        field_words = all_field_words - common_words
        if not field_words:
            continue
        if raw_word_count >= 2 and len(all_field_words) < raw_word_count / 2:
            continue
        overlap = query_words & field_words
        if not overlap:
            continue
        # Jaccard (intersección / unión) en vez de solo "cobertura del
        # campo" — exige que el campo explique una porción real de la
        # PREGUNTA, no solo que comparta una palabra suelta. Con cobertura
        # pura, "programa de estudio" (2 palabras) empataba con preguntas
        # largas que solo compartían "programa" de pasada (caso A6, ver
        # informe) — Jaccard penaliza eso porque el resto de la pregunta
        # queda sin explicar.
        union = query_words | field_words
        score = len(overlap) / len(union)
        # Prior por cobertura documental: un campo que aparece en muchos
        # documentos distintos de la colección es más probable que sea un
        # campo real y estable del curso (p.ej. un dato del sílabo repetido
        # en cada sesión) que uno que aparece una sola vez en un único
        # documento (p.ej. una columna "Modalidad" propia de UNA actividad
        # puntual, sin relación con la modalidad del curso). Sin esto, un
        # campo raro de 1 sola palabra que matchea exacto (Jaccard=1.0)
        # siempre le gana a un campo más específico y ampliamente atestiguado
        # aunque sea semánticamente el correcto — encontrado con datos reales
        # al probar generalidad entre cursos (Curso_1541: "modalidad" de una
        # actividad puntual le ganaba a "modalidad del programa", ver informe).
        # Genérico: no depende de qué campo es, solo de cuántos documentos
        # distintos de ESTA colección lo respaldan.
        coverage_weight = 0.3 + 0.7 * min(entry["documents"], 5) / 5
        adjusted_score = score * coverage_weight
        if adjusted_score > best_score:
            best_field, best_score = field_normalized, adjusted_score
    if best_score >= min_score:
        return best_field

    # Camino léxico no encontró nada (ej. "profesor"/"maestro" no comparten
    # ninguna palabra con el campo real "docente") — respaldo semántico
    # genérico por embeddings, ver _semantic_detect_field().
    try:
        return await _semantic_detect_field(
            collection_name, query_words, known_fields, field_word_map, common_words,
            dense_strategy_name,
        )
    except Exception as e:
        print(f"⚠️ fallback semántico de detect_field falló (se ignora): {e}")
        return None


def resolve_field(collection_name: str, field_normalized: str) -> Dict[str, Any]:
    """Jerarquía de evidencia (NO 'el valor minoritario es el real'):
    1. filtrar valores vacíos.
    2. separar evidencia de mayor autoridad (tabla única, is_repeated_template=False)
       de la de menor autoridad (plantilla repetida).
    3. si hay evidencia de mayor autoridad:
         - un solo valor distinto entre esas fuentes -> CONFIDENTE.
         - dos o más valores distintos y genuinamente diferentes -> AMBIGUO
           (se preserva la contradicción, no se elige por mayoría).
    4. si NO hay evidencia de mayor autoridad pero sí de plantillas ->
       INSUFICIENTE (confianza baja, cae a RAG normal).
    5. si no hay ninguna evidencia -> NO_ENCONTRADO (cae a RAG normal)."""
    rows = db.fetch_all(
        f"""SELECT document_filename, field_raw, value_raw, value_normalized, section_heading,
                   page, is_repeated_template, source_type
            FROM {TABLE_NAME}
            WHERE qdrant_collection_name = %s AND field_normalized = %s""",
        (collection_name, field_normalized),
    )

    rows = [r for r in rows if r["value_normalized"]]
    if not rows:
        return {"status": "not_found", "field": field_normalized}

    unique_rows = [r for r in rows if not r["is_repeated_template"]]
    template_rows = [r for r in rows if r["is_repeated_template"]]

    def _group_by_value(rs):
        groups: Dict[str, List[Dict[str, Any]]] = {}
        for r in rs:
            groups.setdefault(r["value_normalized"], []).append(r)
        return groups

    if unique_rows:
        groups = _group_by_value(unique_rows)
        if len(groups) == 1:
            # Antes de confirmar "confidente", chequear si el campo tiene
            # alta diversidad de valores legítimos entre TODOS los
            # documentos (únicos + plantilla) — señal de que es un campo
            # que varía por sesión/documento (ej. "tema", "semana/sesión"
            # de cada sesión), no una constante del curso, aunque por una
            # particularidad de table_signature() (Fase 3.5) solo UN
            # documento haya quedado marcado como "único". Sin este
            # chequeo el índice respondía con el valor de UNA sola sesión
            # arbitraria como si fuera universal ante "¿cuál es el tema de
            # la sesión?" sin especificar cuál — encontrado con datos
            # reales en la evaluación de 218 preguntas sobre 2 cursos
            # distintos (categoría de metadata estructurada). Umbral (5
            # valores distintos de 5+ documentos) calibrado con datos
            # reales de ambos cursos: separa "docente"/"escala de
            # valoración" (1-3 valores, ruido de formato menor, siguen
            # confidentes) de "tema"/"semana / sesión" (13 valores
            # genuinamente distintos, se degradan a insuficiente). Genérico
            # — no depende de qué campo es, solo de cuántos valores/
            # documentos distintos respaldan ESTE campo en ESTA colección.
            all_rows = unique_rows + template_rows
            all_groups = _group_by_value(all_rows)
            distinct_docs = len({r["document_filename"] for r in all_rows})
            if len(all_groups) >= 5 and distinct_docs >= 5:
                return {
                    "status": "insufficient", "field": field_normalized,
                    "low_confidence_evidence": all_rows,
                    "reason": "alta_diversidad_de_valores_entre_documentos_de_la_coleccion",
                }
            evidence = next(iter(groups.values()))
            return {"status": "confident", "field": field_normalized,
                     "value": evidence[0]["value_raw"], "evidence": evidence}
        return {
            "status": "ambiguous", "field": field_normalized,
            "candidates": [{"value": ev[0]["value_raw"], "evidence": ev} for ev in groups.values()],
        }

    if template_rows:
        return {"status": "insufficient", "field": field_normalized, "low_confidence_evidence": template_rows}

    return {"status": "not_found", "field": field_normalized}


def _detect_row_by_value(collection_name: str, query: str, min_score: float = 0.4) -> Optional[str]:
    """Respaldo genérico para tablas donde la PRIMERA columna funciona como
    identificador de fila en vez de nombre de campo (ej. una tabla de
    ponderación: 'Componente: Examen sumativo | Ponderación: 20% | ...' —
    la pregunta ('¿cuánto vale el examen sumativo?') nombra el VALOR
    'examen sumativo', no la palabra 'Componente'). Busca coincidencia
    léxica (Jaccard) contra los VALORES indexados (no los nombres de
    campo) y devuelve el row_id de la fila que mejor matchea.

    Mismo filtro de "valores de fondo" que detect_field aplica a nombres
    de campo: un valor que se repite igual en muchas filas distintas (ej.
    'Odontología' como Programa de estudio en cada sesión semanal) no
    sirve para identificar UNA fila puntual — se excluye por frecuencia,
    calculado de los datos, no de una lista fija.

    Misma jerarquía de autoridad que resolve_field() (is_repeated_template):
    entre varias filas que matchean el mismo valor por igual score (ej.
    'Presencial' aparece como valor en decenas de sesiones semanales que
    son plantillas repetidas, y también UNA vez en el sílabo real), la
    fila de mayor autoridad (no-plantilla) debe ganar siempre, nunca el
    orden arbitrario de la fila que SQL devuelva primero. Antes de este
    fix esta función no leía is_repeated_template en absoluto — bug real
    encontrado con datos reales: 'el curso es presencial o virtual?'
    resolvía a una sesión semanal (plantilla) en vez de al sílabo (única),
    dando 'presencial' cuando el valor real vigente era 'virtual
    sincrónica'. Genérico: no depende de qué valor/campo es, solo de si
    la fila viene de una tabla repetida o no."""
    query_words = _content_words(query)
    if not query_words:
        return None
    rows = db.fetch_all(
        f"""SELECT DISTINCT value_normalized, row_id, is_repeated_template FROM {TABLE_NAME}
            WHERE qdrant_collection_name = %s AND row_id IS NOT NULL AND value_normalized IS NOT NULL""",
        (collection_name,),
    )
    if not rows:
        return None

    value_word_map: Dict[str, set] = {}
    value_doc_freq: Dict[str, int] = {}
    for row in rows:
        value_normalized = row["value_normalized"]
        vw = _content_words(value_normalized)
        value_word_map[value_normalized] = vw
        for w in vw:
            value_doc_freq[w] = value_doc_freq.get(w, 0) + 1
    common_words = {
        w for w, c in value_doc_freq.items()
        if c > max(4, len(rows) * 0.03)
    }
    query_words = query_words - common_words
    if not query_words:
        return None

    def _best(candidate_rows) -> tuple:
        best_row_id, best_score = None, 0.0
        for row in candidate_rows:
            value_normalized, row_id = row["value_normalized"], row["row_id"]
            all_value_words = value_word_map[value_normalized]
            if not all_value_words or len(all_value_words) > 6:
                continue  # valores muy largos (párrafos) no son "identificadores de fila" — se ignoran
            value_words = all_value_words - common_words
            if not value_words:
                continue
            overlap = query_words & value_words
            if not overlap:
                continue
            union = query_words | value_words
            score = len(overlap) / len(union)
            if score > best_score:
                best_row_id, best_score = row_id, score
        return best_row_id, best_score

    unique_rows = [r for r in rows if not r["is_repeated_template"]]
    template_rows = [r for r in rows if r["is_repeated_template"]]

    best_row_id, best_score = _best(unique_rows)
    if best_score >= min_score:
        return best_row_id

    best_row_id, best_score = _best(template_rows)
    return best_row_id if best_score >= min_score else None


def _resolve_row(collection_name: str, row_id: str, query: str) -> Dict[str, Any]:
    """Dado un row_id ya identificado por valor (ver _detect_row_by_value),
    busca DENTRO de esa misma fila el campo que la pregunta pide (ej.
    'ponderación' para '¿cuánto vale?'). Si ningún campo de la fila
    matchea específicamente, devuelve la fila completa igual — mejor
    mostrar toda la fila (con su fuente) que no mostrar nada."""
    row_fields = db.fetch_all(
        f"""SELECT field_normalized, field_raw, value_raw, document_filename,
                   page, section_heading, is_repeated_template
            FROM {TABLE_NAME} WHERE qdrant_collection_name = %s AND row_id = %s""",
        (collection_name, row_id),
    )
    if not row_fields:
        return {"status": "not_found"}

    query_words = _content_words(query)
    best, best_score = None, 0.0
    for rf in row_fields:
        fw = _content_words(rf["field_normalized"])
        if not fw:
            continue
        overlap = query_words & fw
        score = len(overlap) / len(fw)
        if score > best_score:
            best, best_score = rf, score
    if best and best_score >= 0.4:
        return {"status": "confident", "field": best["field_normalized"],
                 "value": best["value_raw"], "evidence": [best], "matched_by": "row_value"}
    return {"status": "confident_row", "evidence": row_fields, "matched_by": "row_value"}


async def structured_lookup(
    collection_name: str, query: str, dense_strategy_name: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Punto de entrada único: (1) intenta detectar un CAMPO nombrado
    directamente en la pregunta (caso típico: '¿quién es el docente?'),
    léxico primero y respaldo semántico por embeddings si el léxico no
    encuentra nada (ver detect_field/_semantic_detect_field — cubre
    sinónimos como 'profesor'/'maestro' sin diccionario curado); (2) si no
    hay campo, intenta detectar una FILA por su VALOR (caso 'Componente:
    Examen sumativo' — la pregunta nombra el valor, no el nombre de
    columna). None si ninguna de las dos aplica — el caller sigue con el
    pipeline RAG normal sin cambios."""
    field = await detect_field(collection_name, query, dense_strategy_name=dense_strategy_name)
    if field is not None:
        return resolve_field(collection_name, field)

    row_id = _detect_row_by_value(collection_name, query)
    if row_id is not None:
        return _resolve_row(collection_name, row_id, query)

    return None


# ════════════════════════════════════════════════════════════════════
# STRUCTURED QUERY ENGINE (Incremento 1 del agente híbrido) — agregaciones
# deterministas (COUNT/MIN/MAX) que NINGUNA función anterior de este módulo
# resuelve: todas las de arriba devuelven un único valor de campo o una
# única fila, nunca "cuántos distintos X hay". Nace del bug real de
# producción: "¿cuántas semanas tiene la unidad 2?" no tenía ningún camino
# determinista, así que el LLM contaba sobre chunks de RAG y confundía una
# MUESTRA (3 semanas usadas en una fórmula de calificación de un Excel de
# notas) con el TOTAL real (8 semanas, semanas 9-16) — confirmado en vivo
# contra producción, dos rondas de refuerzo de prompt no lo cerraron.
# ════════════════════════════════════════════════════════════════════

# Jerarquía de autoridad configurable (tabla fuente_autoridad, ver
# db_migration_course_map_y_fuente_autoridad.sql) — desempata entre tipos
# de fuente cuando la autoridad binaria de is_repeated_template no alcanza
# (dos fuentes "únicas" pero de distinto tipo, ej. sílabo vs. un Excel de
# notas). Cacheada en memoria por poco tiempo — es una tabla chica que casi
# nunca cambia, pero se recalcula por colección para no pegarle a Postgres
# en cada pregunta.
_authority_cache: Optional[Dict[str, int]] = None


def _get_source_authority() -> Dict[str, int]:
    global _authority_cache
    if _authority_cache is None:
        rows = db.fetch_all(
            "SELECT source_type, prioridad FROM fuente_autoridad WHERE activo = TRUE"
        )
        _authority_cache = {r["source_type"]: r["prioridad"] for r in rows}
    return _authority_cache


def _infer_document_source_type(source_type: str, n_fields: int, template_ratio: float, max_fields_in_collection: int) -> str:
    """Clasificación GENÉRICA (no depende del nombre de ningún archivo ni
    curso puntual) de qué tipo de documento es, usando señales que ya
    calcula el pipeline para cualquier colección:
    - 'bibliografia' viene tal cual del tag ya existente en Qdrant/índice.
    - dentro de 'curso_propio': el documento con MÁS campos distintos de
      toda la colección es, con datos reales de ambos cursos probados esta
      sesión, el sílabo (47 campos en Curso_1472 vs. 18-19 de cada sesión
      semanal y ≤12 de cualquier otro material) — un sílabo es
      estructuralmente el documento administrativo más denso del curso.
    - un documento cuyas filas son CASI TODAS de una estructura de tabla
      repetida (is_repeated_template) en un porcentaje alto es una sesión
      semanal (la plantilla "Programa/Unidad/Semana/Modalidad/..." se
      repite idéntica en cada semana, solo cambian los valores) — OJO:
      is_repeated_template es una señal de ESTRUCTURA repetida, no de
      "valor vacío"; los valores de Unidad/Semana de estas filas SÍ son
      reales y hay que poder contarlos (ver count_distinct más abajo, que
      deliberadamente NO filtra por is_repeated_template).
    - todo lo demás dentro de curso_propio queda como material docente
      genérico."""
    if source_type == "bibliografia":
        return "bibliografia"
    # Piso 20 (antes 25): con el fix de A.5 el sílabo ya no arrastra los
    # nombres de campo corruptos que inflaban su conteo, y en colecciones
    # reales queda en ~21-25 campos. Las sesiones semanales tienen <=20
    # (verificado con datos reales de Curso_1472/Curso_1541 y de la réplica
    # de preprod Curso_11/Curso_12), así que 20 sigue separando bien al
    # sílabo (el de MÁS campos) de una sesión.
    if n_fields == max_fields_in_collection and n_fields >= 20:
        return "silabo"
    if template_ratio >= 0.5:
        # Umbral calibrado con datos reales de Curso_1472: las sesiones
        # semanales miden 0.75-1.0 (una semana con contenido más variado
        # bajó a 0.75), y TODO lo demás (sílabo, bibliografía, material
        # suelto) mide exactamente 0.0 — separación amplia, no un punto
        # de corte ajustado.
        return "sesion_semanal"
    return "material_docente"


def _document_source_types(collection_name: str) -> Dict[str, str]:
    """document_filename -> tipo de fuente inferido, para toda la colección
    de una sola pasada (evita N consultas)."""
    rows = db.fetch_all(
        f"""SELECT document_filename, source_type,
                   COUNT(DISTINCT field_normalized) AS n_fields,
                   AVG(CASE WHEN is_repeated_template THEN 1.0 ELSE 0.0 END) AS template_ratio
            FROM {TABLE_NAME}
            WHERE qdrant_collection_name = %s
            GROUP BY document_filename, source_type""",
        (collection_name,),
    )
    if not rows:
        return {}
    # El máximo de campos (señal de "cuál documento es el sílabo") se calcula
    # SOLO entre documentos que no son bibliografía: un material de lectura
    # extenso puede acumular muchos campos distintos (y nunca es el sílabo),
    # y con el fix de A.5 puede terminar con más campos que el sílabo real,
    # desplazándolo de la clasificación. La bibliografía ya se clasifica como
    # tal por su propio source_type, así que no debe competir por el máximo.
    candidate_fields = [r["n_fields"] for r in rows if r["source_type"] != "bibliografia"]
    max_fields = max(candidate_fields) if candidate_fields else max(r["n_fields"] for r in rows)
    return {
        r["document_filename"]: _infer_document_source_type(
            r["source_type"], r["n_fields"], float(r["template_ratio"]), max_fields
        )
        for r in rows
    }


_ROMAN = ["", "I", "II", "III", "IV", "V", "VI", "VII", "VIII", "IX", "X",
          "XI", "XII", "XIII", "XIV", "XV", "XVI", "XVII", "XVIII", "XIX", "XX"]


def _arabic_to_roman_prefix(text: str) -> Optional[str]:
    """'2' o 'ii' -> 'ii' (normalizado, para matchear el prefijo real
    'ii. especialidades...' que queda en value_normalized). Genérico hasta
    20 (ningún curso real tiene más unidades que eso); no depende de
    contenido de ningún curso puntual, solo de numeración."""
    t = text.strip().lower()
    if t.isdigit():
        n = int(t)
        if 1 <= n < len(_ROMAN):
            return _ROMAN[n].lower()
        return None
    if t and all(c in "ivxlcdm" for c in t):
        return t
    return None


_SECTION_WEEK_RE = re.compile(r"^\s*\S+\s+(\d+)\s*:?")
_SECTION_UNIT_RE = re.compile(r"unidad\s+([ivx\d]+)", re.IGNORECASE)
# Término académico genérico de examen de rezagados — NO es contenido
# curado de ningún curso puntual (igual que "General" ya se excluye por
# ser section_number==0, esto excluye la semana de examen de aplazados
# por lo que ES, no por a qué curso pertenece).
_SECTION_APLAZADOS_RE = re.compile(r"aplazad", re.IGNORECASE)


def _moodle_weeks_with_units(course_id: int) -> Dict[int, Dict[str, Any]]:
    """Semanas REALES del curso según Moodle (course_map.py, ya cacheado,
    sin llamada nueva) — fuente de verdad de CUÁLES semanas existen,
    independiente de si tienen contenido vectorizado o no. El número de
    semana se extrae de forma genérica (primera palabra + número al
    inicio del nombre de sección, ej. "Semana 12 :" o "Senana 04 :" — el
    typo real de Moodle no rompe esto porque no se busca la palabra
    "semana" literal, solo el patrón posicional palabra+número). Si el
    propio nombre de la sección menciona "Unidad X" (visto con datos
    reales en las semanas de cierre de unidad), se captura como
    confirmación directa de Moodle, no inferencia."""
    course = _course_map.get_course_map(course_id)
    result: Dict[int, Dict[str, Any]] = {}
    for s in course.sections:
        if s.section_number == 0:
            continue
        if _SECTION_APLAZADOS_RE.search(s.name):
            continue
        m = _SECTION_WEEK_RE.match(s.name)
        if not m:
            continue
        week_n = int(m.group(1))
        topic = s.name[m.end():].strip(" :.-") or s.name
        unit_m = _SECTION_UNIT_RE.search(s.name)
        unit_roman = _arabic_to_roman_prefix(unit_m.group(1)) if unit_m else None
        result[week_n] = {"topic": topic, "unit_roman": unit_roman}
    return result


def _extract_week_number(value: str) -> Optional[int]:
    m = re.search(r"\d+", value)
    return int(m.group()) if m else None


def _merge_weeks_with_course_map(
    distinct_values: set, extras: Dict[str, str], course_id: int,
    unit_filter_roman: Optional[str],
) -> Dict[str, Any]:
    """Completa distinct_values/extras con semanas reales de Moodle que no
    están en el índice estructurado (ver docstring de count_distinct).
    Sin filtro de unidad: agrega CUALQUIER semana real de Moodle que
    falte. Con filtro de unidad: solo agrega una semana si (a) su propio
    nombre de sección confirma esa unidad, o (b) cae en un hueco INTERIOR
    entre dos semanas ya confirmadas de la misma unidad por el índice
    (nunca se extrapola más allá de ese rango confirmado). Devuelve
    también `sources` (value -> 'indexado'|'moodle'|'continuidad') para
    que la evidencia sea transparente sobre el origen de cada dato."""
    sources = {v: "indexado" for v in distinct_values}
    try:
        moodle_weeks = _moodle_weeks_with_units(course_id)
    except Exception as e:
        print(f"⚠️ No se pudo leer el Course Map para completar semanas (se ignora): {e}")
        return {"values": distinct_values, "extras": extras, "sources": sources}

    covered = {n: v for v in distinct_values if (n := _extract_week_number(v)) is not None}

    if unit_filter_roman is None:
        for week_n, info in moodle_weeks.items():
            if week_n in covered:
                continue
            value = f"semana {week_n} / sesion {week_n}"
            distinct_values.add(value)
            extras.setdefault(value, info["topic"])
            sources[value] = "moodle"
        return {"values": distinct_values, "extras": extras, "sources": sources}

    if not covered:
        return {"values": distinct_values, "extras": extras, "sources": sources}
    lo, hi = min(covered), max(covered)
    for week_n, info in moodle_weeks.items():
        if week_n in covered:
            continue
        value = f"semana {week_n} / sesion {week_n}"
        if info["unit_roman"] == unit_filter_roman:
            distinct_values.add(value)
            extras.setdefault(value, info["topic"])
            sources[value] = "moodle"
        elif info["unit_roman"] is None and lo < week_n < hi:
            distinct_values.add(value)
            extras.setdefault(value, info["topic"])
            sources[value] = "continuidad"
    return {"values": distinct_values, "extras": extras, "sources": sources}


def _table_key(r: Dict[str, Any]) -> tuple:
    """Misma 'fila lógica' = mismo documento + misma tabla física (prefijo
    'pt{N}' del row_id) — CORREGIDO tras verificar con datos reales que
    una tabla campo:valor vertical (ej. sesión semanal) da un row_id
    DISTINTO por cada fila (una fila = un campo), así que Unidad y Semana
    NUNCA comparten row_id exacto — comparten documento + tabla."""
    rid = r["row_id"] or ""
    m = re.match(r"^(.*::pt\d+)::r\d+$", rid)
    return (r["document_filename"], m.group(1) if m else rid)


def count_distinct(
    collection_name: str, group_field: str,
    filter_field: Optional[str] = None, filter_value: Optional[str] = None,
    extra_field: Optional[str] = None, course_id: Optional[int] = None,
) -> Dict[str, Any]:
    """P.ej. count_distinct(coll, group_field='semana / sesion',
    filter_field='unidad', filter_value='II') -> cuántas semanas distintas
    tiene la unidad II. Si filter_field/filter_value son None, cuenta
    TODOS los valores distintos de group_field entre los documentos
    elegibles (ej. "¿cuántas semanas tiene el curso?", sin filtrar por
    unidad). `extra_field` (opcional): un campo adicional a traer por cada
    fila que matchea (ej. 'tema') — para preguntas tipo "¿de qué trata
    cada semana?" sin una segunda consulta.

    Deliberadamente NO filtra por is_repeated_template (ver docstring de
    _infer_document_source_type: esa señal marca ESTRUCTURA repetida, no
    valor vacío — las semanas de cada sesión son datos reales distintos
    aunque la tabla que las contiene tenga la misma forma en las 16
    semanas). En su lugar, restringe a documentos clasificados como
    'silabo' o 'sesion_semanal' — la fuente de verdad real para conteos de
    unidad/semana — excluyendo bibliografía y material docente suelto, que
    es lo que de verdad evita el bug (un Excel de notas no es
    'sesion_semanal' ni 'silabo', así que nunca entra a este conteo aunque
    esté indexado).

    `course_id` (opcional): si se pasa Y group_field es semana/sesión, el
    resultado se completa con las semanas REALES del curso en Moodle
    (course_map.py) que no tengan contenido vectorizado todavía — el
    índice estructurado depende de qué se vectorizó, Moodle no (ver
    _merge_weeks_with_course_map). Sin esto, un hueco de vectorización
    (ej. una semana sin documento indexado) hacía que el conteo pareciera
    menor al real — encontrado con datos reales (Curso_1472, semana 4)."""
    has_filter = bool(filter_field and filter_value)
    field_n = normalize_field(filter_field) if has_filter else None
    value_n = (_arabic_to_roman_prefix(filter_value) or normalize_value(filter_value)) if has_filter else None
    group_n = normalize_field(group_field)
    extra_n = normalize_field(extra_field) if extra_field else None
    is_week_query = "semana" in group_n

    distinct_values: set = set()
    matched_docs: set = set()
    extras: Dict[str, str] = {}

    doc_types = _document_source_types(collection_name)
    eligible_docs = [f for f, t in doc_types.items() if t in ("silabo", "sesion_semanal")]
    if eligible_docs:
        wanted_fields = {group_n} | ({field_n} if field_n else set()) | ({extra_n} if extra_n else set())
        placeholders = ",".join(["%s"] * len(eligible_docs))
        field_placeholders = ",".join(["%s"] * len(wanted_fields))
        rows = db.fetch_all(
            f"""SELECT document_filename, row_id, field_normalized, value_normalized, value_raw
                FROM {TABLE_NAME}
                WHERE qdrant_collection_name = %s AND document_filename IN ({placeholders})
                  AND field_normalized IN ({field_placeholders})""",
            (collection_name, *eligible_docs, *wanted_fields),
        )
        tables: Dict[Any, Dict[str, str]] = {}
        tables_raw: Dict[Any, Dict[str, str]] = {}
        for r in rows:
            key = _table_key(r)
            tables.setdefault(key, {})[r["field_normalized"]] = r["value_normalized"]
            tables_raw.setdefault(key, {})[r["field_normalized"]] = r["value_raw"]

        for key, fields in tables.items():
            doc = key[0]
            gv = fields.get(group_n)
            if not gv:
                continue
            if has_filter:
                fv = fields.get(field_n)
                if not fv or not (fv == value_n or fv.startswith(value_n + ".")):
                    continue
            distinct_values.add(gv)
            matched_docs.add(doc)
            if extra_n and fields.get(extra_n):
                # Texto original (con tildes/mayúsculas), no el normalizado
                # usado para matchear — el normalizado es para comparar de
                # forma robusta, no para mostrárselo al estudiante (bug
                # real: el saludo mostraba "evolucion de la odontologia
                # peruana" sin tildes cuando se interpola directo, sin
                # pasar por el LLM que lo "arregla" al redactar la
                # respuesta conversacional).
                extras[gv] = tables_raw.get(key, {}).get(extra_n) or fields[extra_n]

    sources: Dict[str, str] = {v: "indexado" for v in distinct_values}
    if course_id and is_week_query:
        merged = _merge_weeks_with_course_map(distinct_values, extras, course_id, value_n if has_filter else None)
        distinct_values, extras, sources = merged["values"], merged["extras"], merged["sources"]

    if not distinct_values:
        return {"status": "not_found", "reason": "valor_de_filtro_no_encontrado", "filter_value_normalizado": value_n}

    return {
        "status": "confident",
        "count": len(distinct_values),
        "values": sorted(distinct_values),
        "evidence_documents": sorted(matched_docs),
        "matched_by": "structured_aggregate",
        "topics": extras or None,
        "sources": sources,
    }


def get_table_rows(
    collection_name: str, field_names: List[str],
    source_type_filter: Optional[str] = "silabo",
) -> List[Dict[str, str]]:
    """Trae TODAS las filas de una tabla del índice estructurado que
    contengan cualquiera de `field_names`, agrupadas por fila lógica
    (documento + `row_id` completo) y deduplicadas por CONTENIDO.

    Pensada para tablas "de varias columnas" del sílabo (p.ej. la de
    "Evidencias evaluables / Instrumento / Ponderación", ver A.1 del plan):
    cada fila real se reconstruye como un dict {campo: valor} y se devuelve
    una sola vez aunque el mismo sílabo esté indexado en .docx Y .pdf (cada
    formato es un documento/row_id distinto, pero el contenido de la fila es
    idéntico — de ahí la deduplicación por contenido). Genérica: no
    hardcodea ningún nombre de campo ni de curso, los recibe como parámetro.

    `source_type_filter` (por defecto 'silabo'): restringe a documentos
    clasificados por `_document_source_types()`. None = sin filtro (todos
    los documentos de la colección). Devuelve [] si no hay filas que
    cumplan (nunca inventa una fila vacía)."""
    wanted = {normalize_field(f) for f in (field_names or [])}
    if not wanted:
        return []

    params: List[Any] = [collection_name]
    doc_filter = ""
    if source_type_filter is not None:
        doc_types = _document_source_types(collection_name)
        eligible = [f for f, t in doc_types.items() if t == source_type_filter]
        if not eligible:
            return []
        placeholders = ",".join(["%s"] * len(eligible))
        doc_filter = f" AND document_filename IN ({placeholders})"
        params.extend(eligible)

    field_placeholders = ",".join(["%s"] * len(wanted))
    params.extend(sorted(wanted))
    rows = db.fetch_all(
        f"""SELECT document_filename, row_id, field_normalized, value_raw
            FROM {TABLE_NAME}
            WHERE qdrant_collection_name = %s{doc_filter}
              AND field_normalized IN ({field_placeholders})
            ORDER BY document_filename, row_id""",
        tuple(params),
    )

    # Agrupar por FILA LÓGICA = document_filename + row_id COMPLETO (con el
    # sufijo '::rN'). A diferencia de count_distinct (que agrupa con
    # _table_key() porque ahí cada campo de una tabla vertical es su propia
    # fila), acá cada fila lógica de una tabla horizontal de varias columnas
    # comparte exactamente el mismo row_id ('...::pt98::r0') para todos sus
    # campos. Usar _table_key() colapsaría r0/r1/r2 en un solo dict y
    # perdería filas reales — verificado con datos reales: Curso_1541
    # devolvía 1 fila en vez de 3.
    logical_rows: Dict[Any, Dict[str, str]] = {}
    for r in rows:
        logical_rows.setdefault((r["document_filename"], r["row_id"]), {})[r["field_normalized"]] = r["value_raw"]

    unique: Dict[Any, Dict[str, str]] = {}
    for fields in logical_rows.values():
        if not fields:
            continue
        # Clave de deduplicación = combinación EXACTA de valores (no el
        # row_id): la misma fila indexada desde .docx y .pdf comparte
        # contenido pero no documento/row_id, así que se conserva una sola
        # copia (la primera aparición, en orden document_filename/row_id).
        key = tuple(sorted((k, v or "") for k, v in fields.items()))
        unique.setdefault(key, fields)
    return list(unique.values())


def get_field_by_week(
    collection_name: str, field_name: str, week_field: str = "semana / sesion",
    source_type_filter: Optional[str] = "sesion_semanal",
) -> Dict[int, str]:
    """{número de semana -> valor real de `field_name`} tomado del documento
    de sesión de esa semana (p.ej. 'evidencia de aprendizaje' o 'criterios
    de evaluación').

    Agrupa por DOCUMENTO, no por tabla física: en los documentos de sesión
    semanal el campo 'semana / sesion' y los campos de contenido viven en
    tablas físicas DISTINTAS del mismo documento, así que
    count_distinct(extra_field=...) — que agrupa por `_table_key` — NO los
    cruza (verificado con datos reales: devolvía 16 semanas pero sin
    evidencia/criterios). Como cada documento de sesión corresponde a UNA
    sola semana, agrupar por document_filename sí los cruza correctamente.
    El número de semana se extrae del propio valor real (no del nombre del
    archivo). Semanas sin el campo pedido simplemente no aparecen en el
    resultado — nunca se inventa un valor."""
    week_n = normalize_field(week_field)
    field_n = normalize_field(field_name)
    if not field_n or field_n == week_n:
        return {}

    doc_types = _document_source_types(collection_name)
    if source_type_filter is not None:
        eligible = [f for f, t in doc_types.items() if t == source_type_filter]
    else:
        eligible = list(doc_types.keys())
    if not eligible:
        return {}

    placeholders = ",".join(["%s"] * len(eligible))
    rows = db.fetch_all(
        f"""SELECT document_filename, field_normalized, value_raw
            FROM {TABLE_NAME}
            WHERE qdrant_collection_name = %s AND document_filename IN ({placeholders})
              AND field_normalized IN (%s, %s)""",
        (collection_name, *eligible, week_n, field_n),
    )

    by_doc: Dict[str, Dict[str, str]] = {}
    for r in rows:
        by_doc.setdefault(r["document_filename"], {})[r["field_normalized"]] = r["value_raw"]

    result: Dict[int, str] = {}
    for fields in by_doc.values():
        week_val = fields.get(week_n)
        target = fields.get(field_n)
        if not week_val or not target:
            continue
        m = re.search(r"\d+", week_val)
        if m:
            result[int(m.group())] = target
    return result


def range_bounds(
    collection_name: str, group_field: str, filter_field: str, filter_value: str,
    course_id: Optional[int] = None,
) -> Dict[str, Any]:
    """Primer/último valor (orden natural por número extraído del texto,
    ej. 'semana 9' < 'semana 16' — NO orden lexicográfico, que pondría
    'semana 16' antes que 'semana 9'). Reusa la misma agrupación por tabla
    física que count_distinct(), incluido el completado desde el Course
    Map real si se pasa course_id."""
    base = count_distinct(collection_name, group_field, filter_field, filter_value, course_id=course_id)
    if base["status"] != "confident":
        return base

    def _extract_number(v: str) -> Optional[int]:
        m = re.search(r"\d+", v)
        return int(m.group()) if m else None

    numbered = [(v, _extract_number(v)) for v in base["values"]]
    numbered = [(v, n) for v, n in numbered if n is not None]
    if not numbered:
        return {**base, "first": min(base["values"]), "last": max(base["values"]), "numeric_order": False}

    numbered.sort(key=lambda t: t[1])
    return {**base, "first": numbered[0][0], "last": numbered[-1][0], "numeric_order": True}


def structured_query(collection_name: str, parsed: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Punto de entrada público del Structured Query Engine, análogo a
    structured_lookup() pero para agregaciones. `parsed` viene del Query
    Router (extracción por regex de la pregunta, NUNCA del LLM — ver
    query_router.py): {"operation": "count"|"range", "entity": "unidad"|
    None, "value": "II"|None, "group_field": "semana / sesion",
    "with_topics": bool}. `entity`/`value` en None = sin filtro, cuenta
    TODAS las semanas del curso (no de una unidad puntual — ej. "¿cuántas
    semanas tiene el curso?"). None si la operación no se reconoce; el
    caller sigue con RAG normal."""
    operation = parsed.get("operation")
    entity = parsed.get("entity")
    value = parsed.get("value")
    group_field = parsed.get("group_field", "semana / sesion")
    extra_field = "tema" if parsed.get("with_topics") else None
    course_id = parsed.get("course_id")
    if operation == "count":
        return count_distinct(collection_name, group_field, entity, value, extra_field=extra_field, course_id=course_id)
    if operation == "range":
        if not entity or not value:
            return None
        return range_bounds(collection_name, group_field, entity, value, course_id=course_id)
    return None
