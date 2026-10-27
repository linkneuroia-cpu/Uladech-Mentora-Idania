"""
moodle_tools.py
================
Herramientas de navegación (Incremento 1) y de progreso del estudiante
(Incremento 2, ver plan de arquitectura de esta sesión) para el Query
Router. Navegación opera sobre course_map.CourseMap (estructura real de
Moodle, sin inventar URLs/ids). Progreso/completion está SIMULADO por
ahora: `core_completion_get_course_completion_status` y
`core_completion_get_activities_completion_status` están BLOQUEADAS para
el token actual de ULADECH (verificado en vivo esta sesión — devuelven
"webservice_access_exception"). Las funciones de progreso de abajo tienen
EXACTAMENTE la forma de respuesta real documentada de Moodle (mismos
nombres de campo, mismos tipos) para que, el día que un admin de Moodle
habilite esas dos funciones en el servicio externo del token, el único
cambio necesario sea flipear MOODLE_COMPLETION_ENABLED a True — nada más
del sistema (router, prompts, generación) tiene que cambiar.
"""
from __future__ import annotations

import re
import unicodedata
from typing import Any, Dict, List, Optional

import requests

from settings import settings
import course_map as cm

# ════════════════════════════════════════════════════════════════════
# NAVEGACIÓN — Incremento 1, sin simulación, contra el Course Map real.
# ════════════════════════════════════════════════════════════════════

def get_course_structure(course_id: int) -> Dict[str, Any]:
    """Estructura completa del curso (secciones + actividades), lista
    para citar — cada actividad trae su URL real de Moodle."""
    course = cm.get_course_map(course_id)
    return {
        "course_id": course.course_id,
        "fetched_at": course.fetched_at,
        "sections": [
            {
                "section_number": s.section_number,
                "name": s.name,
                "visible": s.visible,
                "activities": [
                    {"cmid": a.cmid, "name": a.name, "modname": a.modname, "url": a.url, "visible": a.visible}
                    for a in s.activities
                ],
            }
            for s in course.sections
        ],
    }


def get_section(course_id: int, section_number: int) -> Optional[Dict[str, Any]]:
    course = cm.get_course_map(course_id)
    for s in course.sections:
        if s.section_number == section_number:
            return {
                "section_number": s.section_number, "name": s.name, "visible": s.visible,
                "activities": [
                    {"cmid": a.cmid, "name": a.name, "modname": a.modname, "url": a.url, "visible": a.visible}
                    for a in s.activities
                ],
            }
    return None


def _strip_accents_lower(text: str) -> str:
    nfkd = unicodedata.normalize("NFKD", text)
    return "".join(c for c in nfkd if not unicodedata.combining(c)).lower()


_STOPWORDS = {"el", "la", "los", "las", "de", "del", "en", "y", "a", "un", "una", "para", "con", "que"}

# Alias genéricos del vocabulario académico (no de un curso puntual): la
# pregunta y el nombre real de la actividad se normalizan por igual antes de
# comparar, para que "examen de la unidad I" coincida con
# "U1-Ex-Examen sumativo".
_ROMAN_UNIDAD = {
    "i": "1", "ii": "2", "iii": "3", "iv": "4", "v": "5",
    "vi": "6", "vii": "7", "viii": "8", "ix": "9", "x": "10",
}


def _normalize_aliases(text: str) -> str:
    t = _strip_accents_lower(text)
    t = re.sub(r"\bu\s*-?\s*(\d+)\b", lambda m: f"unidad {m.group(1)}", t)
    t = re.sub(
        r"\bunidad\s+([ivx]+)\b",
        lambda m: f"unidad {_ROMAN_UNIDAD.get(m.group(1), m.group(1))}",
        t,
    )
    t = re.sub(r"\bex\b", "examen", t)
    t = re.sub(r"\bef\b", "evaluacion formativa", t)
    t = re.sub(r"\baf\b", "actividad formativa", t)
    return t


def _content_words(text: str) -> set:
    normalized = _normalize_aliases(text)
    words = re.findall(r"[a-z0-9]+", normalized)
    # Se conservan los números de un dígito ("unidad 1") porque distinguen
    # una unidad de otra; el resto sigue el criterio de longitud.
    return {w for w in words if w not in _STOPWORDS and (len(w) > 2 or w.isdigit())}


def find_activity(course_id: int, query: str) -> List[Dict[str, Any]]:
    """Busca actividades por nombre/sección usando superposición de
    palabras (mismo enfoque genérico que ya usa campos_estructurados.py
    para matching léxico — sin lista curada de sinónimos). También
    entiende "semana N" extrayendo el número y matcheando contra el
    número de sección real de Moodle (robusto a errores de tipeo del
    propio nombre de sección en Moodle, ej. 'Senana 04' — no depende del
    texto exacto, solo del número real de sección)."""
    course = cm.get_course_map(course_id)
    query_words = _content_words(query)

    week_match = re.search(r"semana\s*(\d+)", query.lower())
    target_section: Optional[int] = int(week_match.group(1)) if week_match else None

    results = []
    for s in course.sections:
        section_words = _content_words(s.name)
        for a in s.activities:
            # Las actividades ocultas para alumnos y las etiquetas no se
            # muestran ni se resuelven como recurso (decisión del curso).
            if a.modname == "label" or not a.visible or not a.uservisible:
                continue
            activity_words = _content_words(a.name)
            overlap = query_words & (activity_words | section_words)
            score = len(overlap) / max(len(query_words), 1)
            if target_section is not None and s.section_number == target_section:
                score += 1.0  # coincidencia exacta de semana pesa más que solapamiento léxico
            if score > 0:
                results.append({
                    "score": score, "cmid": a.cmid, "name": a.name, "modname": a.modname,
                    "url": a.url, "section_number": s.section_number, "section_name": s.name,
                })
    results.sort(key=lambda r: r["score"], reverse=True)
    return results[:10]


# Vocabulario genérico español -> modname real de Moodle (fijo en
# cualquier instalación de Moodle, no específico de ningún curso) — mismo
# rol que _STOPWORDS: lenguaje natural genérico, no contenido curado de un
# curso puntual.
_MODNAME_WORDS = {
    "tarea": {"assign"}, "tareas": {"assign"}, "actividad": {"assign"},
    "cuestionario": {"quiz"}, "examen": {"assign", "quiz"}, "evaluacion": {"assign", "quiz"},
    "quiz": {"quiz"},
    "foro": {"forum"},
    "carpeta": {"folder"},
    "video": {"url"}, "enlace": {"url"}, "link": {"url"},
    "material": {"resource"}, "recurso": {"resource"}, "lectura": {"resource"},
}

_ORDINAL_LAST = {"ultima", "ultimo", "final"}
_ORDINAL_FIRST = {"primera", "primero", "inicial"}


def find_first_activity(course_id: int, query: str) -> Optional[Dict[str, Any]]:
    """Resuelve preguntas tipo "envíame la primera tarea del curso":
    detecta el tipo de actividad (vocabulario genérico de arriba, vacío =
    cualquier tipo) y el ordinal (primera/última, default primera),
    ordena por número de sección real de Moodle y devuelve esa actividad
    — nunca inventa cuál es "la primera", se basa en el orden real del
    curso en Moodle."""
    words = _content_words(query)
    target_modnames: Optional[set] = None
    for w in words:
        if w in _MODNAME_WORDS:
            target_modnames = _MODNAME_WORDS[w]
            break
    want_last = bool(words & _ORDINAL_LAST) and not (words & _ORDINAL_FIRST)

    course = cm.get_course_map(course_id)
    candidates = []
    for s in course.sections:
        if s.section_number == 0:
            continue  # 'General' no es parte de la secuencia semanal del curso
        for a in s.activities:
            if a.modname == "label" or not a.visible or not a.uservisible:
                continue
            if target_modnames and a.modname not in target_modnames:
                continue
            candidates.append({
                "cmid": a.cmid, "name": a.name, "modname": a.modname, "url": a.url,
                "section_number": s.section_number, "section_name": s.name,
            })
    if not candidates:
        return None
    candidates.sort(key=lambda c: c["section_number"], reverse=want_last)
    return candidates[0]


def get_activity_url(course_id: int, cmid: int) -> Optional[str]:
    course = cm.get_course_map(course_id)
    for s in course.sections:
        for a in s.activities:
            if a.cmid == cmid:
                return a.url
    return None


# ════════════════════════════════════════════════════════════════════
# PROGRESO DEL ESTUDIANTE — Incremento 2, SIMULADO hasta que ULADECH
# habilite las funciones reales de Moodle para este token.
# ════════════════════════════════════════════════════════════════════

# Habilitado — el admin de Moodle de ULADECH autorizó ambas funciones.
# Reverificado en vivo: core_completion_get_activities_completion_status
# ya devuelve datos reales (state/tracking/isoverallcomplete por
# actividad). core_completion_get_course_completion_status también está
# autorizada, pero para Curso_1472 puntual devuelve "nocriteriaset" (el
# curso no tiene criterios de finalización configurados en Moodle — una
# config del curso, no un bloqueo de permisos) — se maneja como cualquier
# otra excepción de Moodle vía el chequeo genérico de abajo, sin caso
# especial.
MOODLE_COMPLETION_ENABLED = True


def _moodle_ws_call(wsfunction: str, **params) -> Dict[str, Any]:
    url = f"{settings.MOODLE_URL}/webservice/rest/server.php"
    p = {"wstoken": settings.MOODLE_TOKEN, "wsfunction": wsfunction, "moodlewsrestformat": "json"}
    p.update(params)
    r = requests.get(url, params=p, verify=False)
    r.raise_for_status()
    return r.json()


def get_course_completion_status(course_id: int, user_id: int) -> Dict[str, Any]:
    """Misma forma que la respuesta REAL y documentada de
    core_completion_get_course_completion_status: {"completionstatus":
    {"completed": bool, "aggregation": int, "completions": [...]}}.
    Simulada mientras MOODLE_COMPLETION_ENABLED sea False — se marca
    explícitamente con "_simulated": True para que el router/prompt nunca
    la presente como un dato real verificado."""
    if MOODLE_COMPLETION_ENABLED:
        data = _moodle_ws_call(
            "core_completion_get_course_completion_status", courseid=course_id, userid=user_id,
        )
        if isinstance(data, dict) and "exception" in data:
            return {"status": "error", "message": data.get("message"), "_simulated": False}
        data["_simulated"] = False
        return data

    return {
        "completionstatus": {"completed": False, "aggregation": 1, "completions": []},
        "_simulated": True,
        "_simulated_reason": (
            "core_completion_get_course_completion_status está bloqueada para el token actual de "
            "ULADECH (verificado en vivo) — pendiente de habilitación por un admin de Moodle."
        ),
    }


def get_activity_completion_status(course_id: int, user_id: int) -> Dict[str, Any]:
    """Misma forma que la respuesta REAL de
    core_completion_get_activities_completion_status: {"statuses": [
    {"cmid": int, "modname": str, "instance": int, "state": int,
    "timecompleted": int, "tracking": int}]}. state: 0=incompleto,
    1=completo, 2=completo-aprobado, 3=completo-desaprobado. tracking:
    0=sin seguimiento, 1=manual, 2=automático (mismo campo que ya expone
    course_map.Activity.completion_tracking, tomado de
    core_course_get_contents, que SÍ está disponible hoy)."""
    if MOODLE_COMPLETION_ENABLED:
        data = _moodle_ws_call(
            "core_completion_get_activities_completion_status", courseid=course_id, userid=user_id,
        )
        if isinstance(data, dict) and "exception" in data:
            return {"status": "error", "message": data.get("message"), "_simulated": False}
        data["_simulated"] = False
        return data

    # Simulación estructuralmente correcta: usa el Course Map real (que SÍ
    # funciona) para listar las actividades reales con su modo de
    # seguimiento real, pero sin poder saber el ESTADO real (completo/
    # incompleto) porque esa parte requiere la función bloqueada — se
    # reporta state=-1 (valor que Moodle nunca usa) como "desconocido",
    # nunca 0 (que significaría "incompleto", una afirmación falsa).
    course = cm.get_course_map(course_id)
    statuses = [
        {
            "cmid": a.cmid, "modname": a.modname, "instance": a.cmid, "state": -1,
            "timecompleted": 0, "tracking": a.completion_tracking,
        }
        for s in course.sections for a in s.activities if a.completion_tracking > 0
    ]
    return {
        "statuses": statuses,
        "_simulated": True,
        "_simulated_reason": (
            "core_completion_get_activities_completion_status está bloqueada para el token actual de "
            "ULADECH (verificado en vivo) — pendiente de habilitación por un admin de Moodle. "
            "state=-1 significa 'estado real desconocido', no 'incompleto'."
        ),
    }


def get_pending_activities(course_id: int, user_id: int) -> Dict[str, Any]:
    """Compuesta: actividades con seguimiento de completion activo cuyo
    estado real se desconoce (ver nota de simulación arriba) — hasta que
    se habilite la función real, esto NO puede afirmar qué está pendiente
    de verdad, solo listar candidatas."""
    completion = get_activity_completion_status(course_id, user_id)
    statuses = completion["statuses"]
    pending = [s for s in statuses if s["state"] in (-1, 0)]
    completed = [s["cmid"] for s in statuses if s["state"] not in (-1, 0)]
    return {
        "pending_candidates": pending,
        "completed_activities": completed,
        "_simulated": completion.get("_simulated", True),
        "_simulated_reason": completion.get("_simulated_reason"),
    }


# ════════════════════════════════════════════════════════════════════
# CALIFICACIONES REALES — gradereport_user_get_grade_items, verificada en
# vivo esta sesión que SÍ está autorizada para este token de ULADECH
# (distinto de completion: no fue anunciada explícitamente, se descubrió
# probándola). core_grades_get_grades NO existe en el servicio externo
# (confirmado con la excepción real de Moodle) — esta es la única función
# de notas disponible.
# ════════════════════════════════════════════════════════════════════

_HTML_TAG_RE = re.compile(r"<[^>]+>")


def get_grade_items(course_id: int, user_id: int) -> Dict[str, Any]:
    """Devuelve cada ítem real del libro de calificaciones del curso
    (actividades + promedios de unidad/curso) con su nota si ya fue
    puesta. `graded=False` cuando Moodle trae `graderaw=null` — el
    docente todavía no calificó ese ítem, nunca se infiere ni se inventa
    un valor intermedio. Los ítems marcados `gradeishidden` por el
    docente se excluyen (Moodle tampoco se los muestra al estudiante en
    su propia vista, se respeta la misma visibilidad acá)."""
    data = _moodle_ws_call("gradereport_user_get_grade_items", courseid=course_id, userid=user_id)
    if isinstance(data, dict) and "exception" in data:
        return {"status": "error", "message": data.get("message")}
    usergrades = data.get("usergrades") or []
    if not usergrades:
        return {"status": "error", "message": "Moodle no devolvió calificaciones para este usuario/curso."}
    items = []
    for it in usergrades[0].get("gradeitems", []):
        if it.get("gradeishidden"):
            continue
        items.append({
            "name": (it.get("itemname") or "").strip() or "(ítem sin nombre)",
            "graded": it.get("graderaw") is not None,
            "grade": it.get("gradeformatted"),
            "grademax": it.get("grademax"),
            "percentage": it.get("percentageformatted"),
            "feedback": _HTML_TAG_RE.sub("", it.get("feedback") or "").strip(),
        })
    return {"status": "ok", "items": items}
