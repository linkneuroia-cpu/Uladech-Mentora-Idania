"""
docentes.py
===========
Cursos de un docente y validación de su login por enlace de Moodle.

Portado (reducido) de `Reporte de funcionalidades/output/dashboard/data_source.py`:
el ERP (MySQL, `view_matricula_cursos`) relaciona documento del docente con
`shortname_curso`; el catálogo de Moodle resuelve ese shortname a `courseid`.
Solo se lee el periodo más reciente (`MAX(Periodo)`).

Se cachea por documento ~10 min (el ERP casi no cambia intradía) y el
catálogo de Moodle ~1 h. Todo es de solo lectura. Si el ERP no está
configurado (`ERP_MYSQL_*` vacío), se falla de forma honesta.
"""
import threading
import time
from typing import Any, Dict, List, Optional

from settings import settings
import moodle_tools as mt

_CURSOS_TTL_SEG = 600      # 10 min
_CATALOGO_TTL_SEG = 3600   # 1 h

_cursos_cache: Dict[str, tuple] = {}
_catalogo_cache: Dict[str, Any] = {"expira": 0.0, "data": None}
_lock = threading.Lock()


def _mysql_conn():
    import pymysql  # import perezoso: la app arranca aunque no esté instalado

    if not settings.ERP_MYSQL_HOST:
        raise RuntimeError("ERP MySQL no configurado (faltan ERP_MYSQL_* en el .env)")
    return pymysql.connect(
        host=settings.ERP_MYSQL_HOST,
        port=settings.ERP_MYSQL_PORT,
        user=settings.ERP_MYSQL_USER,
        password=settings.ERP_MYSQL_PASSWORD,
        database=settings.ERP_MYSQL_DB,
        connect_timeout=10,
        charset="utf8mb4",
        cursorclass=pymysql.cursors.DictCursor,
    )


def _catalogo_moodle() -> Dict[str, Dict[str, Any]]:
    """shortname_curso -> {id, fullname, idnumber} del catálogo real de
    Moodle (core_course_get_courses). Cache en memoria ~1 h."""
    now = time.monotonic()
    with _lock:
        if _catalogo_cache["data"] is not None and _catalogo_cache["expira"] > now:
            return _catalogo_cache["data"]

    data = mt._moodle_ws_call("core_course_get_courses")
    catalogo: Dict[str, Dict[str, Any]] = {}
    if isinstance(data, list):
        for c in data:
            sn = c.get("shortname")
            if sn:
                catalogo[str(sn)] = {
                    "id": c.get("id"),
                    "fullname": c.get("fullname"),
                    "idnumber": c.get("idnumber", ""),
                }
    with _lock:
        _catalogo_cache["data"] = catalogo
        _catalogo_cache["expira"] = time.monotonic() + _CATALOGO_TTL_SEG
    return catalogo


def _fetch_cursos(documento: str) -> List[Dict[str, Any]]:
    conn = _mysql_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT MAX(Periodo) AS p FROM view_matricula_cursos")
            row = cur.fetchone()
            periodo = row.get("p") if row else None
            if periodo is None:
                return []
            cur.execute(
                "SELECT DISTINCT shortname_curso, nombre_curso, nombre_sede "
                "FROM view_matricula_cursos "
                "WHERE Periodo = %s AND documento_docente = %s",
                (periodo, documento),
            )
            rows = cur.fetchall() or []
    finally:
        conn.close()

    catalogo = _catalogo_moodle()
    por_courseid: Dict[int, Dict[str, Any]] = {}
    for r in rows:
        sn = r.get("shortname_curso")
        info = catalogo.get(str(sn)) if sn else None
        # Se descartan los cursos que no existen en Moodle (sin courseid).
        if not info or info.get("id") is None:
            continue
        cid = int(info["id"])
        por_courseid[cid] = {
            "courseid": cid,
            "shortname": sn,
            "nombre_curso": r.get("nombre_curso") or info.get("fullname") or f"Curso {cid}",
            "nombre_sede": r.get("nombre_sede"),
        }
    return sorted(por_courseid.values(), key=lambda c: (c.get("nombre_curso") or "").lower())


def cursos_de(documento: str) -> List[Dict[str, Any]]:
    """Cursos del docente en el periodo más reciente. Cacheado ~10 min por
    documento. Devuelve [{courseid, shortname, nombre_curso, nombre_sede}]."""
    documento = (documento or "").strip()
    if not documento:
        return []
    now = time.monotonic()
    with _lock:
        hit = _cursos_cache.get(documento)
        if hit and hit[0] > now:
            return hit[1]
    cursos = _fetch_cursos(documento)
    with _lock:
        _cursos_cache[documento] = (time.monotonic() + _CURSOS_TTL_SEG, cursos)
    return cursos


def validar_docente(username: str, userid: Any) -> Optional[Dict[str, Any]]:
    """Valida EN VIVO contra Moodle que `userid` corresponde de verdad a
    `username` (documento). Evita que alguien arme la URL con un documento
    ajeno y un userid inventado. Devuelve el usuario de Moodle o None."""
    try:
        uid = int(userid)
    except (TypeError, ValueError):
        return None
    data = mt._moodle_ws_call(
        "core_user_get_users_by_field", field="id", **{"values[0]": uid}
    )
    if isinstance(data, dict) and data.get("exception"):
        return None
    if not isinstance(data, list) or not data:
        return None
    user = data[0]
    if str(user.get("username", "")).strip().lower() != (username or "").strip().lower():
        return None
    return user
