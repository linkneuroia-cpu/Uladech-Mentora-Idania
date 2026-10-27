"""
auth.py
=======
Login del gestor (/gestor) contra la tabla `usuarios` en Postgres.
Contraseñas con hashlib.pbkdf2_hmac (sin dependencias nuevas de hashing) +
sal propia por usuario. Sesión vía cookie firmada (SessionMiddleware de
Starlette, montada en app.py) — sin JWT ni tokens de API para esto, es
solo para el panel interno.
"""
import hashlib
import hmac
import secrets
from typing import Any, Dict, Optional

from fastapi import HTTPException, Request

import db
from settings import settings

_PBKDF2_ITERATIONS = 260_000


def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt), _PBKDF2_ITERATIONS)
    return f"{salt}:{digest.hex()}"


def verify_password(password: str, password_hash: str) -> bool:
    try:
        salt, expected_hex = password_hash.split(":", 1)
    except ValueError:
        return False
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt), _PBKDF2_ITERATIONS)
    return hmac.compare_digest(digest.hex(), expected_hex)


def bootstrap_admin() -> None:
    """Si la tabla `usuarios` está vacía, crea el admin inicial desde
    ADMIN_USERNAME/ADMIN_PASSWORD (.env). Idempotente: no hace nada si ya
    hay al menos un usuario."""
    existing = db.fetch_one("SELECT id FROM usuarios LIMIT 1")
    if existing:
        return
    db.execute(
        "INSERT INTO usuarios (username, password_hash, is_admin) VALUES (%s, %s, TRUE)",
        (settings.ADMIN_USERNAME, hash_password(settings.ADMIN_PASSWORD)),
    )
    print(f"👤 Usuario admin inicial creado: '{settings.ADMIN_USERNAME}' (cambia ADMIN_PASSWORD en .env o la contraseña desde el gestor)")


def authenticate(username: str, password: str) -> Optional[Dict[str, Any]]:
    user = db.fetch_one("SELECT * FROM usuarios WHERE username = %s", (username,))
    # Los docentes (login por enlace de Moodle) no tienen contraseña: no
    # pueden entrar por el formulario usuario/contraseña.
    if not user or not user.get("password_hash"):
        return None
    if not verify_password(password, user["password_hash"]):
        return None
    db.execute("UPDATE usuarios SET last_login = now() WHERE id = %s", (user["id"],))
    return user


def create_user(
    username: str,
    password: Optional[str],
    is_admin: bool = False,
    rol: Optional[str] = None,
    documento: Optional[str] = None,
    moodle_userid: Optional[int] = None,
    nombre: Optional[str] = None,
) -> Dict[str, Any]:
    rol = rol or ("admin" if is_admin else "docente")
    if rol not in ("admin", "docente"):
        raise ValueError("rol debe ser 'admin' o 'docente'")
    is_admin = rol == "admin"
    existing = db.fetch_one("SELECT id FROM usuarios WHERE username = %s", (username,))
    if existing:
        raise ValueError(f"El usuario '{username}' ya existe")
    return db.execute_returning(
        "INSERT INTO usuarios (username, password_hash, is_admin, rol, documento, moodle_userid, nombre) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s) "
        "RETURNING id, username, is_admin, rol, documento, moodle_userid, nombre, created_at",
        (
            username,
            hash_password(password) if password else None,
            is_admin,
            rol,
            documento,
            moodle_userid,
            nombre,
        ),
    )


def upsert_docente(
    documento: str, moodle_userid: Optional[int], nombre: Optional[str]
) -> Dict[str, Any]:
    """Crea o actualiza la fila del docente que entró por el enlace de
    Moodle. Sin contraseña (solo puede entrar por ese enlace). `username` =
    documento para que la tabla siga teniendo una clave estable."""
    return db.execute_returning(
        "INSERT INTO usuarios (username, password_hash, is_admin, rol, documento, moodle_userid, nombre, last_login) "
        "VALUES (%s, NULL, FALSE, 'docente', %s, %s, %s, now()) "
        "ON CONFLICT (username) DO UPDATE SET "
        "rol = 'docente', is_admin = FALSE, documento = EXCLUDED.documento, "
        "moodle_userid = EXCLUDED.moodle_userid, nombre = EXCLUDED.nombre, last_login = now() "
        "RETURNING id, username, is_admin, rol, documento, moodle_userid, nombre",
        (documento, documento, moodle_userid, nombre),
    )


def list_users() -> list:
    return db.fetch_all(
        "SELECT id, username, is_admin, rol, documento, moodle_userid, nombre, created_at, last_login "
        "FROM usuarios ORDER BY id"
    )


def delete_user(user_id: int) -> None:
    db.execute("DELETE FROM usuarios WHERE id = %s", (user_id,))


# ── Dependencias FastAPI ────────────────────────────────────────────────

def get_current_user(request: Request) -> Dict[str, Any]:
    """Dependencia para proteger rutas del gestor. Lanza 401 si no hay
    sesión activa (el frontend redirige a /login)."""
    user = request.session.get("user")
    if not user:
        raise HTTPException(status_code=401, detail="No autenticado. Inicia sesión en /login.")
    return user


def require_admin(request: Request) -> Dict[str, Any]:
    user = get_current_user(request)
    if not (user.get("rol") == "admin" or user.get("is_admin")):
        raise HTTPException(status_code=403, detail="Requiere permisos de administrador.")
    return user


def require_docente_or_admin(request: Request) -> Dict[str, Any]:
    """Cualquier sesión válida puede entrar a /api/docente/* (el admin
    siempre pasa; el docente solo a sus propios cursos, verificado con
    require_docente_curso)."""
    return get_current_user(request)


def require_docente_curso(request: Request, courseid: Optional[int]) -> Dict[str, Any]:
    """403 si `courseid` no figura entre los cursos ACTUALES del docente. El
    admin pasa siempre.

    Los cursos se resuelven en vivo con `docentes.cursos_de` (caché de 10
    min) en vez de confiar en `session.courseids`: esa foto solo se guardaba
    al entrar por el enlace de Moodle, así que un docente que entró por
    contraseña (o cuya matrícula cambió en el ERP) recibía 403 en todo."""
    user = get_current_user(request)
    if user.get("rol") == "admin" or user.get("is_admin"):
        return user
    if courseid is None:
        raise HTTPException(
            status_code=403,
            detail="Este curso no figura entre tus cursos del periodo actual.",
        )
    documento = user.get("documento") or user.get("username")
    if not documento:
        raise HTTPException(
            status_code=403,
            detail="Este curso no figura entre tus cursos del periodo actual.",
        )
    try:
        import docentes

        cursos = docentes.cursos_de(documento)
    except Exception as e:
        print(f"[WARN] No se pudieron verificar los cursos de {documento}: {e}")
        raise HTTPException(
            status_code=503,
            detail="No se pudieron verificar tus cursos. Intenta nuevamente más tarde.",
        )
    permitidos = {int(c["courseid"]) for c in (cursos or []) if c.get("courseid") is not None}
    if int(courseid) not in permitidos:
        raise HTTPException(
            status_code=403,
            detail="Este curso no figura entre tus cursos del periodo actual.",
        )
    return user
