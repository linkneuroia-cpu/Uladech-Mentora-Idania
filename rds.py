"""
rds.py
======
CRUD de RD (aulas) en Postgres, y su relación con colecciones de Qdrant
(colecciones) y asistentes. Cada RD es un registro propio (14 semillas
+ las que se agreguen), no una categoría que agrupa cursos.
"""
from typing import Any, Dict, List, Optional

import db

_UNSET = object()  # distingue "no viene en el request" de "viene como null"


def list_rds() -> List[Dict[str, Any]]:
    """Todas las RD, con conteo de colecciones reales, cursos registrados
    (con o sin colección) y asistentes asignados."""
    return db.fetch_all(
        """
        SELECT r.*,
               COUNT(DISTINCT c.id) FILTER (WHERE c.qdrant_collection_name IS NOT NULL) AS total_colecciones,
               COUNT(DISTINCT c.id) AS total_courseids,
               COUNT(DISTINCT a.id) AS total_asistentes
        FROM rds r
        LEFT JOIN colecciones c ON c.rd_id = r.id
        LEFT JOIN asistentes a ON a.rd_id = r.id
        GROUP BY r.id
        ORDER BY r.id
        """
    )


def get_rd(rd_id: int) -> Optional[Dict[str, Any]]:
    return db.fetch_one("SELECT * FROM rds WHERE id = %s", (rd_id,))


def get_rd_detail(rd_id: int) -> Optional[Dict[str, Any]]:
    rd = get_rd(rd_id)
    if not rd:
        return None
    rd["colecciones"] = db.fetch_all(
        "SELECT * FROM colecciones WHERE rd_id = %s ORDER BY id", (rd_id,)
    )
    rd["asistentes"] = db.fetch_all(
        "SELECT id, nombre, token, activo, created_at FROM asistentes WHERE rd_id = %s ORDER BY id",
        (rd_id,),
    )
    return rd


def create_rd(nombre: str, moodle_courseid: int, moodle_course_url: Optional[str] = None) -> Dict[str, Any]:
    """Crea la RD y registra su courseid inicial en la lista de cursos
    (colecciones, todavía sin colección) — igual que el seed de las 14
    RD originales."""
    rd = db.execute_returning(
        "INSERT INTO rds (nombre, moodle_courseid, moodle_course_url) VALUES (%s, %s, %s) RETURNING *",
        (nombre, moodle_courseid, moodle_course_url),
    )
    db.execute(
        "INSERT INTO colecciones (rd_id, moodle_courseid) VALUES (%s, %s)",
        (rd["id"], moodle_courseid),
    )
    return rd


def update_rd(
    rd_id: int,
    nombre: Optional[str] = None,
    moodle_courseid: Optional[int] = None,
    moodle_course_url: Optional[str] = None,
    dense_strategy: Any = _UNSET,
    sparse_strategy: Any = _UNSET,
    distance: Any = _UNSET,
) -> Dict[str, Any]:
    """dense_strategy/sparse_strategy/distance son el embedding "maestro" de
    la RD: todas sus colecciones deben compartirlo para ser intercambiables
    entre sí (mismo asistente, distinto courseid). Una vez que la RD YA
    tiene un dense_strategy fijado, no se puede cambiar si tiene colecciones
    reales (dejaría vectores incomparables entre colecciones de la misma
    RD). Excepción: si `existing["dense_strategy"]` todavía es NULL
    (colecciones creadas antes de que existiera este concepto de "colección
    madre"), sí se puede FIJARLO por primera vez aunque ya haya colecciones
    reales — no es un cambio de embedding, es solo declarar cuál ya se usó."""
    existing = get_rd(rd_id)
    if not existing:
        raise ValueError(f"La RD {rd_id} no existe")
    embedding_changed = (
        (dense_strategy is not _UNSET and dense_strategy != existing["dense_strategy"])
        or (sparse_strategy is not _UNSET and sparse_strategy != existing["sparse_strategy"])
        or (distance is not _UNSET and distance != existing["distance"])
    )
    if embedding_changed and existing["dense_strategy"] and has_real_collections(rd_id):
        raise ValueError(
            "No se puede cambiar el embedding de esta RD: ya tiene colecciones reales creadas "
            "y deben ser intercambiables entre sí. Elimínalas primero si de verdad necesitas cambiarlo."
        )
    return db.execute_returning(
        "UPDATE rds SET nombre=%s, moodle_courseid=%s, moodle_course_url=%s, "
        "dense_strategy=%s, sparse_strategy=%s, distance=%s, updated_at=now() WHERE id=%s RETURNING *",
        (
            nombre if nombre is not None else existing["nombre"],
            moodle_courseid if moodle_courseid is not None else existing["moodle_courseid"],
            moodle_course_url if moodle_course_url is not None else existing["moodle_course_url"],
            existing["dense_strategy"] if dense_strategy is _UNSET else dense_strategy,
            existing["sparse_strategy"] if sparse_strategy is _UNSET else sparse_strategy,
            existing["distance"] if distance is _UNSET else distance,
            rd_id,
        ),
    )


def has_real_collections(rd_id: int) -> bool:
    row = db.fetch_one(
        "SELECT 1 FROM colecciones WHERE rd_id = %s AND qdrant_collection_name IS NOT NULL LIMIT 1",
        (rd_id,),
    )
    return row is not None


def set_embedding(
    rd_id: int, dense_strategy: Optional[str], sparse_strategy: Optional[str], distance: Optional[str] = None
) -> Dict[str, Any]:
    """Fija el embedding maestro de la RD sin pasar por las validaciones de
    update_rd — usado internamente al crear la primera colección real (la
    "padre") de una RD que todavía no tenía nada fijado (ver app.py
    qdrant_create_collection)."""
    if not get_rd(rd_id):
        raise ValueError(f"La RD {rd_id} no existe")
    return db.execute_returning(
        "UPDATE rds SET dense_strategy=%s, sparse_strategy=%s, distance=%s, updated_at=now() WHERE id=%s RETURNING *",
        (dense_strategy, sparse_strategy, distance, rd_id),
    )


def delete_rd(rd_id: int) -> None:
    """Lanza ValueError si la RD no existe, o si tiene colecciones reales o
    asistentes asignados (FK ON DELETE RESTRICT) — hay que reasignarlos o
    eliminarlos primero. Los courseid sin colección (solo registrados en la
    lista) se limpian automáticamente, ya que no representan nada que perder."""
    if not get_rd(rd_id):
        raise ValueError(f"La RD {rd_id} no existe")
    import psycopg2

    db.execute(
        "DELETE FROM colecciones WHERE rd_id = %s AND qdrant_collection_name IS NULL", (rd_id,)
    )
    try:
        db.execute("DELETE FROM rds WHERE id = %s", (rd_id,))
    except psycopg2.errors.ForeignKeyViolation:
        raise ValueError(
            "No se puede eliminar: esta RD tiene colecciones o asistentes asignados. "
            "Reasígnalos o elimínalos primero."
        )


def link_collection(
    qdrant_collection_name: str,
    rd_id: Optional[int],
    moodle_courseid: int,
    vector_schema: Optional[str] = None,
) -> Dict[str, Any]:
    """Registra en Postgres que una colección de Qdrant pertenece a una RD
    + curso específico. Llamado desde POST /api/collections (creación).
    Si ese (rd_id, courseid) ya estaba registrado sin colección (vía
    add_courseid), "llena" esa fila en vez de crear una duplicada. Si ya
    tenía una colección DISTINTA asignada, bloquea — nunca se la roba
    silenciosamente (antes lo hacía vía ON CONFLICT ... DO UPDATE).
    `rd_id=None`: colección "normal" — no hay RD que validar/vincular
    formalmente, se registra solo el courseid como referencia.
    `vector_schema`: 'hybrid'/'legacy', tal cual viaja en
    CollectionCreateRequest — se persiste para poder mostrarlo después sin
    tener que volver a consultar Qdrant."""
    if rd_id is not None:
        if not get_rd(rd_id):
            raise ValueError(f"La RD {rd_id} no existe")
        existing = db.fetch_one(
            "SELECT qdrant_collection_name FROM colecciones WHERE rd_id = %s AND moodle_courseid = %s",
            (rd_id, moodle_courseid),
        )
        if existing and existing["qdrant_collection_name"] and existing["qdrant_collection_name"] != qdrant_collection_name:
            raise ValueError(
                f"El curso {moodle_courseid} de la RD {rd_id} ya tiene asignada la colección "
                f"'{existing['qdrant_collection_name']}'. Reasígnala o elimínala primero."
            )

    import psycopg2

    try:
        return db.execute_returning(
            "INSERT INTO colecciones (qdrant_collection_name, rd_id, moodle_courseid, vector_schema) "
            "VALUES (%s, %s, %s, %s) "
            "ON CONFLICT (rd_id, moodle_courseid) DO UPDATE SET "
            "qdrant_collection_name = EXCLUDED.qdrant_collection_name, "
            "vector_schema = EXCLUDED.vector_schema "
            "RETURNING *",
            (qdrant_collection_name, rd_id, moodle_courseid, vector_schema),
        )
    except psycopg2.errors.UniqueViolation:
        raise ValueError(
            f"La colección '{qdrant_collection_name}' ya está vinculada a otra RD/courseid."
        )


def reassign_collection(qdrant_collection_name: str, rd_id: Optional[int], moodle_courseid: int) -> Dict[str, Any]:
    """Cambia el RD/courseid de una colección ya vinculada. Usado por el
    listado de Colecciones para reasignar. `rd_id=None` la convierte en
    colección "normal" (independiente, sin RD)."""
    if rd_id is not None and not get_rd(rd_id):
        raise ValueError(f"La RD {rd_id} no existe")
    if not db.fetch_one(
        "SELECT 1 FROM colecciones WHERE qdrant_collection_name = %s", (qdrant_collection_name,)
    ):
        raise ValueError(f"La colección '{qdrant_collection_name}' no tiene un vínculo RD que reasignar")
    import psycopg2

    try:
        return db.execute_returning(
            "UPDATE colecciones SET rd_id = %s, moodle_courseid = %s "
            "WHERE qdrant_collection_name = %s RETURNING *",
            (rd_id, moodle_courseid, qdrant_collection_name),
        )
    except psycopg2.errors.UniqueViolation:
        raise ValueError(f"El curso {moodle_courseid} de la RD {rd_id} ya tiene asignada otra colección.")


def add_courseid(rd_id: int, moodle_courseid: int) -> Dict[str, Any]:
    """Registra un courseid más bajo una RD, todavía sin colección asignada
    — para que "Gestión RD" liste todos los cursos que replican la plantilla
    aunque aún no tengan contenido vectorizado."""
    if not get_rd(rd_id):
        raise ValueError(f"La RD {rd_id} no existe")
    import psycopg2

    try:
        return db.execute_returning(
            "INSERT INTO colecciones (rd_id, moodle_courseid) VALUES (%s, %s) RETURNING *",
            (rd_id, moodle_courseid),
        )
    except psycopg2.errors.UniqueViolation:
        raise ValueError(f"El curso {moodle_courseid} ya está registrado en esta RD.")


def remove_courseid_entry(entry_id: int) -> None:
    """Quita un courseid de la lista de una RD. Solo tiene sentido si esa
    fila todavía no tiene colección asignada (si la tiene, hay que
    reasignar o eliminar la colección primero)."""
    row = db.fetch_one("SELECT * FROM colecciones WHERE id = %s", (entry_id,))
    if not row:
        raise ValueError(f"El registro {entry_id} no existe")
    if row["qdrant_collection_name"]:
        raise ValueError(
            "Este curso ya tiene una colección asignada — reasígnala o elimínala antes de quitarlo."
        )
    db.execute("DELETE FROM colecciones WHERE id = %s", (entry_id,))


def unlink_collection(qdrant_collection_name: str) -> None:
    """Al eliminar una colección de Qdrant, el courseid sigue siendo válido
    para su RD (solo se quedó sin colección) — se limpia el vínculo en vez
    de borrar la fila, para no perder el registro del curso. vector_schema
    se limpia junto con el nombre: sin colección real, no hay esquema que
    reportar (si no, quedaría una fila diciendo "híbrida" sin colección)."""
    db.execute(
        "UPDATE colecciones SET qdrant_collection_name = NULL, vector_schema = NULL "
        "WHERE qdrant_collection_name = %s",
        (qdrant_collection_name,),
    )


def get_rd_for_collection(qdrant_collection_name: str) -> Optional[Dict[str, Any]]:
    """Colección -> su RD (con dense_strategy/sparse_strategy, el embedding
    maestro que todas las colecciones de esa RD deben compartir). `None` si
    la colección no está vinculada a ninguna RD."""
    return db.fetch_one(
        "SELECT r.* FROM colecciones c JOIN rds r ON r.id = c.rd_id WHERE c.qdrant_collection_name = %s",
        (qdrant_collection_name,),
    )


def resolve_collection(rd_id: int, moodle_courseid: int) -> Optional[str]:
    """(rd_id, courseid) -> nombre de colección de Qdrant, o None si no hay
    ninguna colección de esa RD asignada a ese curso, O si ese curso
    puntual está desactivado (`activo_asistente=False`) — un asistente
    ligado a una RD por defecto responde para CUALQUIER courseid que esa
    RD tenga registrado; este flag permite habilitar solo algunos cursos
    de una RD (ej. un rollout controlado) sin tocar los demás courseids
    de la misma RD ni el flag `activo` del asistente completo. Usado por
    la ruta pública del asistente — mismo `None` que ya dispara el 404
    "no hay colección asignada" de siempre, así que el estudiante ve el
    mismo mensaje sin importar si es un curso sin colección o uno
    puntualmente desactivado."""
    row = db.fetch_one(
        "SELECT qdrant_collection_name FROM colecciones WHERE rd_id = %s AND moodle_courseid = %s AND activo_asistente = TRUE",
        (rd_id, moodle_courseid),
    )
    return row["qdrant_collection_name"] if row else None


def set_curso_activo(entry_id: int, activo: bool) -> Dict[str, Any]:
    """Activa/desactiva un curso puntual de una RD (ver resolve_collection)
    — no borra ni toca la colección de Qdrant, solo si el asistente
    responde o no para ese courseid."""
    row = db.execute_returning(
        "UPDATE colecciones SET activo_asistente = %s WHERE id = %s RETURNING *", (activo, entry_id),
    )
    if not row:
        raise ValueError(f"El registro {entry_id} no existe")
    return row


def resolve_collection_normal(moodle_courseid: int) -> Optional[str]:
    """Igual que resolve_collection pero para una colección "normal" (sin
    RD) — usado por asistentes "normal", que resuelven directamente por su
    propio courseid en vez de RD+courseid."""
    row = db.fetch_one(
        "SELECT qdrant_collection_name FROM colecciones WHERE rd_id IS NULL AND moodle_courseid = %s",
        (moodle_courseid,),
    )
    return row["qdrant_collection_name"] if row else None


# ── Migración (re-sincronización automática con Moodle) ────────────────
# Ver migracion.py para el scheduler que consume `list_colecciones_periodicas`.

_MIGRACION_INTERVALOS_VALIDOS = (5, 60, 1440)  # 5 min / 1 hora / 1 día


def set_migracion_config(
    qdrant_collection_name: str, modo: str, intervalo_minutos: Optional[int]
) -> Dict[str, Any]:
    """Fija el modo de migración de una colección. `modo='periodica'`
    requiere `intervalo_minutos` en (5, 60, 1440) — valores fijos, no
    cualquier entero: un intervalo demasiado corto (p.ej. 1 minuto)
    saturaría a Moodle con el tick del scheduler cada 60s."""
    if modo not in ("manual", "periodica"):
        raise ValueError(f"modo inválido: '{modo}' (debe ser 'manual' o 'periodica')")
    if modo == "periodica":
        if intervalo_minutos not in _MIGRACION_INTERVALOS_VALIDOS:
            raise ValueError(
                f"intervalo_minutos debe ser uno de {_MIGRACION_INTERVALOS_VALIDOS} para modo 'periodica'"
            )
        existing = db.fetch_one(
            "SELECT moodle_courseid FROM colecciones WHERE qdrant_collection_name=%s",
            (qdrant_collection_name,),
        )
        if existing and not existing["moodle_courseid"]:
            raise ValueError(
                "Esta colección no tiene un curso de Moodle real — no se puede poner en modo 'periodica'"
            )
    else:
        intervalo_minutos = None

    row = db.execute_returning(
        "UPDATE colecciones SET migracion_modo=%s, migracion_intervalo_minutos=%s "
        "WHERE qdrant_collection_name=%s RETURNING *",
        (modo, intervalo_minutos, qdrant_collection_name),
    )
    if not row:
        raise ValueError(f"La colección '{qdrant_collection_name}' no existe")
    return row


def set_migracion_status(qdrant_collection_name: str, estado: str, resultado: Optional[str]) -> None:
    """Actualiza el estado de la última corrida (manual o automática) de
    migración de una colección. `estado`: 'corriendo' | 'ok' | 'error'."""
    db.execute(
        "UPDATE colecciones SET migracion_estado=%s, migracion_ultimo_resultado=%s, "
        "migracion_ultima_at=CASE WHEN %s = 'corriendo' THEN migracion_ultima_at ELSE now() END "
        "WHERE qdrant_collection_name=%s",
        (estado, resultado, estado, qdrant_collection_name),
    )


def list_colecciones_periodicas() -> List[Dict[str, Any]]:
    """Colecciones en modo 'periodica' que ya vencieron su intervalo desde
    la última corrida — la cola de trabajo del scheduler (migracion.py).
    Requiere moodle_courseid > 0: el sentinel 0 (colección "normal" sin
    curso de Moodle real, p.ej. un asistente institucional) no tiene nada
    que sincronizar. No excluye por rd_id — una colección "normal" con un
    courseid real también puede migrarse sola."""
    return db.fetch_all(
        "SELECT * FROM colecciones WHERE migracion_modo = 'periodica' AND moodle_courseid > 0 "
        "AND migracion_estado IS DISTINCT FROM 'corriendo' "
        "AND (migracion_ultima_at IS NULL "
        "     OR now() - migracion_ultima_at >= (migracion_intervalo_minutos || ' minutes')::interval)"
    )
