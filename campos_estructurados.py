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
        cur.execute(f"DELETE FROM {TABLE_NAME} WHERE qdrant_collection_name = %s", (collection_name,))
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
