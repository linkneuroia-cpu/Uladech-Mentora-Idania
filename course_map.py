"""
course_map.py
==============
Course Map — Incremento 1 del agente académico híbrido (Course Map +
Navegación + Structured Query Engine, ver plan de arquitectura de esta
sesión). Representa la estructura NAVEGABLE real de un curso de Moodle
(secciones/actividades/urls/visibilidad), para responder preguntas de
navegación ("¿dónde está X?") sin RAG ni LLM.

Reusa `core_course_get_contents` (misma función que ya usa
`definitions.get_course_resources()` para vectorización), pero sin
filtrar/aplanar — acá se necesita el árbol completo (todos los modtypes,
con cmid/url/visibilidad/número de sección), no solo los archivos
descargables. Por eso es una llamada propia a Moodle, no una reutilización
de esa función.

Caché: tabla Postgres `course_map_cache` (ver
db_migration_course_map_y_fuente_autoridad.sql). Invalidación primaria
atada a `ledger.sync_curso_archivos()` (cuando detecta archivos nuevos/
modificados/eliminados, el Course Map de ese curso también se invalida —
ver `invalidate_course_map()`, llamada desde ese mismo flujo). TTL de 24h
como respaldo secundario para cursos donde nadie disparó un sync
recientemente — el docente puede subir contenido en cualquier momento, así
que la estructura NO se asume estática.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import requests

import db
from settings import settings

DEFAULT_TTL_MINUTES = 1440  # 24h — respaldo secundario, no el mecanismo principal


@dataclass
class Activity:
    cmid: int                      # module "id" real de Moodle — nunca inventado
    name: str
    modname: str                   # 'resource','url','assign','quiz','forum', etc.
    url: Optional[str]              # link real que Moodle ya provee — nunca construido a mano
    visible: bool
    uservisible: bool
    completion_tracking: int        # 0/1/2 = sin seguimiento/manual/automático (MODO, no estado)
    section_number: int


@dataclass
class Section:
    section_number: int             # campo 'section' real de Moodle
    name: str
    visible: bool
    summary: str
    activities: List[Activity] = field(default_factory=list)


@dataclass
class CourseMap:
    course_id: int
    fetched_at: str                 # ISO 8601 UTC
    sections: List[Section] = field(default_factory=list)

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False)

    @staticmethod
    def from_json(raw: str) -> "CourseMap":
        data = json.loads(raw)
        sections = [
            Section(
                section_number=s["section_number"], name=s["name"], visible=s["visible"],
                summary=s["summary"],
                activities=[Activity(**a) for a in s.get("activities", [])],
            )
            for s in data.get("sections", [])
        ]
        return CourseMap(course_id=data["course_id"], fetched_at=data["fetched_at"], sections=sections)


def _fetch_course_map_from_moodle(course_id: int) -> CourseMap:
    """Llamada real a core_course_get_contents — misma función que ya usa
    definitions.get_course_resources(), pero sin filtrar/aplanar: acá se
    necesita el árbol completo (todos los modtypes) para navegación."""
    url = f"{settings.MOODLE_URL}/webservice/rest/server.php"
    params = {
        "wstoken": settings.MOODLE_TOKEN,
        "wsfunction": "core_course_get_contents",
        "moodlewsrestformat": "json",
        "courseid": course_id,
    }
    r = requests.get(url, params=params, verify=False)
    r.raise_for_status()
    data = r.json()
    if isinstance(data, dict) and "exception" in data:
        raise RuntimeError(f"Moodle API error (core_course_get_contents): {data.get('message', data)}")

    sections: List[Section] = []
    for raw_section in data:
        activities = [
            Activity(
                cmid=m.get("id"),
                name=m.get("name") or "",
                modname=m.get("modname") or "",
                url=m.get("url"),
                visible=bool(m.get("visible", 1)),
                uservisible=bool(m.get("uservisible", m.get("visible", 1))),
                completion_tracking=int(m.get("completion") or 0),
                section_number=raw_section.get("section", 0),
            )
            for m in raw_section.get("modules", [])
        ]
        sections.append(Section(
            section_number=raw_section.get("section", 0),
            name=raw_section.get("name") or "",
            visible=bool(raw_section.get("visible", 1)),
            summary=raw_section.get("summary") or "",
            activities=activities,
        ))

    return CourseMap(
        course_id=course_id,
        fetched_at=datetime.now(timezone.utc).isoformat(),
        sections=sections,
    )


def get_course_map(course_id: int, force_refresh: bool = False) -> CourseMap:
    """Punto de entrada único: devuelve el Course Map, desde caché si está
    vigente (no invalidado por sync de archivos Y dentro del TTL), o lo
    trae de Moodle y lo cachea si no."""
    if not force_refresh:
        row = db.fetch_one(
            "SELECT map_json, fetched_at, ttl_minutes, invalidated_at "
            "FROM course_map_cache WHERE moodle_courseid = %s",
            (course_id,),
        )
        if row and row["invalidated_at"] is None:
            age_minutes = (datetime.now(timezone.utc) - row["fetched_at"]).total_seconds() / 60
            if age_minutes < row["ttl_minutes"]:
                return CourseMap.from_json(json.dumps(row["map_json"]) if isinstance(row["map_json"], dict) else row["map_json"])

    course_map = _fetch_course_map_from_moodle(course_id)
    db.execute(
        "INSERT INTO course_map_cache (moodle_courseid, map_json, fetched_at, ttl_minutes, invalidated_at) "
        "VALUES (%s, %s, now(), %s, NULL) "
        "ON CONFLICT (moodle_courseid) DO UPDATE SET "
        "map_json = EXCLUDED.map_json, fetched_at = now(), invalidated_at = NULL",
        (course_id, course_map.to_json(), DEFAULT_TTL_MINUTES),
    )
    return course_map


def get_course_dates(course_id: int) -> Dict[str, Any]:
    """startdate/enddate reales de Moodle (core_course_get_courses, ya en
    uso y verificado funcionando con el token actual) — a diferencia de la
    tarjeta HTML del aula (frágil, descartada), este es un campo nativo de
    la API. Devuelve epoch y fecha legible en español; startdate=0
    significa que Moodle no tiene fecha configurada para este curso (no se
    inventa una)."""
    url = f"{settings.MOODLE_URL}/webservice/rest/server.php"
    params = {
        "wstoken": settings.MOODLE_TOKEN,
        "wsfunction": "core_course_get_courses",
        "moodlewsrestformat": "json",
        "options[ids][0]": course_id,
    }
    r = requests.get(url, params=params, verify=False)
    r.raise_for_status()
    data = r.json()
    if isinstance(data, dict) and "exception" in data:
        raise RuntimeError(f"Moodle API error (core_course_get_courses): {data.get('message', data)}")
    if not data:
        return {"status": "not_found"}
    course = data[0]
    startdate = course.get("startdate") or 0
    enddate = course.get("enddate") or 0
    if not startdate:
        return {"status": "not_found", "reason": "moodle_sin_fecha_configurada"}
    return {
        "status": "confident",
        "startdate_epoch": startdate,
        "startdate": _format_date_es(startdate),
        "enddate_epoch": enddate,
        "enddate": _format_date_es(enddate) if enddate else None,
    }


def get_current_week_number(course_id: int) -> Dict[str, Any]:
    """Calcula determinísticamente en qué semana real está el curso HOY
    (fecha del servidor, UTC) a partir de startdate real de Moodle — nunca
    adivinado por RAG. Asume 1 sección semanal = 1 semana calendario desde
    el inicio (la misma convención que ya usa la numeración de secciones
    de Moodle en este tipo de curso). Si el curso no empezó todavía o ya
    terminó, lo dice explícitamente en vez de devolver un número fuera de
    rango."""
    dates = get_course_dates(course_id)
    if dates.get("status") != "confident":
        return {"status": "not_found"}
    started = datetime.fromtimestamp(dates["startdate_epoch"], tz=timezone.utc)
    now = datetime.now(timezone.utc)
    elapsed_days = (now - started).days
    if elapsed_days < 0:
        return {"status": "not_started", "startdate": dates["startdate"]}
    week_n = elapsed_days // 7 + 1
    return {"status": "confident", "week_number": week_n, "elapsed_days": elapsed_days}


_MESES_ES = [
    "", "enero", "febrero", "marzo", "abril", "mayo", "junio", "julio",
    "agosto", "septiembre", "octubre", "noviembre", "diciembre",
]


def _format_date_es(epoch: int) -> str:
    """strftime('%B') depende del locale del sistema (en inglés acá) —
    tabla fija de meses en español, no depende de ningún curso puntual."""
    dt = datetime.fromtimestamp(epoch, tz=timezone.utc)
    return f"{dt.day:02d} de {_MESES_ES[dt.month]} de {dt.year}"


# Patrón genérico de la plantilla institucional de ULADECH en el resumen
# de la sección "General" — cualquier bloque <strong>...</strong> que
# contenga un ":" se trata como "ETIQUETA: valor". NO es una lista fija de
# las 5 etiquetas vistas en Curso_1472 — sirve para cualquier curso que
# use la misma plantilla, con las etiquetas que tenga. Soporta tanto
# "<strong>ETIQUETA:</strong> valor" (valor fuera del <strong>) como
# "<strong>ETIQUETA: valor</strong>" (todo adentro, visto en la plantilla
# real sin llenar de HORA DE CLASES) — se concatena lo que haya de ambos
# lados por si el formato real terminara siendo mixto.
_STRONG_BLOCK_RE = re.compile(r"<strong>\s*(.*?)\s*</strong>\s*([^<]*)", re.IGNORECASE | re.DOTALL)
_PLACEHOLDER_RE = re.compile(r"^[\s\-_]*$")
_DOT_RUN_RE = re.compile(r"\.{3,}")  # "..........." = campo sin llenar, aunque tenga texto alrededor
# "(Chimbote o Huaraz)" = lista de opciones sin resolver, no un valor
# elegido — mismo principio que la corrida de puntos: un valor entre
# paréntesis con " o "/" u " entre palabras es genéricamente una
# plantilla de opciones, no dato real, en cualquier curso que la use.
_OPTIONS_PLACEHOLDER_RE = re.compile(r"^\(.*\b[oóu]\b.*\)$", re.IGNORECASE)


def parse_general_section_fields(course_id: int) -> Dict[str, str]:
    """Extrae los pares etiqueta:valor ya rellenados del resumen de la
    sección 'General' (sections[0].summary, ya incluido en el Course Map
    — sin llamada nueva a Moodle). Hoy (Curso_1472) viene vacío/plantilla
    sin llenar; el usuario confirmó que esto se va a personalizar por
    curso — cuando eso pase, esta función ya lo lee sin cambios. Un valor
    con una corrida de 3+ puntos (el patrón real de la plantilla sin
    llenar, ej. "DÍA............................") se descarta —
    nunca se reporta un placeholder como si fuera un dato real."""
    course = get_course_map(course_id)
    general = next((s for s in course.sections if s.section_number == 0), None)
    if not general or not general.summary:
        return {}
    result: Dict[str, str] = {}
    for strong_content, trailing in _STRONG_BLOCK_RE.findall(general.summary):
        if ":" not in strong_content:
            continue
        label, _, inline_value = strong_content.partition(":")
        label = label.strip().upper()
        value = f"{inline_value.strip()} {trailing.strip()}".strip()
        if (not label or not value or _PLACEHOLDER_RE.match(value)
                or _DOT_RUN_RE.search(value) or _OPTIONS_PLACEHOLDER_RE.match(value)):
            continue
        result[label] = value
    return result


def invalidate_course_map(course_id: int) -> None:
    """Llamado desde ledger.sync_curso_archivos() cuando detecta un
    archivo nuevo/modificado/eliminado para este curso — el docente subió
    algo, así que la próxima pregunta de navegación debe traer la
    estructura fresca en vez de esperar el TTL de 24h."""
    db.execute(
        "UPDATE course_map_cache SET invalidated_at = now() WHERE moodle_courseid = %s",
        (course_id,),
    )
