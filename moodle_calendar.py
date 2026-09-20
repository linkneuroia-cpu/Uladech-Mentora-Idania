"""
moodle_calendar.py
==================
Fechas de vencimiento reales de las actividades de un curso ("¿cuándo vence
la tarea X?", "¿qué se me vence esta semana?"). Ver Parte C del PLAN.md.

Estrategia de fuentes, en orden:
  1. Acceso directo de SOLO LECTURA a la base de datos de Moodle
     (`mdl_event`, eventtype='due'), cruzando `modulename='assign'` +
     `instance` con `mdl_assign` para el nombre real de la actividad. Es la
     vía fiable y la única disponible hoy en PREPRODUCCIÓN (ver
     settings.MOODLE_DB_*). En producción NO existe ese acceso.
  2. Si no hay DB configurada, intenta el webservice
     `core_calendar_get_action_events_by_course` (por si un admin de Moodle
     lo habilita en el futuro).
  3. Si ninguna fuente responde, devuelve status "unavailable" — el router
     degrada de forma honesta ("no tengo acceso a fechas de entrega") y
     NUNCA inventa una fecha.

No escribe nada en Moodle en ningún caso.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

import requests

from settings import settings
import course_map as cm

_MOODLE_HTML_RE = re.compile(r"<[^>]+>")


def _db_configured() -> bool:
    return bool(settings.MOODLE_DB_HOST and settings.MOODLE_DB_NAME and settings.MOODLE_DB_USER)


def _get_due_dates_from_db(course_id: int) -> List[Dict[str, Any]]:
    """Filas reales de mdl_event con eventtype='due' para el curso, con el
    nombre real de la actividad (mdl_assign). pymysql se importa de forma
    perezosa: si la dependencia no está instalada pero tampoco hay DB
    configurada, el módulo sigue importándose sin error."""
    import pymysql
    import pymysql.cursors

    conn = pymysql.connect(
        host=settings.MOODLE_DB_HOST,
        port=int(settings.MOODLE_DB_PORT or 3306),
        user=settings.MOODLE_DB_USER,
        password=settings.MOODLE_DB_PASSWORD,
        database=settings.MOODLE_DB_NAME,
        connect_timeout=8,
        cursorclass=pymysql.cursors.DictCursor,
    )
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT e.id, e.name AS event_name, e.timestart, e.eventtype,
                       e.modulename, e.instance, a.name AS assign_name
                FROM mdl_event e
                LEFT JOIN mdl_assign a
                       ON (e.modulename = 'assign' AND e.instance = a.id)
                WHERE e.courseid = %s AND e.eventtype = 'due'
                ORDER BY e.timestart
                """,
                (course_id,),
            )
            return list(cur.fetchall())
    finally:
        conn.close()


def _get_due_dates_from_ws(course_id: int, user_id: Optional[int]) -> Optional[List[Dict[str, Any]]]:
    """Intento por webservice (puede no estar autorizado/habilitado). None si
    no se pudo obtener una respuesta utilizable — nunca lanza hacia afuera."""
    url = f"{settings.MOODLE_URL}/webservice/rest/server.php"
    params = {
        "wstoken": settings.MOODLE_TOKEN,
        "wsfunction": "core_calendar_get_action_events_by_course",
        "moodlewsrestformat": "json",
        "courseid": course_id,
        "limitnum": 50,
    }
    try:
        r = requests.get(url, params=params, verify=False, timeout=15)
        r.raise_for_status()
        data = r.json()
    except Exception:
        return None
    if not isinstance(data, dict) or "exception" in data:
        return None
    events = data.get("events")
    if not isinstance(events, list):
        return None
    out = []
    for e in events:
        ts = e.get("timesort") or e.get("timestart")
        if not ts:
            continue
        out.append({
            "event_name": e.get("name") or "",
            "assign_name": None,
            "timestart": int(ts),
            "eventtype": "due",
            "modulename": e.get("modulename") or (e.get("action") or {}).get("name"),
        })
    return out


def get_due_dates(course_id: int, user_id: Optional[int] = None) -> Dict[str, Any]:
    """Punto de entrada. Devuelve {"status": "confident"|"unavailable",
    "source": ..., "events": [{"name", "due_epoch", "due_date"}]}.
    `events` ordenado por fecha real. Nunca inventa una fecha."""
    source = None
    raw: Optional[List[Dict[str, Any]]] = None

    if _db_configured():
        try:
            raw = _get_due_dates_from_db(course_id)
            source = "moodle_db"
        except Exception as e:
            print(f"⚠️ moodle_calendar: falló la consulta directa a la DB de Moodle ({e}) — se intenta el webservice")

    if raw is None:
        raw = _get_due_dates_from_ws(course_id, user_id)
        if raw is not None:
            source = "webservice"

    if raw is None:
        return {"status": "unavailable", "reason": "sin_fuente_de_fechas"}

    events = []
    for r in raw:
        ts = r.get("timestart")
        if not ts:
            continue
        name = (r.get("assign_name") or r.get("event_name") or "").strip()
        name = _MOODLE_HTML_RE.sub("", name).strip()
        events.append({
            "name": name or "Actividad sin nombre",
            "due_epoch": int(ts),
            "due_date": cm._format_date_es(int(ts)),
            "modulename": r.get("modulename") or "assign",
        })

    if not events:
        return {"status": "not_found", "reason": "curso_sin_fechas_de_entrega", "source": source}

    return {"status": "confident", "source": source, "events": events}
