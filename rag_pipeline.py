"""
rag_pipeline.py
================
Orquestación de lectura del pipeline RAG: recuperación híbrida (dense +
sparse fusionados por Qdrant) → reranking → boosting por `source_type` →
construcción del prompt → generación con el tutor. Usado por los
endpoints `/api/rag/*` en app.py y por la sección "Pruebas del LLM".
"""
import asyncio
import re
import time
import unicodedata
from pathlib import Path
from typing import Any, Dict, List, Optional

from qdrant_admin import get_qdrant_admin
from settings import settings
from strategies import registry as strategy_registry
from strategies.generation import SYSTEM_PROMPT_TEMPLATE
from strategies.rerank import apply_source_boost, apply_week_boost, _extract_all_week_numbers
import campos_estructurados
import moodle_tools as mt
import query_router

# `rerank_score` es la probabilidad de relevancia del cross-encoder (0-1,
# sigmoid ya aplicado por BGE/Cohere) ANTES del boost de curso_propio.
# retrieve_rerank_boost() siempre devuelve top_n candidatos aunque ninguno
# sea realmente relevante (p.ej. preguntas conversacionales/meta que no
# citan ningún documento, o "no entiendo la parte X" sin contexto previo)
# — sin este filtro esos candidatos igual se mandaban al LLM como contexto
# (y se mostraban como "fuentes"), y el modelo terminaba construyendo una
# respuesta con contenido con ~0.001% de relevancia real en vez de admitir
# que no tiene con qué responder. Se filtra sobre el score SIN boost: el
# boost es para priorizar orden entre candidatos ya relevantes, no una
# señal de relevancia en sí misma.
MIN_SOURCE_RELEVANCE = 0.01


async def hybrid_retrieve(
    *,
    collection_name: str,
    query: str,
    dense_strategy_name: Optional[str] = None,
    sparse_strategy_name: Optional[str] = None,
    top_k: Optional[int] = None,
    prefetch_limit: Optional[int] = None,
    timings: Optional[Dict[str, float]] = None,
) -> List[Dict[str, Any]]:
    """Embebe la consulta con las estrategias dense+sparse activas y
    recupera candidatos fusionados (RRF) en una sola llamada a Qdrant."""
    dense = strategy_registry.get_dense_strategy(dense_strategy_name)
    sparse = strategy_registry.get_sparse_strategy(sparse_strategy_name)

    # dense.embed() y sparse.embed() son independientes entre sí (ambas solo
    # necesitan el texto de la consulta) — antes se esperaban una después de
    # la otra, sumando sus tiempos en vez de solaparlos.
    t0 = time.perf_counter()
    (dense_vector,), (sparse_vector,) = await asyncio.gather(
        dense.embed([query], is_query=True),
        sparse.embed([query]),
    )
    if timings is not None:
        timings["embed_query_ms"] = (time.perf_counter() - t0) * 1000

    admin = get_qdrant_admin()
    # admin.hybrid_search() es una llamada síncrona (qdrant_client con HTTP
    # sync) — sin run_in_executor bloquearía el event loop entero mientras
    # dura la consulta a Qdrant, serializando a TODOS los usuarios
    # concurrentes (de este asistente y de cualquier otro) hasta que
    # termine. Este era el único punto del pipeline de lectura sin
    # offload; embed()/rerank() ya lo hacían.
    t0 = time.perf_counter()
    loop = asyncio.get_event_loop()
    hits = await loop.run_in_executor(
        None,
        lambda: admin.hybrid_search(
            collection_name=collection_name,
            dense_query=dense_vector,
            sparse_indices=sparse_vector["indices"],
            sparse_values=sparse_vector["values"],
            limit=top_k or settings.RERANK_TOP_K,
            **({"prefetch_limit": prefetch_limit} if prefetch_limit else {}),
        ),
    )
    if timings is not None:
        timings["qdrant_search_ms"] = (time.perf_counter() - t0) * 1000

    return [
        {
            "id": h.id,
            "score": h.score,
            "text": (h.payload or {}).get("text", ""),
            "payload": h.payload or {},
        }
        for h in hits
    ]


async def retrieve_rerank_boost(
    *,
    collection_name: str,
    query: str,
    dense_strategy_name: Optional[str] = None,
    sparse_strategy_name: Optional[str] = None,
    rerank_strategy_name: Optional[str] = None,
    top_k: Optional[int] = None,
    top_n: Optional[int] = None,
    timings: Optional[Dict[str, float]] = None,
) -> List[Dict[str, Any]]:
    """hybrid_retrieve() -> reranker.rerank() sobre todo el pool recuperado
    -> apply_source_boost() (prioriza curso_propio) -> apply_week_boost()
    (prioriza coincidencia exacta de semana/sesión si la pregunta menciona
    una) -> top_n final.

    Preguntas de dato puntual de tabla (docente, créditos, modalidad...) ya
    no dependen de que el chunk correcto entre a este pool de similitud —
    se resuelven antes, en answer_query(), vía el Índice de Campos
    Estructurados (campos_estructurados.py), que se inyecta como evidencia
    directa sin pasar por retrieval/reranking. El router de palabras clave
    que existía acá (Fase 3.7, _METADATA_FIELD_PATTERNS) fue retirado tras
    comprobar que el índice iguala sus métricas con 0 reglas curadas — ver
    informe en Data prueba/.

    Preguntas que mencionan 2+ semanas/sesiones distintas ("compara la
    semana 9 con la semana 10") piden un pool más profundo desde el
    principio: apply_week_boost() ya boostea candidatos de CUALQUIERA de
    las semanas mencionadas, pero eso solo ayuda si esos candidatos llegan
    a estar en el pool — con el top_k por defecto, a veces solo una de las
    2 semanas entra. Señal 100% genérica (cuenta de números de semana en
    la pregunta, ver strategies.rerank._extract_all_week_numbers), no
    depende de ningún curso/tema puntual."""
    effective_top_k = top_k
    effective_prefetch_limit = None
    if effective_top_k is None and len(_extract_all_week_numbers(query)) >= 2:
        effective_top_k = max(settings.RERANK_TOP_K, 150)
        effective_prefetch_limit = 300

    candidates = await hybrid_retrieve(
        collection_name=collection_name,
        query=query,
        dense_strategy_name=dense_strategy_name,
        sparse_strategy_name=sparse_strategy_name,
        top_k=effective_top_k,
        prefetch_limit=effective_prefetch_limit,
        timings=timings,
    )
    if not candidates:
        return []

    n = top_n or settings.RERANK_TOP_N
    reranker = strategy_registry.get_rerank_strategy(rerank_strategy_name)
    t0 = time.perf_counter()
    reranked = await reranker.rerank(query, candidates, top_n=len(candidates))
    if timings is not None:
        timings["rerank_ms"] = (time.perf_counter() - t0) * 1000
    # apply_source_boost sin truncar todavía (top_n=len(reranked)): el
    # boost de semana/sesión necesita ver el pool completo, no solo lo que
    # ya haya sobrevivido al corte de curso_propio/bibliografía.
    boosted = apply_source_boost(reranked, settings.RERANK_BOOST_MULTIPLIER, top_n=len(reranked))
    week_boosted = apply_week_boost(boosted, query)
    return week_boosted[:n]


# Verificación de fuente solicitada por nombre — genérica, no hardcodea
# ningún documento concreto. Detecta cuando la pregunta nombra un TIPO de
# documento (reglamento, manual, sílabo, artículo, norma, guía, ley,
# informe, libro, cuadro, tabla, registro, resolución, decreto, protocolo,
# diccionario, glosario, documento o archivo) y compara ese nombre contra
# el `filename` REAL de cada candidato recuperado — un dato de metadata
# objetivo, no una decisión del LLM. Nace del caso real reproducido: le
# preguntaron por "el reglamento de investigación" (que no existe en el
# curso) y el modelo tomó esa frase de la propia pregunta y la pegó sobre
# contenido real pero de OTRO documento (TESIS.pdf) como si lo describiera.
_DOC_TYPE_WORDS = (
    "reglamento", "manual", "s[ií]labo", "art[ií]culo", "norma(?:\\s+t[ée]cnica)?",
    "gu[ií]a", "ley", "informe", "libro", "cuadro", "tabla", "registro", "acta",
    "resoluci[oó]n", "decreto", "protocolo", "documento", "archivo",
    "diccionario", "glosario",
)
_DOC_REFERENCE_RE = re.compile(
    r"\b(?:el|la|los|las|del|de la)\s+(" + "|".join(_DOC_TYPE_WORDS) + r")\b"
    r"(?:\s+(?:de|del|de la|sobre))?\s*([a-záéíóúñ0-9][^?.,;:]{2,60})?",
    re.IGNORECASE,
)
# Mención literal de un archivo por nombre (ej. "TESIS.pdf", "el archivo
# Norma-Tecnica-del-Odontograma.pdf") — más específica y confiable que el
# patrón de arriba cuando existe. Se ancla a lo sumo 4 palabras antes de la
# extensión (no toda la oración); las palabras de relleno (interrogativos,
# verbos, "archivo"/"documento") se recortan después con
# _FILLER_PREFIX_WORDS para no diluir el match contra el filename real.
_FILENAME_MENTION_RE = re.compile(
    r"(?:[\w\-]+[ ]){0,4}[\w\-]+\.(?:pdf|docx?|pptx?|xlsx?|txt|csv)\b", re.IGNORECASE
)
_STOPWORDS_MATCH = {
    "de", "del", "la", "el", "los", "las", "que", "sobre", "con", "para",
    "en", "y", "o", "un", "una", "curso", "este", "esta", "ese", "esa",
}
_FILLER_PREFIX_WORDS = {
    "que", "cual", "cuales", "quien", "dime", "dame", "muestrame", "dice",
    "decir", "archivo", "documento", "el", "la", "los", "las", "un", "una",
    "del", "de",
}


def _strip_filler_prefix(text: str) -> str:
    """Recorta palabras de relleno al INICIO de una mención de archivo
    (ej. "Qué dice el archivo TESIS.pdf" -> "TESIS.pdf") sin tocar el
    resto — un filename real puede tener espacios propios (ej. "SILABO
    INTRODUCCIÓN A LA ODONTOLOGÍA.pdf"), así que solo se recorta el
    prefijo, nunca el interior."""
    words = text.split()
    while len(words) > 1:
        normalized = unicodedata.normalize("NFKD", words[0]).encode("ascii", "ignore").decode().lower()
        if normalized in _FILLER_PREFIX_WORDS:
            words.pop(0)
        else:
            break
    return " ".join(words)


def _extract_document_reference(query: str) -> Optional[str]:
    """Si la pregunta nombra explícitamente un documento/reglamento/manual/
    archivo puntual, devuelve ese nombre tal como lo escribió el
    estudiante. None si la pregunta no referencia ninguna fuente puntual
    (la inmensa mayoría de las preguntas — esta función no debe alterar su
    comportamiento en absoluto)."""
    m = _FILENAME_MENTION_RE.search(query)
    if m:
        return _strip_filler_prefix(m.group(0).strip())
    m = _DOC_REFERENCE_RE.search(query)
    if m:
        doc_type = m.group(1)
        topic = (m.group(2) or "").strip()
        return f"{doc_type} {topic}".strip()
    return None


_EXTENSION_RE = re.compile(r"\.(?:pdf|docx?|pptx?|xlsx?|txt|csv)\b", re.IGNORECASE)


def _tokenize_for_match(text: str) -> set:
    # Se quita la extensión ANTES de tokenizar — si no, "TESIS.pdf" aporta
    # el token "pdf" del lado de la referencia pero no del lado del
    # filename real (Path.stem ya se la sacó), diluyendo el match para
    # nombres de un solo término.
    without_ext = _EXTENSION_RE.sub("", text or "")
    normalized = unicodedata.normalize("NFKD", without_ext).encode("ascii", "ignore").decode()
    words = re.findall(r"[a-z0-9]+", normalized.lower())
    return {w for w in words if w not in _STOPWORDS_MATCH and len(w) > 2}


def _filename_matches_reference(filename: str, reference: str) -> bool:
    """True si el `filename` REAL de un chunk coincide razonablemente con
    lo que el estudiante nombró — comparación léxica sobre metadata
    (nunca semántica/por tema), tolerante a que falte alguna palabra
    (typos reales vistos en nombres de archivo de Moodle) pero exige que
    la MAYORÍA de las palabras con contenido de la referencia aparezcan en
    el nombre real del archivo."""
    ref_tokens = _tokenize_for_match(reference)
    if not ref_tokens:
        return False
    fname_tokens = _tokenize_for_match(Path(filename or "").stem)
    overlap = ref_tokens & fname_tokens
    return len(overlap) >= max(1, -(-len(ref_tokens) * 6 // 10))  # ceil(60%)


def annotate_source_match(
    candidates: List[Dict[str, Any]], reference: Optional[str]
) -> bool:
    """Marca en cada candidato (`c["source_match"]`) si su archivo real
    coincide con la fuente que el estudiante pidió por nombre. No filtra
    ni reordena nada — solo etiqueta, para que build_generation_prompt()
    pueda mostrarle al LLM cuáles fragmentos SÍ son la fuente pedida y
    cuáles son de un documento distinto. Devuelve True si al menos un
    candidato coincide."""
    if reference is None:
        for c in candidates:
            c["source_match"] = None
        return False
    found = False
    for c in candidates:
        matches = _filename_matches_reference(c.get("payload", {}).get("filename") or "", reference)
        c["source_match"] = matches
        found = found or matches
    return found


def _find_course_map_resource(course_id: Optional[int], reference: str) -> Optional[Dict[str, Any]]:
    """Busca en el Course Map real (Moodle) un recurso/actividad cuyo nombre
    coincida con la fuente que pidió el estudiante. Sirve para el caso 1.6:
    el recurso existe en el aula pero su contenido no está indexado. None si
    no hay course_id o ningún match razonable (nunca inventa)."""
    if not course_id or not reference:
        return None
    try:
        matches = mt.find_activity(course_id, reference)
    except Exception:
        return None
    if matches and matches[0].get("score", 0) >= 0.4:
        return matches[0]
    return None


def build_generation_prompt(
    query: str,
    candidates: List[Dict[str, Any]],
    document_reference: Optional[str] = None,
    requested_source_found: Optional[bool] = None,
    course_map_match: Optional[Dict[str, Any]] = None,
) -> str:
    """Agrupa los candidatos recuperados en bloques [MATERIAL DEL CURSO] /
    [BIBLIOGRAFÍA COMPLEMENTARIA] con citas (filename, diapositiva/minuto/
    fragmento), seguidos de la pregunta del estudiante.

    Si `document_reference` no es None (el estudiante nombró una fuente
    puntual — ver `_extract_document_reference`), antepone una línea de
    estado objetiva (encontrada o no, según metadata real) y marca cada
    fragmento que NO pertenece a esa fuente como "[FUENTE DISTINTA A LA
    SOLICITADA]" para que el modelo nunca lo presente como si fuera el
    documento pedido."""
    curso_blocks: List[str] = []
    biblio_blocks: List[str] = []

    for c in candidates:
        payload = c.get("payload", {})

        citation_parts = [payload.get("filename") or "documento"]
        if payload.get("section_name"):
            citation_parts.append(f"semana/sección: {payload['section_name']}")
        if payload.get("slide_number") is not None:
            citation_parts.append(f"diapositiva {payload['slide_number']}")
        start_time = payload.get("start_time")
        if start_time is not None:
            minutes, seconds = divmod(int(start_time), 60)
            citation_parts.append(f"minuto {minutes}:{seconds:02d}")
        elif payload.get("chunk") is not None:
            citation_parts.append(f"fragmento {payload['chunk']}")
        citation = ", ".join(str(p) for p in citation_parts)

        header = payload.get("contextual_header") or ""
        text = payload.get("text") or c.get("text", "")
        mismatch_tag = (
            "[FUENTE DISTINTA A LA SOLICITADA] "
            if document_reference is not None and c.get("source_match") is False
            else ""
        )
        block = f"{mismatch_tag}[Fuente: {citation}]\n{(header + chr(10)) if header else ''}{text}"

        if payload.get("source_type") == "bibliografia":
            biblio_blocks.append(block)
        else:
            curso_blocks.append(block)

    parts = []
    if document_reference is not None:
        if not requested_source_found and course_map_match:
            # Fix 1.6: el nombre pedido NO coincide con ningún archivo
            # indexado en Qdrant, pero SÍ con un recurso real del aula
            # (Course Map). Causa real: el nombre del RECURSO en Moodle suele
            # ser distinto del nombre del ARCHIVO vectorizado. Se le da al
            # modelo el nombre y enlace reales para que responda con
            # precisión, sin afirmar que "no existe" ni presentar otro
            # documento como si fuera el pedido.
            parts.append(
                f"[VERIFICACIÓN DE FUENTE SOLICITADA] El estudiante pidió específicamente: "
                f"\"{document_reference}\". No hay ningún documento indexado con ese nombre exacto, PERO en "
                f"el aula virtual SÍ existe el recurso \"{course_map_match.get('name')}\" (enlace real: "
                f"{course_map_match.get('url')}). Respondé con precisión: ese recurso existe en el aula "
                f"(dale el enlace real), pero no pudiste confirmar que corresponda exactamente al documento "
                f"indexado (el nombre del archivo puede ser distinto) — sugerile verificarlo con el enlace. "
                f"NUNCA respondas que no existe, y nunca presentes el contenido de otro documento como si "
                f"fuera ese recurso sin aclarar que es un documento distinto."
            )
        else:
            estado = "SÍ" if requested_source_found else "NO"
            parts.append(
                f"[VERIFICACIÓN DE FUENTE SOLICITADA] El estudiante pidió específicamente: "
                f"\"{document_reference}\". ¿Se encontró un documento real con ese nombre entre "
                f"las fuentes recuperadas? {estado}."
            )
    if curso_blocks:
        parts.append("[MATERIAL DEL CURSO]\n" + "\n\n".join(curso_blocks))
    if biblio_blocks:
        parts.append("[BIBLIOGRAFÍA COMPLEMENTARIA]\n" + "\n\n".join(biblio_blocks))
    context_block = "\n\n".join(parts) if parts else "(sin fragmentos recuperados)"

    return f"{context_block}\n\nPregunta del estudiante: {query}"


def _build_structured_evidence_candidate(structured: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Convierte el resultado de campos_estructurados.structured_lookup()
    en un candidato con la MISMA forma que produce hybrid_retrieve() (para
    que build_generation_prompt()/apply_source_boost() lo traten igual que
    cualquier otro), o None si no hay evidencia estructurada utilizable
    (status 'insufficient'/'not_found' -> el RAG normal sigue intacto).

    'ambiguous': NO se elige un valor — se arma un bloque que muestra
    explícitamente los valores distintos encontrados y su fuente, para que
    el LLM explique la discrepancia o se abstenga, nunca que adivine cuál
    es el correcto (ver informe, sección "resolución de conflictos")."""
    if structured is None:
        return None
    status = structured.get("status")

    if status == "confident":
        ev = structured["evidence"][0]
        field_raw = ev.get("field_raw") or structured.get("field", "")
        text = f"{field_raw}: {ev['value_raw']}" if field_raw else str(ev["value_raw"])
        filename, page, section = ev["document_filename"], ev.get("page"), ev.get("section_heading")
        source_type = ev.get("source_type") or "curso_propio"
    elif status == "confident_row":
        ev_list = structured["evidence"]
        first = ev_list[0]
        text = " | ".join(f"{e['field_raw']}: {e['value_raw']}" for e in ev_list if e.get("value_raw"))
        filename, page, section = first["document_filename"], first.get("page"), first.get("section_heading")
        source_type = first.get("source_type") or "curso_propio"
    elif status == "ambiguous":
        lines = ["[ADVERTENCIA: valores DISTINTOS encontrados para este dato en documentos diferentes del curso — no elijas uno, explicá la discrepancia o pedí precisión]"]
        for cand in structured["candidates"]:
            ev = cand["evidence"][0]
            lines.append(f"- En {ev['document_filename']}: {cand['value']}")
        text = "\n".join(lines)
        filename, page, section = "(múltiples documentos, ver detalle abajo)", None, None
        source_type = "curso_propio"
    else:  # "insufficient" / "not_found"
        return None

    return {
        "payload": {
            "filename": filename,
            "source_type": source_type,
            "section_name": section,
            "chunk": None,
            "slide_number": None,
            "start_time": None,
            "text": text,
            "contextual_header": "[Evidencia estructurada — dato extraído directamente de una tabla del curso]",
        },
        "score": 1.0,
        "rerank_score": 1.0,
        "boosted_score": 10_000.0,  # por encima de cualquier boost normal — es evidencia exacta, no una similitud
    }


async def answer_query(
    *,
    collection_name: str,
    query: str,
    dense_strategy_name: Optional[str] = None,
    sparse_strategy_name: Optional[str] = None,
    rerank_strategy_name: Optional[str] = None,
    generation_strategy_name: Optional[str] = None,
    top_n: Optional[int] = None,
    extra_system_prompt: Optional[str] = None,
    history: Optional[List[Dict[str, str]]] = None,
    course_id: Optional[int] = None,
    user_id: Optional[int] = None,
) -> Dict[str, Any]:
    """Pipeline completo: retrieve_rerank_boost() [Qdrant hybrid + rerank +
    boosts] + campos_estructurados.structured_lookup() [evidencia de tabla
    de alta precisión, si la colección tiene índice] -> evidencia
    consolidada -> build_generation_prompt() -> Evidence/Answerability Gate
    (filtro MIN_SOURCE_RELEVANCE + abstención) -> generation_strategy.generate()
    -> validación de grounding/fuente. `extra_system_prompt` se AGREGA al
    prompt fijo, nunca lo reemplaza — así la regla de prioridad
    curso_propio/bibliografía siempre se respeta. `history`: turnos previos
    de la conversación (ver assistants.get_recent_history), para que el
    tutor recuerde lo ya preguntado en la misma sesión.

    El índice de campos estructurados es una fuente de evidencia adicional,
    no un asistente aparte: se consulta siempre (si la colección no tiene
    índice poblado, structured_lookup() devuelve None sin error y el
    pipeline sigue igual que antes de que existiera)."""
    t_total = time.perf_counter()
    timings: Dict[str, float] = {}
    candidates = await retrieve_rerank_boost(
        collection_name=collection_name,
        query=query,
        dense_strategy_name=dense_strategy_name,
        sparse_strategy_name=sparse_strategy_name,
        rerank_strategy_name=rerank_strategy_name,
        top_n=top_n,
        timings=timings,
    )
    # Se filtra ACÁ (antes de armar el prompt), no solo al construir
    # `sources` más abajo — si no, el LLM seguía viendo en su contexto
    # candidatos irrelevantes aunque la UI ya no los mostrara como fuentes,
    # y a veces respondía con eso en vez de admitir que no tiene material
    # para esa pregunta. Con la lista vacía, build_generation_prompt() cae
    # en "(sin fragmentos recuperados)" y el system prompt le indica qué
    # hacer en ese caso.
    candidates = [c for c in candidates if c.get("rerank_score", 1.0) >= MIN_SOURCE_RELEVANCE]

    # Índice de campos estructurados: si la pregunta apunta a un campo real
    # de esta colección (descubierto de los datos, no de una lista fija —
    # ver campos_estructurados.py), se inyecta como evidencia de alta
    # confianza en el MISMO pool que compite/se cita junto con lo
    # recuperado por Qdrant — no es una respuesta aparte. Ambiguo -> se
    # preserva la contradicción explícitamente, nunca se elige un valor por
    # mayoría. Insuficiente/no encontrado/colección sin índice -> no se
    # toca nada, sigue el RAG normal sin este paso (siempre seguro: si la
    # colección no tiene índice poblado en Postgres, structured_lookup()
    # devuelve None de inmediato, sin excepción).
    t0 = time.perf_counter()
    try:
        structured = await campos_estructurados.structured_lookup(
            collection_name, query, dense_strategy_name=dense_strategy_name)
    except Exception as e:
        print(f"⚠️ structured_lookup falló (se ignora, sigue el RAG normal): {e}")
        structured = None
    timings["structured_lookup_ms"] = (time.perf_counter() - t0) * 1000
    synthetic = _build_structured_evidence_candidate(structured)
    if synthetic is not None:
        candidates.insert(0, synthetic)

    # Query Router (Incremento 1 del agente híbrido, ver plan de esta
    # sesión) — reglas deterministas (regex) para preguntas de conteo/
    # rango unidad-semana (Structured Query Engine) y de navegación
    # (Course Map / Moodle real). Nunca usa el LLM para decidir la
    # respuesta en sí, solo para fraseársela al estudiante. Igual que
    # structured_lookup arriba: None = no aplica, sigue el RAG normal sin
    # ningún cambio de comportamiento.
    t0 = time.perf_counter()
    try:
        routed = query_router.route(collection_name, course_id, query, user_id=user_id)
    except Exception as e:
        print(f"⚠️ query_router falló (se ignora, sigue el RAG normal): {e}")
        routed = None
    timings["query_router_ms"] = (time.perf_counter() - t0) * 1000
    if routed is not None:
        candidates.insert(0, routed)

    # Si el estudiante nombró una fuente puntual (reglamento/manual/sílabo/
    # archivo/...), se verifica por metadata real (filename) cuáles
    # candidatos son efectivamente esa fuente — ver annotate_source_match().
    document_reference = _extract_document_reference(query)
    requested_source_found = annotate_source_match(candidates, document_reference)
    # Fix 1.6: si la fuente pedida no está indexada, ver si igual existe en
    # el aula (Course Map) para responder "existe pero no está disponible".
    course_map_match = (
        _find_course_map_resource(course_id, document_reference)
        if (document_reference is not None and not requested_source_found)
        else None
    )

    user_prompt = build_generation_prompt(
        query, candidates, document_reference, requested_source_found, course_map_match
    )
    system_prompt = SYSTEM_PROMPT_TEMPLATE
    if extra_system_prompt:
        system_prompt = f"{SYSTEM_PROMPT_TEMPLATE}\n\nContexto adicional de este asistente:\n{extra_system_prompt}"
    generator = strategy_registry.get_generation_strategy(generation_strategy_name)
    t0 = time.perf_counter()
    answer, tokens_consumidos = await generator.generate(system_prompt, user_prompt, history=history)
    timings["generation_ms"] = (time.perf_counter() - t0) * 1000
    timings["total_ms"] = (time.perf_counter() - t_total) * 1000
    print(
        "⏱️  answer_query: "
        + ", ".join(f"{k}={v:.0f}ms" for k, v in timings.items())
    )

    # `candidates` ya viene filtrado por relevancia (arriba) — refleja
    # exactamente lo que el LLM tuvo disponible para responder.
    sources = [
        {
            "filename": c["payload"].get("filename"),
            "source_type": c["payload"].get("source_type"),
            "section_name": c["payload"].get("section_name"),
            "chunk": c["payload"].get("chunk"),
            "slide_number": c["payload"].get("slide_number"),
            "start_time": c["payload"].get("start_time"),
            "score": c.get("boosted_score", c.get("rerank_score", c.get("score"))),
            "text": c["payload"].get("text"),
        }
        for c in candidates
    ]

    return {
        "answer": answer,
        "sources": sources,
        "tokens_consumidos": tokens_consumidos,
        "config_used": {
            "dense": strategy_registry.resolve_strategy_name("dense", dense_strategy_name),
            "sparse": strategy_registry.resolve_strategy_name("sparse", sparse_strategy_name),
            "rerank": strategy_registry.resolve_strategy_name("rerank", rerank_strategy_name),
            "generation": strategy_registry.resolve_strategy_name(
                "generation", generation_strategy_name
            ),
            "timings_ms": {k: round(v) for k, v in timings.items()},
        },
    }
