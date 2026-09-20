"""
query_router.py
================
Query Router — Incremento 1 del agente académico híbrido (ver plan de
arquitectura de esta sesión). Reglas deterministas (regex) PRIMERO, nunca
LLM, para las preguntas que una fuente determinista puede resolver sin
ambigüedad: conteos/rangos de unidad-semana (Structured Query Engine,
campos_estructurados.py) y navegación (Course Map, moodle_tools.py). Todo
lo que no matchea una regla determinista sigue cayendo a RAG exactamente
como hoy — este router NUNCA puede empeorar una respuesta, solo a veces
evitar RAG a favor de una respuesta verificable.

Mismo patrón de resolución que strategies/registry.py: intentar reglas
deterministas primero, degradar a "no aplica" (None) si ninguna matchea —
sin fallback semántico por embeddings todavía (eso queda para cuando el
Incremento 1 esté validado con datos reales; agregar esa capa ahora sin
evidencia de que hace falta sería sobre-ingeniería)."""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

import campos_estructurados as ce
import course_map as cm
import moodle_calendar as mc
import moodle_tools as mt

_UNIT_RE = re.compile(r"unidad\s+([ivx\d]+)", re.IGNORECASE)
_TOPIC_WORDS_RE = re.compile(r"\b(tema|temas|trata|contenido)\b", re.IGNORECASE)
_COUNT_WEEKS_RE = re.compile(r"cu[aá]nt[oa]s?\s+(semanas?|sesiones?)\b", re.IGNORECASE)

_STRUCTURED_LAST_RE = re.compile(
    r"([uú]ltima|final)\s+semana\b.*\bunidad\s+([ivx\d]+)", re.IGNORECASE,
)
_STRUCTURED_FIRST_RE = re.compile(
    r"(primera|inicial)\s+semana\b.*\bunidad\s+([ivx\d]+)", re.IGNORECASE,
)
# "¿cuántas semanas tiene el curso [y de qué trata cada semana]?" — sin
# "unidad", es sobre el curso completo. "cuánto dura"/"duración" son
# sinónimos reales de la misma pregunta — bug real encontrado: "¿cuánto
# dura el curso?" no matcheaba nada de esto, caía a RAG y alucinó "15
# semanas" (el real, verificado contra Moodle, es 16).
_STRUCTURED_COUNT_TOTAL_RE = re.compile(
    r"cu[aá]nt[oa]s?\s+(semanas?|sesiones?)\s+tiene\s+(el\s+)?curso"
    r"|cu[aá]nto\s+dura\s+(el\s+)?curso|duraci[oó]n\s+del\s+curso",
    re.IGNORECASE,
)

# Trigger verbos/pedidos de navegación — "quiero el/los/la/las enlace(s)/
# link(s)/ruta/url" y "pásame" son sinónimos reales de "envíame"/"dame"
# que faltaban (bug real: "quiero los enlaces de cada material" y "quiero
# las url" no matcheaban nada de esto).
_NAVIGATION_RE = re.compile(
    r"\b(d[oó]nde (est[aá]|queda|encuentro)|enlace de|link de|c[oó]mo llego a"
    r"|env[ií]ame|env[ií]a|dame|pasa(me)?|necesito|quiero ver"
    r"|quiero\s+(el|los|la|las)\s+(enlace|link|ruta|url)s?)\b", re.IGNORECASE,
)
_ORDINAL_ACTIVITY_RE = re.compile(
    r"\b(primera|primero|[uú]ltima|[uú]ltimo)\b.*"
    r"\b(tarea|cuestionario|examen|actividad|material|recurso|foro|video)\b",
    re.IGNORECASE,
)

# "de" opcional: cubre tanto "fecha de inicio" como "fecha inicio"
# (fraseo informal real, visto en pruebas).
_COURSE_DATE_RE = re.compile(
    r"\b(cu[aá]ndo\s+(inici[oó]|empieza|comienza)|fecha\s+(de\s+)?inicio)\b.*\bcurso\b"
    r"|\bcurso\b.*\b(cu[aá]ndo\s+(inici[oó]|empieza|comienza)|fecha\s+(de\s+)?inicio)\b",
    re.IGNORECASE,
)
_SCHEDULE_RE = re.compile(
    r"\bhorario\b|\ba\s+qu[eé]\s+hora\b|\bd[ií]a\s+y\s+hora\b", re.IGNORECASE,
)
# "¿en qué semana estamos?" / "esta semana" / "semana actual" — sin
# número ni "unidad" explícitos, pregunta por AHORA (fecha real, no una
# unidad puntual) — visto con datos reales: sin esto, cae a RAG y
# alucina una semana plausible pero falsa.
_CURRENT_WEEK_RE = re.compile(
    r"\b(en\s+qu[eé]\s+semana\s+(estamos|vamos|va)|semana\s+actual|esta\s+semana)\b",
    re.IGNORECASE,
)
# "¿qué me falta?" / "¿qué tengo pendiente?" / "actividades pendientes" —
# progreso REAL del estudiante (Incremento 2, ya desbloqueado — ver
# moodle_tools.MOODLE_COMPLETION_ENABLED). La última alternativa es
# deliberadamente amplia (qué/cuáles ... pendiente, sin exigir adyacencia)
# — bug real encontrado: "qué TAREAS tengo pendiente" no matcheaba la
# frase rígida "qué tengo pendiente" y caía a NAVIGATION (por "enviame"),
# que no cruza con completion real y terminaba contradiciendo una
# respuesta de PROGRESS anterior en la misma conversación.
_PROGRESS_RE = re.compile(
    r"\b(qu[eé]\s+me\s+falta|qu[eé]\s+tengo\s+pendiente|actividades?\s+pendientes?"
    r"|tareas?\s+pendientes?|pendientes?\s+(de|por)\s+(hacer|entregar|completar)"
    r"|me\s+falta\s+(hacer|completar)|qu[eé]\s+falta\s+por\s+hacer"
    r"|(qu[eé]|cu[aá]les?)\b.*\bpendiente(s)?\b"
    # "cuántas actividades me falta" — sin la palabra "pendiente" y sin
    # empezar con "qué" — bug real encontrado sin esto.
    r"|cu[aá]nt[oa]s?\b.*\bfalta\b)",
    re.IGNORECASE,
)
# "semana N" dentro de una pregunta de PROGRESS — mismo patrón ya usado en
# moodle_tools.find_activity() para navegación, reutilizado acá para poder
# filtrar pendientes/completadas a una sección puntual del curso.
_WEEK_NUMBER_RE = re.compile(r"semana\s*(\d+)", re.IGNORECASE)
# "¿cómo van mis calificaciones?" / "¿cuánto tengo de nota?" / "quiero
# saber mi puntaje" — pide un dato PERSONAL real (la nota propia del
# estudiante), no los criterios de evaluación del sílabo. Diseñada como
# DOS conjuntos de palabras (mismo patrón que _SECTION_CONTENT_WORDS_RE)
# combinados con lookahead en vez de una lista de frases exactas — bug
# real encontrado: enumerar frases puntuales ("cómo van", "mi nota") deja
# afuera cualquier sinónimo o forma nueva de preguntar (ej. "cuáles son
# mis calificaciones", "notas de mi curso", "nota de mi última tarea",
# "puntaje que tengo" no matcheaban nada). Dispara si la pregunta tiene
# una palabra de nota/calificación Y, en cualquier parte, una señal de que
# es personal o un pedido — así cubre cualquier combinación razonable de
# ambas listas sin tener que volver a tocar la regla por cada fraseo
# nuevo.
_GRADES_RE = re.compile(
    r"(?=.*\b(calificaci[oó]n(es)?|notas?|puntaj(e|es)|puntuaci[oó]n(es)?|resultados?)\b)"
    r"(?=.*\b(mis?\b|de\s+mi\b|c[oó]mo\s+(voy|estoy|vengo|van)\b|qu[eé]\s+tal\s+voy\b"
    r"|cu[aá]les?\s+son\b|cu[aá]l\s+es\b|env[ií]a(me)?\b|dame\b|mu[eé]strame\b|pasa(me)?\b|dime\b"
    r"|quiero\s+(ver|saber)\b|necesito\b|tengo\b))",
    re.IGNORECASE,
)
# Palabras genéricas de "material/recurso del curso" — no ligadas a ningún
# curso puntual, vocabulario natural de cualquier estudiante. Se usan para
# (a) ampliar CURRENT_WEEK a preguntas de "esta semana" sin decir la frase
# exacta, y (b) como último recurso de NAVIGATION cuando no hay match
# preciso de una actividad puntual (ver _format_navigation_evidence).
_SECTION_CONTENT_WORDS_RE = re.compile(
    r"\b(materiales?|recursos?|documentos?|enlaces?|videos?|art[ií]culos?|lecturas?|contenidos?"
    r"|urls?|links?|rutas?)\b",
    re.IGNORECASE,
)
# "¿cómo apruebo el curso?" / "¿qué evidencias evaluables tiene?" — pide el
# cuadro REAL del sílabo (evidencia evaluable / instrumento / ponderación),
# que YA está indexado pero no se consultaba (ver A.1 del plan). Antes de
# esto solo había una instrucción de prompt para priorizar la nota mínima
# "si el LLM la encontraba" — ahora el dato es determinista (reemplaza esa
# instrucción, retirada de strategies/generation.py).
_EVALUATION_CRITERIA_RE = re.compile(
    r"c[oó]mo\s+(apruebo|se\s+aprueba|se\s+eval[uú]a|se\s+califica)\b"
    r"|qu[eé]\s+(necesito|se\s+necesita)\s+para\s+aprobar"
    r"|evidencias?\s+evaluables?"
    r"|instrumentos?\s+de\s+evaluaci[oó]n"
    r"|ponderaci[oó]n\b"
    r"|criterios?\s+de\s+evaluaci[oó]n\s+del\s+curso"
    r"|c[oó]mo\s+se\s+eval[uú]a\s+el\s+curso",
    re.IGNORECASE,
)
# "quiero estudiar para la semana X" / "dame preguntas de práctica" /
# "cómo me preparo" / "repasar para la evaluación" — genera preguntas de
# práctica atadas a la evidencia evaluable y los criterios REALES de esa
# semana (ver A.2 del plan), no reflexión filosófica genérica.
_STUDY_HELP_RE = re.compile(
    r"quiero\s+estudiar|ay[uú]dame\s+a\s+estudiar|preguntas?\s+de\s+pr[aá]ctica"
    r"|c[oó]mo\s+me\s+preparo|repasar\s+(para\s+)?(la\s+)?(evaluaci[oó]n|examen)"
    r"|(prepararme|preparar(me)?)\s+para\s+(la\s+)?(evaluaci[oó]n|examen)"
    r"|estudiar\s+para\s+(la\s+)?(semana|evaluaci[oó]n|examen)",
    re.IGNORECASE,
)
# "¿cuándo vence...?" / "¿qué se me vence?" / "fecha límite de..." — fechas
# REALES de entrega (mdl_event) vía moodle_calendar.py (Parte C del plan).
# Solo viable con acceso a la DB de Moodle (preprod) o con el webservice de
# calendario autorizado; si no, degrada honestamente.
_DUE_DATES_RE = re.compile(
    r"cu[aá]ndo\s+(vence|se\s+entrega|es\s+la\s+entrega|hay\s+que\s+entregar)"
    r"|qu[eé]\s+(se\s+)?(me\s+)?vence"
    r"|fecha\s+l[ií]mite"
    r"|cu[aá]l\s+es\s+la\s+fecha\s+(de\s+)?(entrega|vencimiento)"
    r"|fechas?\s+de\s+(entrega|vencimiento)",
    re.IGNORECASE,
)


def _classify(query: str) -> Optional[Dict[str, Any]]:
    """Devuelve {"intent": ...} o None si ninguna regla determinista
    matchea (el caller sigue con RAG)."""
    m = _STRUCTURED_LAST_RE.search(query)
    if m:
        return {"intent": "STRUCTURED", "operation": "range", "entity": "unidad", "value": m.group(2), "want": "last"}

    m = _STRUCTURED_FIRST_RE.search(query)
    if m:
        return {"intent": "STRUCTURED", "operation": "range", "entity": "unidad", "value": m.group(2), "want": "first"}

    # Regla unificada de unidad: dispara con "cuántas semanas tiene la
    # unidad X", "cuáles son los temas de la unidad X", o ambas juntas —
    # sin importar el orden en que aparezcan en la pregunta (búsqueda
    # independiente de orden, no una sola frase fija). Encontrado con
    # datos reales: exigir la frase exacta "cuántas...tiene..." dejaba
    # afuera preguntas que solo piden los temas, sin "cuántas".
    unit_m = _UNIT_RE.search(query)
    if unit_m:
        wants_count = bool(_COUNT_WEEKS_RE.search(query))
        wants_topics = bool(_TOPIC_WORDS_RE.search(query))
        if wants_count or wants_topics:
            return {
                "intent": "STRUCTURED", "operation": "count", "entity": "unidad",
                "value": unit_m.group(1), "with_topics": wants_topics,
            }

    m = _STRUCTURED_COUNT_TOTAL_RE.search(query)
    if m:
        return {"intent": "STRUCTURED", "operation": "count", "entity": None, "value": None,
                "with_topics": bool(_TOPIC_WORDS_RE.search(query))}

    # Antes de CURRENT_WEEK: "¿qué se me vence esta semana?" contiene "esta
    # semana" (que dispararía CURRENT_WEEK) pero en realidad pregunta por
    # fechas de entrega — ver _DUE_DATES_RE.
    if _DUE_DATES_RE.search(query):
        return {"intent": "DUE_DATES"}

    if _CURRENT_WEEK_RE.search(query):
        return {"intent": "CURRENT_WEEK"}

    if _PROGRESS_RE.search(query):
        week_m = _WEEK_NUMBER_RE.search(query)
        return {"intent": "PROGRESS", "section_number": int(week_m.group(1)) if week_m else None}

    if _GRADES_RE.search(query):
        return {"intent": "GRADES"}

    if _EVALUATION_CRITERIA_RE.search(query):
        return {"intent": "EVALUATION_CRITERIA"}

    if _STUDY_HELP_RE.search(query):
        week_m = _WEEK_NUMBER_RE.search(query)
        return {"intent": "STUDY_HELP", "section_number": int(week_m.group(1)) if week_m else None}

    if _SCHEDULE_RE.search(query):
        return {"intent": "SCHEDULE"}

    if _COURSE_DATE_RE.search(query):
        return {"intent": "COURSE_DATE"}

    if _ORDINAL_ACTIVITY_RE.search(query):
        return {"intent": "NAVIGATION", "ordinal": True}

    if _NAVIGATION_RE.search(query):
        return {"intent": "NAVIGATION", "ordinal": False}

    return None


def _format_structured_evidence(parsed: Dict[str, Any], result: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Convierte un resultado de campos_estructurados.structured_query()
    en un candidato de evidencia, mismo formato que
    rag_pipeline._build_structured_evidence_candidate() ya usa para el
    índice de campo simple — boosted_score alto para que domine el
    ranking igual que la evidencia estructurada existente. Frasea el
    conteo como "semanas encontradas en el índice", NUNCA como el total
    absoluto — la evidencia solo puede afirmar lo que efectivamente está
    indexado, ver docstring de count_distinct() en campos_estructurados.py."""
    if result.get("status") != "confident":
        return None

    docs = ", ".join(result.get("evidence_documents", []))
    unit_label = parsed["value"].upper() if parsed.get("value") else None

    if parsed["operation"] == "count":
        scope = f"la Unidad {unit_label}" if unit_label else "el curso"
        count = result["count"]
        # El conteo se repite explícito y las semanas van una por línea
        # (no en un párrafo denso separado por comas/punto y coma) —
        # encontrado con datos reales: un párrafo largo invita al LLM a
        # "recontar" al parafrasear y a veces se equivoca (ej. dijo "15"
        # cuando la evidencia decía "13" y listaba exactamente 13): darle
        # el número ya contado, repetido, y una lista fácil de copiar
        # tal cual reduce el margen para que invente un número distinto.
        lines = [f"EL NÚMERO EXACTO DE SEMANAS ENCONTRADAS ES: {count}. NO RECALCULES NI RECUENTES — usa este número tal cual."]
        lines.append(f"Semanas encontradas con material indexado para {scope} ({count} en total):")
        for v in result["values"]:
            week_label = v.split(" / ")[0].strip()
            topic = (result.get("topics") or {}).get(v)
            lines.append(f"- {week_label}" + (f": {topic}" if topic else ""))
        lines.append(
            f"(Nota: son {count} semanas — este número refleja lo efectivamente indexado en el sistema, "
            "no necesariamente el total oficial si falta material por vectorizar. Al responder, repetí "
            f"el número {count} tal cual aparece acá, sin volver a contar la lista vos mismo.)"
        )
        text = "\n".join(lines)
        section_label = f"Structured Query Engine — Unidad {unit_label}" if unit_label else "Structured Query Engine — Curso completo"
    else:
        which = result.get(parsed.get("want", "last"), "")
        text = (
            f"Según los documentos indexados, la semana {'última' if parsed.get('want')=='last' else 'primera'} "
            f"con material encontrado para la Unidad {unit_label} es: {which}. "
            f"(Nota: refleja lo efectivamente indexado, no necesariamente el límite oficial de la unidad.)"
        )
        section_label = f"Structured Query Engine — Unidad {unit_label}"

    return _wrap_as_candidate(
        text=text,
        filename=docs or "Índice de Campos Estructurados",
        section_name=section_label,
    )


# Piso de confianza genérico para un match léxico de find_activity(): por
# debajo de esto, más de dos tercios de las palabras de la pregunta NO
# aparecen en el nombre de la actividad/sección encontrada — presentarlo
# como "la" respuesta sería afirmar con seguridad algo débilmente
# respaldado (bug real: un ítem del gradebook sin actividad real detrás
# —itemtype "manual"— devolvió una carpeta distinta como si fuera "la"
# tarea pedida). Una coincidencia exacta de "semana N" ya suma +1.0 en
# find_activity(), así que nunca queda bloqueada por este piso.
_NAVIGATION_MIN_SCORE = 0.4


def _format_navigation_evidence(course_id: int, query: str) -> Optional[Dict[str, Any]]:
    matches = mt.find_activity(course_id, query)
    if matches and matches[0]["score"] >= _NAVIGATION_MIN_SCORE:
        top = matches[0]
        others = matches[1:3]
        text = f"La actividad \"{top['name']}\" está en {top['section_name']} — enlace real: {top['url']}"
        if others:
            text += ". También relacionado: " + "; ".join(
                f"\"{o['name']}\" ({o['url']})" for o in others
            )
        return _wrap_as_candidate(
            text=text,
            filename="Estructura real del curso (Moodle)",
            section_name=top["section_name"],
        )

    # Sin match confiable de UNA actividad puntual: si la pregunta es sobre
    # materiales/recursos genéricos (no un nombre concreto de documento),
    # devolver el contenido REAL de la semana correspondiente (la que se
    # menciona explícitamente, o si no, la semana actual por fecha) es más
    # útil y honesto que no decir nada — bug real encontrado: "envíame los
    # enlaces de los materiales a revisar" caía acá sin match y terminaba
    # en RAG diciendo "no tengo acceso a los enlaces", falso.
    if not _SECTION_CONTENT_WORDS_RE.search(query):
        return None
    week_m = _WEEK_NUMBER_RE.search(query)
    section_number = int(week_m.group(1)) if week_m else None
    if section_number is None:
        current = cm.get_current_week_number(course_id)
        if current.get("status") == "confident":
            section_number = current["week_number"]
    if section_number is None:
        return None
    return _format_section_contents_evidence(course_id, section_number)


def _format_course_date_evidence(course_id: int) -> Optional[Dict[str, Any]]:
    result = cm.get_course_dates(course_id)
    if result.get("status") != "confident":
        return None
    text = f"El curso inicia el {result['startdate']}"
    if result.get("enddate"):
        text += f" y finaliza el {result['enddate']}"
    text += " (fecha real de Moodle)."
    return _wrap_as_candidate(text=text, filename="Estructura real del curso (Moodle)", section_name="Fechas del curso")


def get_current_week_summary(collection_name: str, course_id: int) -> Optional[Dict[str, Any]]:
    """Datos puros (sin envolver como candidato RAG) de en qué semana real
    está el curso hoy — calculado desde la fecha real de inicio (Moodle) +
    la fecha de hoy, nunca adivinado. Reusado por el Query Router
    (`_format_current_week_evidence`) y por el saludo inicial del chat
    (`app.py`, vía `assistants.compose_saludo`) para que el saludo salude
    con contexto real de dónde va el curso, en vez de un genérico. Devuelve
    None si el curso no empezó o no se pudo determinar (el saludo cae al
    genérico sin contexto de semana en ese caso, nunca inventa una)."""
    current = cm.get_current_week_number(course_id)
    if current.get("status") != "confident":
        return None
    week_n = current["week_number"]
    result = ce.count_distinct(collection_name, "semana / sesion", extra_field="tema", course_id=course_id)
    topic = None
    if result.get("status") == "confident":
        match = next((v for v in result["values"] if v.split(" / ")[0].strip() == f"semana {week_n}"), None)
        if match:
            topic = (result.get("topics") or {}).get(match)
        elif week_n > len(result["values"]):
            return {"week_number": week_n, "topic": None, "beyond_last_week": True}
    return {"week_number": week_n, "topic": topic, "beyond_last_week": False}


_SECTION_MODNAME_ES = {
    "resource": "Documento", "folder": "Carpeta", "url": "Enlace/Video",
    "assign": "Tarea", "quiz": "Cuestionario", "forum": "Foro", "label": "Texto", "page": "Página",
}


def _format_section_contents_evidence(course_id: int, section_number: int) -> Optional[Dict[str, Any]]:
    """Lista REAL y COMPLETA de los recursos/actividades de una sección de
    Moodle (Course Map) — usada para responder honestamente preguntas tipo
    "¿hay videos/artículos/una evaluación oral esta semana?": bug real
    encontrado sin esto: esas preguntas caían a RAG, que a veces mezclaba
    contenido de OTRA semana (ej. mencionó un video de la Semana 13 al
    responder sobre la Semana 3) en vez de decir honestamente qué hay (o no
    hay) en la semana real preguntada."""
    course = cm.get_course_map(course_id)
    section = next((s for s in course.sections if s.section_number == section_number), None)
    if section is None:
        return None
    if not section.activities:
        text = f"En \"{section.name}\" no hay ningún recurso o actividad registrado en Moodle."
        return _wrap_as_candidate(text=text, filename="Estructura real del curso (Moodle)", section_name=section.name)
    rows = [[a.name, _SECTION_MODNAME_ES.get(a.modname, a.modname), a.url] for a in section.activities]
    lines = [
        f"Contenido REAL y COMPLETO de \"{section.name}\" según Moodle:",
        _md_table(["Nombre", "Tipo", "Enlace"], rows),
        "IMPORTANTE: esta lista es completa y real — si el estudiante pregunta por un tipo de recurso "
        "puntual (video, artículo, evaluación, lectura, etc.) que NO aparece en esta tabla, decile "
        "honestamente que no hay ese tipo de recurso en esta sección, sin tomar un ejemplo de otra "
        "semana ni inventar uno. Mantené el formato de tabla en tu respuesta.",
    ]
    text = "\n".join(lines)
    return _wrap_as_candidate(text=text, filename="Estructura real del curso (Moodle)", section_name=section.name)


def _format_current_week_evidence(collection_name: str, course_id: int) -> Optional[Dict[str, Any]]:
    """"¿En qué semana estamos?" — calculado desde la fecha real de inicio
    del curso (Moodle) + la fecha de hoy, NUNCA adivinado por RAG (bug
    real encontrado: sin esto, el LLM alucinaba una semana plausible pero
    falsa a partir de cualquier chunk que mencionara 'semana' con buen
    score). También incluye el contenido real completo de esa semana (ver
    _format_section_contents_evidence) para responder preguntas sobre qué
    hay (o no hay) esta semana — video, artículo, evaluación, etc."""
    current = cm.get_current_week_number(course_id)
    status = current.get("status")
    if status == "not_found":
        return None
    if status == "not_started":
        text = f"El curso todavía no empezó — inicia el {current['startdate']} (fecha real de Moodle)."
        return _wrap_as_candidate(text=text, filename="Estructura real del curso (Moodle)", section_name="Semana actual")

    summary = get_current_week_summary(collection_name, course_id)
    week_n = summary["week_number"] if summary else current["week_number"]
    if summary and summary["beyond_last_week"]:
        result = ce.count_distinct(collection_name, "semana / sesion", course_id=course_id)
        total = result.get("count", "?") if result.get("status") == "confident" else "?"
        text = f"Según la fecha de inicio real del curso, ya deberíamos estar más allá de la última semana indexada (semana {week_n} calculada, curso de {total} semanas)."
        return _wrap_as_candidate(text=text, filename="Estructura real del curso (Moodle)", section_name="Semana actual")

    text = f"Según la fecha de inicio real del curso, hoy corresponde a la Semana {week_n}."
    if summary and summary["topic"]:
        text += f" Tema: {summary['topic']}."

    section_evidence = _format_section_contents_evidence(course_id, week_n)
    if section_evidence:
        text += "\n\n" + section_evidence["payload"]["text"]

    return _wrap_as_candidate(text=text, filename="Estructura real del curso (Moodle)", section_name=f"Semana actual — Semana {week_n}")


def _md_table(headers: List[str], rows: List[List[str]]) -> str:
    """Tabla Markdown GFM real (no viñetas) — el frontend del chat
    (asistente_chat.html::mdToHtml) ya sabe renderizar este formato, y el
    modelo tiende a preservar la forma en que le llega la evidencia (ya
    visto esta sesión con listas 'una por línea'). Usarla para cualquier
    lista de más de 2-3 filas se ve mucho más ordenado que viñetas
    sueltas (pedido explícito del usuario)."""
    sep = ["---"] * len(headers)
    lines = ["| " + " | ".join(headers) + " |", "| " + " | ".join(sep) + " |"]
    for row in rows:
        lines.append("| " + " | ".join(str(c) if c else "-" for c in row) + " |")
    return "\n".join(lines)


def _format_progress_evidence(
    course_id: int, user_id: int, section_number: Optional[int] = None,
) -> Optional[Dict[str, Any]]:
    """"¿Qué me falta?" — progreso REAL del estudiante autenticado
    (moodle_userid de su propia sesión, nunca de otro), cruzando
    completion real (Moodle) con nombres/urls reales (Course Map).
    Nunca inventa una actividad — si Moodle no reporta completion para
    nada en este curso, lo dice. `section_number` (opcional, ej. "en la
    semana 3 qué me falta"): filtra pendientes/completadas a esa sección
    real — bug real encontrado sin esto: una pregunta con "semana N" no
    cruzaba con completion y caía a NAVIGATION, que llegó a listar una
    tarea YA completada como si faltara hacerla."""
    result = mt.get_pending_activities(course_id, user_id)
    if result.get("_simulated"):
        # No debería pasar con MOODLE_COMPLETION_ENABLED=True, pero si
        # Moodle vuelve a bloquear la función más adelante, esto sigue
        # siendo honesto en vez de inventar una lista.
        return None

    course = cm.get_course_map(course_id)
    by_cmid = {a.cmid: (a.name, s.name, s.section_number) for s in course.sections for a in s.activities}

    pending = result.get("pending_candidates", [])
    completed = result.get("completed_activities", [])

    scope_label = None
    if section_number is not None:
        pending = [p for p in pending if by_cmid.get(p["cmid"], (None, None, None))[2] == section_number]
        completed = [c for c in completed if by_cmid.get(c, (None, None, None))[2] == section_number]
        section = next((s for s in course.sections if s.section_number == section_number), None)
        scope_label = section.name if section else f"Semana {section_number}"
        if section is None:
            text = f"No encontré ninguna Semana {section_number} real en la estructura del curso — no inventes esa sección."
            return _wrap_as_candidate(text=text, filename="Progreso real del estudiante (Moodle)", section_name="Actividades pendientes")
        if not pending and not completed:
            text = (
                f"En \"{scope_label}\" no hay ninguna actividad con seguimiento de finalización (completion "
                "tracking) activado en Moodle — no se puede afirmar qué está pendiente o completado ahí. No "
                "inventes una lista de tareas para esa sección; sugerile al estudiante revisar esa semana "
                "directamente en el aula virtual."
            )
            return _wrap_as_candidate(text=text, filename="Progreso real del estudiante (Moodle)", section_name=scope_label)

    scope_txt = f" en \"{scope_label}\"" if scope_label else " en este curso"

    if not pending:
        # Se listan también las actividades YA completadas que Moodle sí
        # rastrea (no solo "no hay pendientes" a secas) — es la evidencia
        # real que evita que el modelo, ante un "¿por qué?" del estudiante,
        # invente una excusa (bug real visto: dijo "tal vez no se habilitó
        # la fecha de entrega" sin ningún dato que lo respalde). Con esto
        # puede señalar, con nombre real, cuál actividad concreta ya
        # figura como completada en vez de una razón inventada.
        lines = [f"Según el registro real de completion de Moodle, no hay actividades pendientes marcadas{scope_txt}."]
        if completed:
            lines.append(f"Actividades con seguimiento de finalización activado, YA marcadas como completadas{scope_txt}:")
            rows = [[by_cmid.get(cmid, (f"Actividad {cmid}", "", None))[0], by_cmid.get(cmid, ("", "", None))[1]] for cmid in completed]
            lines.append(_md_table(["Actividad", "Semana/Sección"], rows))
            lines.append(
                "IMPORTANTE: solo se listan actividades con seguimiento de finalización (completion "
                "tracking) activado en Moodle — puede haber otras actividades del curso sin ese "
                "seguimiento activado que Moodle no reporta acá. Si el estudiante insiste en que le "
                "falta algo que no aparece en esta lista, no inventes una razón (nunca digas 'quizás no "
                "se habilitó la fecha de entrega' ni nada que no esté en esta evidencia) — decile con "
                "honestidad que, según el registro de Moodle, esa actividad puntual figura como "
                "completada (si está en la lista de arriba) o que no tenés visibilidad sobre su estado "
                "(si no está en ninguna lista), y sugerile confirmar directamente en el aula virtual. "
                "Mantené el formato de tabla de arriba en tu respuesta, no lo reescribas como viñetas."
            )
        text = "\n".join(lines)
        return _wrap_as_candidate(text=text, filename="Progreso real del estudiante (Moodle)", section_name=scope_label or "Actividades pendientes")

    lines = [f"Actividades pendientes según el registro real de completion de Moodle{scope_txt}:"]
    rows = [[by_cmid.get(p["cmid"], (f"Actividad {p['cmid']}", "", None))[0], by_cmid.get(p["cmid"], ("", "", None))[1]] for p in pending]
    lines.append(_md_table(["Actividad", "Semana/Sección"], rows))
    lines.append("Mantené el formato de tabla de arriba en tu respuesta, no lo reescribas como viñetas.")
    text = "\n".join(lines)
    return _wrap_as_candidate(text=text, filename="Progreso real del estudiante (Moodle)", section_name=scope_label or "Actividades pendientes")


# Convención real de nombres de ULADECH, vista en todos los ítems del
# gradebook de este curso ("U1-AF-...", "U2-Ex-...") — mismo criterio ya
# usado en el resto del código para "Semana N"/"Unidad N" (parseo de texto
# genérico, no una lista curada por curso puntual).
_ITEM_UNIT_PREFIX_RE = re.compile(r"^U(\d+)[-\s]", re.IGNORECASE)


def _item_unit(item_name: str) -> str:
    m = _ITEM_UNIT_PREFIX_RE.match(item_name.strip())
    return f"Unidad {m.group(1)}" if m else "-"


def _format_grades_evidence(course_id: int, user_id: int) -> Dict[str, Any]:
    """Notas reales del estudiante — gradereport_user_get_grade_items,
    confirmada en vivo que SÍ está autorizada para este token (a
    diferencia de core_grades_get_grades, que no existe en el servicio
    externo). Nunca presenta un criterio de evaluación genérico del
    sílabo/sesión como si fuera la nota real (bug real encontrado antes de
    conectar esta función) — siempre distingue ítems con nota real puesta
    de los que todavía no se calificaron, sin inventar ningún valor."""
    result = mt.get_grade_items(course_id, user_id)
    if result.get("status") != "ok":
        text = (
            "No se pudo obtener el registro real de calificaciones del estudiante en este momento. No "
            "presentes los criterios de evaluación del sílabo o de una sesión como si fueran la nota "
            "real del estudiante — decile con honestidad que no pudiste consultar sus notas ahora."
        )
        return _wrap_as_candidate(text=text, filename="Calificaciones del estudiante (Moodle)", section_name="No disponible")

    items = result["items"]
    graded_count = sum(1 for i in items if i["graded"])

    rows = [
        [
            it["name"],
            _item_unit(it["name"]),
            it["grade"] if it["graded"] else "Sin calificar",
            it["grademax"],
            it["percentage"] if it["graded"] else "-",
            it.get("feedback") or "-",
        ]
        for it in items
    ]

    lines = [
        "Calificaciones reales del estudiante, según el registro de notas (gradebook) de Moodle "
        f"({graded_count} de {len(items)} ítems ya calificados):",
        _md_table(["Actividad", "Unidad", "Nota", "Sobre", "%", "Retroalimentación"], rows),
        "NOTA SOBRE AGRUPACIÓN: el gradebook de Moodle agrupa las notas por Unidad (columna 'Unidad' de la "
        "tabla, tomada del prefijo real del nombre del ítem, ej. 'U1-'/'U2-'), NO por semana individual — "
        "no existe una forma real de filtrar por una semana exacta. Si el estudiante pide notas 'de esta "
        "semana' o de una semana puntual, NUNCA inventes qué ítems corresponden a esa semana; como mucho "
        "podés ofrecer los de la Unidad a la que pertenece esa semana si el estudiante ya te dijo o "
        "preguntó por esa unidad antes, aclarando explícitamente que el agrupamiento real es por unidad, "
        "no por semana exacta.",
        "IMPORTANTE: usa EXACTAMENTE estos valores tal cual, nunca inventes ni calcules una nota "
        "distinta ni un promedio que no esté ya dado arriba. Nunca presentes un criterio de evaluación "
        "del sílabo/sesión como si fuera una nota real. Un ítem con 'Sin calificar' no es una nota "
        "negativa ni un cero, simplemente el docente todavía no lo calificó. Mantené el formato de tabla "
        "de arriba en tu respuesta, no lo reescribas como viñetas.",
    ]
    text = "\n".join(lines)
    return _wrap_as_candidate(text=text, filename="Calificaciones del estudiante (Moodle)", section_name="Calificaciones")


def _format_evaluation_criteria_evidence(collection_name: str) -> Optional[Dict[str, Any]]:
    """Cuadro REAL de "Evidencias evaluables, instrumentos y ponderación"
    del sílabo (ver A.1 del plan) — determinista, desde el índice
    estructurado. Antes esto solo existía como instrucción de prompt ("si
    el material recuperado trae la nota mínima, priorizala"), que dependía
    de que RAG trajera el chunk correcto. Nunca arma una columna "Unidad"
    que la fuente no trae por fila."""
    rows = ce.get_table_rows(
        collection_name, ["evidencia evaluable", "instrumento", "ponderacion"],
    )
    if not rows:
        return None
    table_rows = [
        [r.get("evidencia evaluable"), r.get("instrumento"), r.get("ponderacion")]
        for r in rows
    ]
    lines = [
        "Cuadro REAL de evidencias evaluables, instrumentos y ponderación del sílabo (tal cual está indexado):",
        _md_table(["Evidencia evaluable", "Instrumento", "Ponderación"], table_rows),
        "IMPORTANTE: presentá esta tabla tal cual, con estas tres columnas y estas filas. No agregues ni "
        "inventes una columna 'Unidad' si la fuente no la trae explícita por fila, no omitas filas ni "
        "inventes ponderaciones. Mantené el formato de tabla en tu respuesta, no lo reescribas como viñetas.",
    ]
    return _wrap_as_candidate(
        text="\n".join(lines),
        filename="Sílabo — Evidencias evaluables",
        section_name="Evidencias evaluables, instrumentos y ponderación",
    )


def _study_field_for_week(collection_name: str, week_n: int, field: str) -> Optional[str]:
    """Valor real de `field` para la semana `week_n`, desde el documento de
    sesión de esa semana (campos_estructurados.get_field_by_week). None si
    esa semana no trae ese campo — nunca se inventa."""
    return ce.get_field_by_week(collection_name, field).get(week_n)


def _format_study_help_evidence(
    collection_name: str, course_id: int, section_number: Optional[int] = None,
) -> Optional[Dict[str, Any]]:
    """"Quiero estudiar para la semana X, dame preguntas" — arma la
    evidencia evaluable y los criterios REALES de esa semana (ver A.2 del
    plan), más los recursos reales de Moodle de esa sección (incluido
    cualquier enlace real de evaluación oral/escrita). Si no hay semana
    explícita, resuelve la semana actual por fecha real (nunca adivinada).
    La instrucción al LLM ata las preguntas de práctica a esa evidencia/
    criterio concreto, nunca a reflexión genérica."""
    if section_number is None:
        current = cm.get_current_week_number(course_id)
        if current.get("status") != "confident":
            return None
        section_number = current["week_number"]

    evidencia = _study_field_for_week(collection_name, section_number, "evidencia de aprendizaje")
    criterios = _study_field_for_week(collection_name, section_number, "criterios de evaluación")
    section_evidence = _format_section_contents_evidence(course_id, section_number)

    if not evidencia and not criterios and not section_evidence:
        return None

    lines = [f"Material REAL de la Semana {section_number} para preparar la evaluación:"]
    if evidencia:
        lines.append(f"- Evidencia de aprendizaje de esta semana: {evidencia}")
    if criterios:
        lines.append(f"- Criterios de evaluación de esta semana: {criterios}")
    if section_evidence:
        lines.append("")
        lines.append(section_evidence["payload"]["text"])
    lines.append(
        "IMPORTANTE: generá preguntas de práctica atadas a la evidencia y los criterios REALES de arriba, "
        "nombrando el código real de la actividad (p.ej. AF/IF/RSU) si aparece. Nunca preguntas genéricas "
        "de reflexión desconectadas de lo que se va a calificar. Si los recursos reales de la semana "
        "indican un formato puntual (oral, escrito), mencionálo como parte de cómo prepararse, sin "
        "inventar nada sobre qué hace ese recurso por dentro."
    )
    return _wrap_as_candidate(
        text="\n".join(lines),
        filename="Evidencias y criterios reales de la semana (Sílabo/Moodle)",
        section_name=f"Semana {section_number} — preparación",
    )


def _format_due_dates_evidence(course_id: int) -> Optional[Dict[str, Any]]:
    """"¿Cuándo vence...?" — fechas REALES de entrega (mdl_event vía
    moodle_calendar). Si no hay fuente disponible, degrada de forma honesta
    y NUNCA inventa una fecha (Parte C del plan)."""
    result = mc.get_due_dates(course_id)
    if result.get("status") == "unavailable":
        text = (
            "No tengo acceso a las fechas de entrega de este curso en este momento. No inventes ni "
            "supongas ninguna fecha: decile al estudiante que confirme las fechas directamente en el "
            "aula virtual."
        )
        return _wrap_as_candidate(text=text, filename="Fechas de entrega (Moodle)", section_name="No disponible")
    if result.get("status") != "confident":
        return None
    rows = [[e["name"], e["due_date"]] for e in result["events"]]
    lines = [
        "Fechas REALES de vencimiento de las actividades del curso (según Moodle):",
        _md_table(["Actividad", "Vence"], rows),
        "IMPORTANTE: usá exactamente estas fechas, sin inventar ni ajustar ninguna. Mantené el formato "
        "de tabla en tu respuesta.",
    ]
    return _wrap_as_candidate(
        text="\n".join(lines),
        filename="Fechas de entrega (Moodle)",
        section_name="Vencimientos del curso",
    )


def _format_schedule_evidence(course_id: int) -> Optional[Dict[str, Any]]:
    fields = cm.parse_general_section_fields(course_id)
    hora_key = next((k for k in fields if "HORA" in k), None)
    if not hora_key:
        return None
    text = f"Según el resumen del aula del curso, el horario de clases es: {fields[hora_key]}."
    return _wrap_as_candidate(text=text, filename="Sección General del aula (Moodle)", section_name="Horario del curso")


def _wrap_as_candidate(text: str, filename: str, section_name: str) -> Dict[str, Any]:
    """Misma forma exacta que rag_pipeline._build_structured_evidence_candidate()
    — para que build_generation_prompt()/apply_source_boost() lo traten
    igual que cualquier otro candidato ya validado."""
    return {
        "payload": {
            "filename": filename,
            "source_type": "curso_propio",
            "section_name": section_name,
            "chunk": None, "slide_number": None, "start_time": None,
            "text": text,
            "contextual_header": "[Evidencia del Query Router — Course Map / Structured Query Engine, no RAG]",
        },
        "score": 1.0,
        "rerank_score": 1.0,
        "boosted_score": 10_000.0,
    }


def route(
    collection_name: str, course_id: Optional[int], query: str, user_id: Optional[int] = None,
) -> Optional[Dict[str, Any]]:
    """Punto de entrada único. None si ninguna regla determinista aplica
    (o si el intent SÍ matchea pero la fuente determinista no tiene
    evidencia confidente) — en ambos casos el caller sigue con RAG normal
    sin ningún cambio de comportamiento. `user_id`: SIEMPRE el
    moodle_userid real de la sesión que pregunta (ver app.py), nunca de
    otro estudiante — necesario para PROGRESS."""
    parsed = _classify(query)
    if parsed is None:
        return None

    if parsed["intent"] == "STRUCTURED":
        result = ce.structured_query(collection_name, {
            "operation": parsed["operation"], "entity": parsed["entity"], "value": parsed["value"],
            "with_topics": parsed.get("with_topics", False), "course_id": course_id,
        })
        if result is None:
            return None
        return _format_structured_evidence(parsed, result)

    if parsed["intent"] == "EVALUATION_CRITERIA":
        return _format_evaluation_criteria_evidence(collection_name)

    if not course_id:
        return None

    try:
        if parsed["intent"] == "STUDY_HELP":
            return _format_study_help_evidence(collection_name, course_id, parsed.get("section_number"))

        if parsed["intent"] == "DUE_DATES":
            return _format_due_dates_evidence(course_id)

        if parsed["intent"] == "PROGRESS":
            if not user_id:
                return None
            return _format_progress_evidence(course_id, user_id, parsed.get("section_number"))

        if parsed["intent"] == "GRADES":
            if not user_id:
                return None
            return _format_grades_evidence(course_id, user_id)

        if parsed["intent"] == "CURRENT_WEEK":
            return _format_current_week_evidence(collection_name, course_id)

        if parsed["intent"] == "COURSE_DATE":
            return _format_course_date_evidence(course_id)

        if parsed["intent"] == "SCHEDULE":
            return _format_schedule_evidence(course_id)

        if parsed["intent"] == "NAVIGATION":
            if parsed.get("ordinal"):
                found = mt.find_first_activity(course_id, query)
                if not found:
                    return None
                text = f"La actividad \"{found['name']}\" está en {found['section_name']} — enlace real: {found['url']}"
                return _wrap_as_candidate(text=text, filename="Estructura real del curso (Moodle)", section_name=found["section_name"])
            return _format_navigation_evidence(course_id, query)
    except Exception as e:
        print(f"⚠️ query_router: intent {parsed['intent']} falló (se ignora, cae a RAG): {e}")
        return None

    return None
