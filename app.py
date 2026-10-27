"""
app.py
======
FastAPI — un solo puerto (8100), tres asistentes:
  · /api/semi/*    → asistente semiautomático
  · /api/auto/*    → asistente automático (Moodle)
  · /api/update/*  → asistente de actualización
  · /api/          → endpoints compartidos (colecciones, job status, modelos)
"""

import asyncio
import concurrent.futures
import mimetypes
import re
import unicodedata
import uuid
from pathlib import Path
from contextlib import asynccontextmanager
from typing import Any, Dict, List, Optional

# En algunos Windows/Python el registro de mimetypes no conoce .webp
# (StaticFiles lo serviría como text/plain) — se registra explícito.
mimetypes.add_type("image/webp", ".webp")

from pydantic import BaseModel
from fastapi import (
    FastAPI, UploadFile, File, HTTPException,
    BackgroundTasks, Query, Request, Depends
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware
from settings import (
    settings, validate_settings, AVAILABLE_EMBEDDING_MODELS,
    STAGE_CATALOGS, get_strategy_info, strategy_is_usable, get_model_dimensions,
)

from definitions import (get_service,
    AutoPreviewRequest, AutoVectorizeRequest,
    UpdateVectorizeRequest,
    _sanitize_filename,
    get_moodle_user_info, get_moodle_course_name,
)
from core import (
    SemiAutoCore, AutoCore, UpdateCore,
    get_job, job_store,
)
import rag_pipeline
import credentials
import db
import auth
import docentes
import rds
import ledger
import assistants
import course_map
import campos_estructurados
import query_router
import moodle_calendar
from strategies import registry as strategy_registry
from strategies.runtime_config import get_runtime_config
from qdrant_admin import CollectionCreateRequest, CollectionUpdateRequest, get_qdrant_admin


# ═══════════════════════════════════════════════════════════════════
# STARTUP
# ═══════════════════════════════════════════════════════════════════

async def _temp_janitor_loop() -> None:
    """Barre archivos huérfanos de temp_docs/ (ver core.purge_stale_temp_files).
    Corre una vez al arrancar y luego cada hora. Es una red de seguridad: el
    cleanup normal borra cada archivo al terminar de vectorizarlo, pero un
    job interrumpido (reinicio del proceso) o un archivo que falló la
    validación antes de entrar al job quedaban en disco para siempre."""
    from core import purge_stale_temp_files
    while True:
        try:
            n = purge_stale_temp_files(max_age_hours=6.0)
            if n:
                print(f"🧹 Janitor temp_docs: {n} archivo(s) huérfano(s) eliminado(s)")
        except Exception as e:
            print(f"⚠️ Error en janitor de temp_docs: {e}")
        await asyncio.sleep(3600)


@asynccontextmanager
async def lifespan(app: FastAPI):
    print("🚀 Iniciando Sistema de Vectorización v2...")
    # Pool de threads más grande para el executor por defecto de asyncio
    # (usado por db.run, las llamadas a Moodle, y todo el pipeline RAG vía
    # run_in_executor): el default de Python (min(32, cpu_count+4)) puede
    # quedarse corto si varios alumnos consultan asistentes al mismo tiempo
    # — cada mensaje encadena varias llamadas bloqueantes (Postgres, Qdrant,
    # el LLM de generación). Son esperas de I/O, no de CPU, así que un pool
    # más grande es seguro y evita que las consultas se encolen entre sí.
    asyncio.get_event_loop().set_default_executor(
        concurrent.futures.ThreadPoolExecutor(max_workers=64)
    )
    validate_settings()
    scheduler_task = None
    janitor_task = None
    if db.check_connection():
        print("✅ Postgres conectado")
        auth.bootstrap_admin()
        credentials.load_all_into_settings()
        from migracion import migracion_scheduler_loop
        scheduler_task = asyncio.create_task(migracion_scheduler_loop())
        janitor_task = asyncio.create_task(_temp_janitor_loop())
    else:
        print("⚠️ Postgres no disponible al arrancar — el login y las funciones de Asistentes/RD fallarán hasta que se restablezca la conexión.")
    # Pre-carga el modelo por defecto al arrancar
    get_service()
    print("✅ Sistema listo")
    yield
    print("👋 Cerrando sistema...")
    if scheduler_task is not None:
        scheduler_task.cancel()
        try:
            await scheduler_task
        except asyncio.CancelledError:
            pass
    if janitor_task is not None:
        janitor_task.cancel()
        try:
            await janitor_task
        except asyncio.CancelledError:
            pass


app = FastAPI(
    title=settings.API_TITLE,
    version=settings.API_VERSION,
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Rutas públicas (sin sesión): login, estáticos, chat público de
# asistentes, y documentación/health. Todo lo demás requiere sesión. ──
_PUBLIC_PATH_PREFIXES = ("/login", "/logout", "/img", "/asistente", "/docs", "/redoc", "/openapi.json", "/api/health")

_CSRF_METODOS = {"POST", "PUT", "PATCH", "DELETE"}


@app.middleware("http")
async def security_headers_mw(request: Request, call_next):
    """Cabeceras para que Moodle (mod_secureurl, plugin idanIA) pueda
    embeber el gestor y el chat en un iframe — ver settings.MOODLE_FRAME_ANCESTOR.
    Sin X-Frame-Options (nunca se fijó ninguno acá ni en nginx — no hay
    nada que "quitar", solo no agregarlo). Se corre en TODAS las
    respuestas, no solo /login y /asistente, porque el panel del gestor
    también se embebe.

    Además, defensa mínima de CSRF por `Origin`: con la cookie en
    SameSite=None (necesaria para el iframe) se pierde la protección que
    daba SameSite=Lax, así que en los métodos que cambian datos se exige
    que, si la petición trae `Origin`, sea el propio origen de F1 — el
    fetch() del JS de F1 corriendo DENTRO del iframe lleva el origen de
    F1 (de donde se cargó ese script), no el de la página que lo embebe."""
    if request.method in _CSRF_METODOS:
        origin = request.headers.get("origin")
        if origin and origin.rstrip("/") != settings.PUBLIC_BASE_URL.rstrip("/"):
            return JSONResponse({"detail": "Origen no permitido."}, status_code=403)
    response = await call_next(request)
    response.headers["Content-Security-Policy"] = f"frame-ancestors {settings.MOODLE_FRAME_ANCESTOR}"
    response.headers["Permissions-Policy"] = (
        f'microphone=(self "{settings.MOODLE_FRAME_ANCESTOR}"), '
        f'clipboard-read=(self "{settings.MOODLE_FRAME_ANCESTOR}"), '
        f'clipboard-write=(self "{settings.MOODLE_FRAME_ANCESTOR}")'
    )
    return response


@app.middleware("http")
async def auth_gate(request: Request, call_next):
    # request.url.path incluye el root_path completo cuando la app corre
    # montada bajo un prefijo (ver asgi_app al final del archivo) — hay que
    # sacárselo para comparar contra _PUBLIC_PATH_PREFIXES, que están
    # definidas en términos de las rutas internas (sin prefijo).
    path = request.url.path
    root_path = request.scope.get("root_path", "")
    if root_path and path.startswith(root_path):
        path = path[len(root_path):] or "/"
    if path == "/" or path.startswith(_PUBLIC_PATH_PREFIXES):
        return await call_next(request)
    user = request.session.get("user")
    if not user:
        if path.startswith("/api/"):
            return JSONResponse({"detail": "No autenticado. Inicia sesión en /login."}, status_code=401)
        return RedirectResponse(f"{settings.resolved_url_prefix}/login")

    # Un docente solo puede usar su propio panel y sus endpoints. Se usa una
    # lista BLANCA (no negra) para que ningún endpoint de configuración quede
    # abierto por olvido al agregar rutas nuevas.
    rol = user.get("rol") or ("admin" if user.get("is_admin") else "docente")
    if rol == "docente":
        permitido = (
            path == "/gestor"
            or path == "/api/me"
            or path.startswith("/api/docente/")
            or path == "/logout"
            or path.startswith("/img")
        )
        if not permitido:
            if path.startswith("/api/"):
                return JSONResponse({"detail": "Requiere rol administrador."}, status_code=403)
            return RedirectResponse(f"{settings.resolved_url_prefix}/gestor")
    return await call_next(request)


# NOTA de orden: Starlette apila los middlewares registrados con
# add_middleware() de modo que el ÚLTIMO agregado queda MÁS externo (se
# ejecuta primero). SessionMiddleware debe ejecutarse ANTES que auth_gate
# (para que request.session ya exista cuando auth_gate lo lee), así que
# se registra DESPUÉS en el código — verificado en vivo: registrarlo antes
# rompía con "SessionMiddleware must be installed to access request.session".
app.add_middleware(
    SessionMiddleware,
    secret_key=settings.SESSION_SECRET_KEY,
    same_site=settings.SESSION_SAMESITE,
    https_only=settings.SESSION_HTTPS_ONLY,
)


# Rutas ABSOLUTAS (ancladas a este archivo), no relativas al cwd del
# proceso: una relativa como "frontend" se rompe si algo (p.ej. pix2tex,
# ver _call_preserving_cwd en definitions.py) hace os.chdir() en medio de
# una request concurrente — bug real reproducido en preproducción, tumbó
# el login del gestor para todo el proceso mientras corría una
# vectorización.
_APP_DIR = Path(__file__).resolve().parent
(_APP_DIR / "frontend").mkdir(exist_ok=True)
templates = Jinja2Templates(directory=str(_APP_DIR / "frontend"))
templates.env.globals["URL_PREFIX"] = settings.resolved_url_prefix

(_APP_DIR / "img").mkdir(exist_ok=True)
app.mount("/img", StaticFiles(directory=str(_APP_DIR / "img")), name="img")


# ═══════════════════════════════════════════════════════════════════
# HELPERS
# ═══════════════════════════════════════════════════════════════════

def _save_upload_to_temp(upload: UploadFile, content: bytes) -> tuple:
    """
    Guarda un UploadFile en temp_docs/ con nombre sanitizado para el sistema de archivos.
    """
    original_name = upload.filename
    safe_name = _sanitize_filename(original_name)
    dest = settings.TEMP_DIR / safe_name
    dest.write_bytes(content)
    return dest, original_name


def _resolve_model(model_name: Optional[str]) -> str:
    """
    Retorna el model_name validado. Si es None usa el default del .env.
    Lanza HTTP 400 si el nombre no está en el catálogo.
    """
    key = model_name or settings.EMBEDDING_MODEL
    if key not in AVAILABLE_EMBEDDING_MODELS:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Modelo '{key}' no está en el catálogo de modelos en español. "
                f"Usa GET /api/models para ver los disponibles."
            ),
        )
    return key


# ═══════════════════════════════════════════════════════════════════
# INTERFAZ WEB + LOGIN
# ═══════════════════════════════════════════════════════════════════

@app.get("/", include_in_schema=False)
async def root(request: Request):
    return RedirectResponse(
        f"{settings.resolved_url_prefix}/gestor" if request.session.get("user") else f"{settings.resolved_url_prefix}/login"
    )


@app.get("/gestor", response_class=HTMLResponse, tags=["Interfaz"])
async def home(request: Request):
    return templates.TemplateResponse("interfaz.html", {"request": request, "user": request.session.get("user")})


def _qparam(request: Request, name: str) -> str:
    """query param aceptando también la forma 'amp;<name>' que deja un
    '&amp;' literal sin decodificar (Moodle a veces escapa los '&')."""
    qp = request.query_params
    return (qp.get(name) or qp.get("amp;" + name) or "").strip()


@app.get("/login", response_class=HTMLResponse, tags=["Interfaz"])
async def login_page(request: Request):
    # Enlace directo desde una actividad URL de Moodle (plugin idanIA,
    # mod_secureurl): ?token=<token cifrado AES-256-GCM, scope="gestor">.
    # Reemplaza el viejo esquema de username/userid en claro en la URL (y
    # el DOCENTE_LOGIN_TOKEN, retirado) — ver secureurl.py/auth.verify_secureurl.
    # Un token de otro scope (p.ej. "activity", el del chat) o inválido/
    # vencido/reusado da 403 directo, sin caer al formulario de admin.
    token = _qparam(request, "token")
    if token:
        claims = auth.verify_secureurl(token, scope="gestor")
        username = claims.get("username")
        userid = claims.get("userid")
        if not username or not userid:
            raise HTTPException(403, "Enlace no válido o caducado. Vuelve a abrirlo desde Moodle.")
        loop = asyncio.get_event_loop()
        # Defensa adicional (ya no es la única barrera: el token ya probó
        # que Moodle lo emitió para este `username`/`userid`) — confirma en
        # vivo que el userid del token corresponde de verdad a ese username.
        moodle_user = await loop.run_in_executor(None, docentes.validar_docente, username, userid)
        if not moodle_user:
            raise HTTPException(403, "Enlace no válido o caducado. Vuelve a abrirlo desde Moodle.")
        try:
            cursos = await loop.run_in_executor(None, docentes.cursos_de, username)
        except Exception as e:
            print(f"⚠️ No se pudieron resolver los cursos del docente {username}: {e}")
            return templates.TemplateResponse(
                "login.html",
                {"request": request, "error": "No se pudieron consultar tus cursos. Intenta nuevamente más tarde."},
                status_code=503,
            )
        if not cursos:
            return templates.TemplateResponse(
                "login.html",
                {"request": request, "error": "No encontramos cursos tuyos en el periodo actual."},
            )
        row = await db.run(
            auth.upsert_docente, username, moodle_user.get("id"), moodle_user.get("fullname"),
        )
        request.session.clear()
        request.session["user"] = {
            "id": row["id"],
            "username": username,
            "is_admin": False,
            "rol": "docente",
            "documento": username,
            "moodle_userid": row.get("moodle_userid") or moodle_user.get("id"),
            "nombre": moodle_user.get("fullname") or username,
            "courseids": [c["courseid"] for c in cursos],
        }
        # Redirect sin el `token` en la URL: no queda en el historial del
        # navegador ni en logs de proxies más de lo necesario (de todas
        # formas es de un solo uso — JtiCache ya lo invalidó).
        return RedirectResponse(f"{settings.resolved_url_prefix}/gestor", status_code=303)
    if request.session.get("user"):
        return RedirectResponse(f"{settings.resolved_url_prefix}/gestor")
    return templates.TemplateResponse("login.html", {"request": request, "error": None})


class LoginRequest(BaseModel):
    username: str
    password: str


@app.post("/login", tags=["Interfaz"])
async def login_submit(request: Request, req: LoginRequest):
    user = await db.run(auth.authenticate, req.username, req.password)
    if not user:
        raise HTTPException(401, "Usuario o contraseña incorrectos.")
    request.session["user"] = {
        "id": user["id"],
        "username": user["username"],
        "is_admin": bool(user.get("is_admin")),
        "rol": user.get("rol") or ("admin" if user.get("is_admin") else "docente"),
        "documento": user.get("documento"),
        "moodle_userid": user.get("moodle_userid"),
        "nombre": user.get("nombre") or user["username"],
    }
    return {"success": True}


@app.post("/logout", tags=["Interfaz"])
async def logout(request: Request):
    request.session.clear()
    return {"success": True}


# ═══════════════════════════════════════════════════════════════════
# USUARIOS (solo administradores)
# ═══════════════════════════════════════════════════════════════════

class CreateUserRequest(BaseModel):
    username: str = ""
    password: Optional[str] = None
    is_admin: bool = False
    rol: Optional[str] = None            # 'admin' | 'docente'; default según is_admin
    documento: Optional[str] = None      # obligatorio para docentes (login por Moodle)
    moodle_userid: Optional[int] = None
    nombre: Optional[str] = None


@app.get("/api/usuarios", tags=["Usuarios"])
async def list_usuarios(user=Depends(auth.require_admin)):
    return {"usuarios": await db.run(auth.list_users)}


@app.post("/api/usuarios", status_code=201, tags=["Usuarios"])
async def create_usuario(req: CreateUserRequest, user=Depends(auth.require_admin)):
    rol = req.rol or ("admin" if req.is_admin else "docente")
    if rol not in ("admin", "docente"):
        raise HTTPException(400, "rol debe ser 'admin' o 'docente'")
    username = (req.username or req.documento or "").strip()
    if not username:
        raise HTTPException(400, "Falta el usuario (o el documento, para docentes).")
    if rol == "docente" and not (req.documento or "").strip():
        raise HTTPException(400, "Un docente se crea con su documento (login por enlace de Moodle).")
    try:
        return await db.run(
            auth.create_user,
            username,
            req.password,
            rol == "admin",
            rol,
            (req.documento or "").strip() or None,
            req.moodle_userid,
            req.nombre,
        )
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.delete("/api/usuarios/{user_id}", tags=["Usuarios"])
async def delete_usuario(user_id: int, user=Depends(auth.require_admin)):
    if user_id == user["id"]:
        raise HTTPException(400, "No puedes eliminar tu propio usuario mientras tienes sesión activa.")
    await db.run(auth.delete_user, user_id)
    return {"success": True}


# ═══════════════════════════════════════════════════════════════════
# SESIÓN / ROLES / PANEL DEL DOCENTE
# ═══════════════════════════════════════════════════════════════════

_TPL_IDANIA_PATH = _APP_DIR / "plantillas" / "asistente_idania.json"
# courseid -> job_id de la vectorización automática lanzada al crear el
# asistente. En memoria (el job_store también lo es): si el proceso
# reinicia, el job se pierde igual.
_docente_jobs: Dict[int, str] = {}


def _load_plantilla_idania() -> Dict[str, Any]:
    """Plantilla compartida por el wizard de admin, la creación desde el
    panel del docente y el spec e2e (plantillas/asistente_idania.json)."""
    try:
        import json as _json
        with open(_TPL_IDANIA_PATH, encoding="utf-8") as f:
            data = _json.load(f)
        data.setdefault("nombre", "Idania")
        return data
    except Exception as e:
        print(f"[WARN] No se pudo leer la plantilla de Idania ({_TPL_IDANIA_PATH}): {e}")
        return {"nombre": "Idania"}


def _is_admin(user: Dict[str, Any]) -> bool:
    return bool(user.get("rol") == "admin" or user.get("is_admin"))


@app.get("/api/me", tags=["Interfaz"])
async def api_me(request: Request):
    user = auth.get_current_user(request)
    if _is_admin(user):
        return {
            "username": user.get("username"),
            "nombre": user.get("nombre") or user.get("username"),
            "rol": "admin",
            "cursos": None,
        }
    documento = user.get("documento") or user.get("username")
    try:
        cursos = await db.run(docentes.cursos_de, documento)
    except Exception as e:
        print(f"[WARN] /api/me no pudo resolver cursos de {documento}: {e}")
        cursos = []
    return {
        "username": user.get("username"),
        "nombre": user.get("nombre"),
        "rol": "docente",
        "cursos": cursos,
    }


async def _create_normal_collection(
    name: str,
    courseid: int,
    dense_strategy: str,
    sparse_strategy: str,
    distance: str = "Cosine",
    description: Optional[str] = None,
) -> Dict[str, Any]:
    """Crea una colección "normal" (sin RD) con el embedding indicado y la
    registra en Postgres — mismo camino que POST /api/collections."""
    try:
        get_strategy_info("dense", dense_strategy)
    except ValueError as e:
        raise HTTPException(400, str(e))
    dense_size = get_model_dimensions(dense_strategy)
    try:
        result = get_qdrant_admin().create_collection(
            name=name,
            description=description,
            vector_schema="hybrid",
            dense_size=dense_size,
            distance=distance,
        )
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(500, str(e))
    try:
        await db.run(rds.link_collection, name, None, courseid, "hybrid")
    except ValueError as e:
        try:
            get_qdrant_admin().delete_collection(name, force=True)
        except Exception:
            pass
        raise HTTPException(400, str(e))
    return result


async def _vectorizar_curso_docente(
    courseid: int,
    collection_name: str,
    model_name: str,
    filenames: Optional[List[str]] = None,
) -> None:
    """Vectoriza en segundo plano los archivos indicados (o todo el curso
    soportado y no duplicado si `filenames` es None). Deja el job_id en
    `_docente_jobs` para que el panel muestre el progreso."""
    try:
        if filenames is None:
            loop = asyncio.get_event_loop()
            preview = await loop.run_in_executor(
                None, AutoCore.preview_course, courseid, collection_name, model_name
            )
            filenames = [
                f["filename"]
                for m in preview.get("modules", [])
                for f in m.get("files", [])
                if not f.get("is_duplicate") and f.get("file_type") != "other"
            ]
        filenames = [f for f in (filenames or []) if f]
        if not filenames:
            print(f"[INFO] Curso {courseid}: nada nuevo para vectorizar.")
            _docente_jobs.pop(courseid, None)
            return
        _docente_jobs[courseid] = "__starting__"
        job_info = await AutoCore.download_and_validate(
            courseid, collection_name, filenames, model_name, source_type="curso_propio"
        )
        _docente_jobs[courseid] = job_info["job_id"]
        try:
            await AutoCore.confirm_and_vectorize(job_info["job_id"])
        finally:
            # Al terminar (bien o mal) el panel vuelve a "Listo": el estado
            # "Vectorizando…" solo debe durar mientras corre el job.
            _docente_jobs.pop(courseid, None)
        print(f"[OK] Vectorizacion automatica del curso {courseid} terminada.")
    except Exception as e:
        _docente_jobs.pop(courseid, None)
        print(f"[ERROR] Vectorizacion automatica del curso {courseid} fallo: {e}")


@app.get("/api/docente/cursos", tags=["Docente"])
async def docente_listar_cursos(request: Request):
    user = auth.require_docente_or_admin(request)
    documento = user.get("documento") or (None if _is_admin(user) else user.get("username"))
    if not documento:
        return {"cursos": []}
    try:
        cursos = await db.run(docentes.cursos_de, documento)
    except Exception as e:
        raise HTTPException(503, f"No se pudieron consultar tus cursos: {e}")
    loop = asyncio.get_event_loop()
    out = []
    for c in cursos:
        cid = c["courseid"]
        coleccion = await db.run(rds.resolve_collection_para_curso, cid)
        puntos = 0
        if coleccion:
            try:
                info = await loop.run_in_executor(
                    None, lambda col=coleccion: get_qdrant_admin().get_collection_info(col)
                )
                puntos = getattr(info, "points_count", 0) or 0
            except Exception:
                puntos = 0
        # Asistente que sirve al curso: normal propio o el de su RD.
        a = await db.run(assistants.get_asistente_para_curso, cid)
        if a:
            es_rd = bool(a.get("rd_id"))
        else:
            es_rd = bool(await db.run(
                db.fetch_one,
                "SELECT 1 FROM colecciones WHERE moodle_courseid = %s AND rd_id IS NOT NULL LIMIT 1",
                (cid,),
            ))
        asistente = None
        if a:
            cfg = await db.run(assistants.get_curso_config, a["id"], cid)
            tipo = "rd" if es_rd else "normal"
            rd_nombre = None
            if tipo == "rd":
                rd = await db.run(rds.get_rd, a["rd_id"])
                rd_nombre = (rd or {}).get("nombre")
            asistente = {
                "id": a["id"],
                "nombre": a.get("nombre"),
                "token": a.get("token"),
                "url_publica": f"{settings.resolved_url_prefix}/asistente/{a.get('token')}",
                "activo": (cfg["activo"] if cfg else True),
                "global_activo": a.get("activo"),
                "func_notas": (cfg["func_notas"] if cfg else a.get("func_notas", True)),
                "func_documentos": (cfg["func_documentos"] if cfg else a.get("func_documentos", True)),
                "func_navegacion": (cfg["func_navegacion"] if cfg else a.get("func_navegacion", True)),
                "max_actividades_sesion": (
                    cfg["max_actividades_sesion"] if cfg else a.get("max_actividades_sesion", 10)
                ),
                "tipo": tipo,
                "rd_nombre": rd_nombre,
            }
        out.append({
            **c,
            "coleccion": coleccion,
            "puntos": puntos,
            "asistente": asistente,
            "tipo_curso": "rd" if es_rd else "normal",
            "job_vectorizacion": _docente_jobs.get(cid),
        })
    return {"cursos": out}


@app.get("/api/docente/cursos/{courseid}/documentos", tags=["Docente"])
async def docente_documentos(courseid: int, request: Request):
    auth.require_docente_curso(request, courseid)
    coleccion = await db.run(rds.resolve_collection_para_curso, courseid)
    if not coleccion:
        return {"collection_name": None, "documents": []}
    try:
        data = get_qdrant_admin().list_documents_in_collection(coleccion)
    except ValueError as e:
        raise HTTPException(404, str(e))
    except Exception as e:
        raise HTTPException(500, str(e))
    asistente = await db.run(assistants.get_asistente_para_curso, courseid)
    excluidos = set()
    if asistente:
        excluidos = set(await db.run(assistants.get_excluded_filenames, asistente["id"], courseid))
    docs = data.get("documents", [])
    for d in docs:
        d["excluido"] = d.get("filename") in excluidos
    return {
        "collection_name": coleccion,
        "total_documents": data.get("total_documents", len(docs)),
        "total_points": data.get("total_points", 0),
        "documents": docs,
    }


@app.get("/api/docente/cursos/{courseid}/asistente", tags=["Docente"])
async def docente_get_asistente(courseid: int, request: Request):
    """Estado + configuración POR CURSO del asistente que sirve al curso
    (normal o de su RD)."""
    auth.require_docente_curso(request, courseid)
    a = await db.run(assistants.get_asistente_para_curso, courseid)
    if not a:
        raise HTTPException(404, "El curso no tiene un asistente configurado.")
    cfg = await db.run(assistants.get_curso_config, a["id"], courseid)
    return {
        "asistente": a,
        "tipo": "rd" if a.get("rd_id") else "normal",
        "config": cfg or {
            "func_notas": a.get("func_notas", True),
            "func_documentos": a.get("func_documentos", True),
            "func_navegacion": a.get("func_navegacion", True),
            "max_actividades_sesion": a.get("max_actividades_sesion", 10),
            "activo": True,
        },
    }


class ExcluidosRequest(BaseModel):
    filenames: List[str] = []


@app.put("/api/docente/cursos/{courseid}/asistente/documentos-excluidos", tags=["Docente"])
async def docente_set_documentos_excluidos(
    courseid: int, req: ExcluidosRequest, request: Request
):
    user = auth.require_docente_curso(request, courseid)
    asistente = await db.run(assistants.get_asistente_para_curso, courseid)
    if not asistente:
        raise HTTPException(404, "El curso no tiene un asistente configurado.")
    coleccion = await db.run(rds.resolve_collection_para_curso, courseid)
    if not coleccion:
        raise HTTPException(404, "El curso no tiene una colección vectorizada.")
    try:
        data = get_qdrant_admin().list_documents_in_collection(coleccion)
    except Exception as e:
        raise HTTPException(500, str(e))
    validos = {d.get("filename") for d in data.get("documents", [])}
    invalidos = [f for f in req.filenames if f not in validos]
    if invalidos:
        raise HTTPException(
            400,
            f"Estos nombres no pertenecen a la colección: {', '.join(invalidos[:5])}",
        )
    saved = await db.run(
        assistants.set_excluded_filenames,
        asistente["id"], courseid, req.filenames, user.get("id"),
    )
    return {"excluidos": saved}


class DocenteAsistentePatch(BaseModel):
    max_actividades_sesion: Optional[int] = None
    func_notas: Optional[bool] = None
    func_documentos: Optional[bool] = None
    func_navegacion: Optional[bool] = None
    activo: Optional[bool] = None


_PERMITIDOS_DOCENTE_PATCH = {
    "max_actividades_sesion", "func_notas", "func_documentos", "func_navegacion", "activo",
}


@app.patch("/api/docente/cursos/{courseid}/asistente", tags=["Docente"])
async def docente_patch_asistente(
    courseid: int, req: DocenteAsistentePatch, request: Request
):
    """Override POR CURSO (no toca el asistente global ni la RD)."""
    user = auth.require_docente_curso(request, courseid)
    asistente = await db.run(assistants.get_asistente_para_curso, courseid)
    if not asistente:
        raise HTTPException(404, "El curso no tiene un asistente configurado.")
    fields = req.model_dump(exclude_unset=True)
    extra = set(fields) - _PERMITIDOS_DOCENTE_PATCH
    if extra:
        raise HTTPException(400, f"Campos no permitidos: {', '.join(sorted(extra))}")
    if "max_actividades_sesion" in fields:
        v = fields["max_actividades_sesion"]
        if v is None or not (1 <= int(v) <= 50):
            raise HTTPException(400, "max_actividades_sesion debe estar entre 1 y 50.")
    try:
        return await db.run(
            assistants.upsert_curso_config,
            asistente["id"], courseid, **fields, updated_by=user.get("id"),
        )
    except ValueError as e:
        raise HTTPException(400, str(e))


class DocenteAsistenteCreate(BaseModel):
    max_actividades_sesion: int = 10
    func_notas: bool = True
    func_documentos: bool = True
    func_navegacion: bool = True


@app.post("/api/docente/cursos/{courseid}/asistente", status_code=201, tags=["Docente"])
async def docente_crear_asistente(
    courseid: int, req: DocenteAsistenteCreate, request: Request
):
    user = auth.require_docente_curso(request, courseid)
    if not (1 <= req.max_actividades_sesion <= 50):
        raise HTTPException(400, "max_actividades_sesion debe estar entre 1 y 50.")
    existing = await db.run(assistants.get_asistente_para_curso, courseid)
    if existing:
        raise HTTPException(
            409,
            f"El curso {courseid} ya tiene un asistente (id {existing['id']}). Edítalo en vez de crear otro.",
        )
    # Curso que pertenece a una RD: el asistente y su colección los gestiona
    # la administración; no se crea un asistente "normal" paralelo.
    rd_row = await db.run(
        db.fetch_one,
        "SELECT rd_id FROM colecciones WHERE moodle_courseid = %s AND rd_id IS NOT NULL LIMIT 1",
        (courseid,),
    )
    if rd_row:
        raise HTTPException(
            409,
            "Este curso pertenece a una RD; el asistente lo gestiona la administración.",
        )

    coleccion = await db.run(rds.resolve_collection_normal, courseid)
    crear_coleccion = coleccion is None
    if crear_coleccion:
        coleccion = f"Curso_{courseid}"
        await _create_normal_collection(
            coleccion,
            courseid,
            settings.DOCENTE_DEFAULT_DENSE,
            settings.DOCENTE_DEFAULT_SPARSE,
            distance="Cosine",
            description=f"Colección normal del curso {courseid} (asistente Idania).",
        )

    tpl = _load_plantilla_idania()
    try:
        asistente = await db.run(
            assistants.create_asistente,
            nombre=tpl.get("nombre") or "Idania",
            rd_id=None,
            moodle_courseid=courseid,
            prompt_maestro=tpl.get("prompt_maestro"),
            created_by=user.get("id"),
            dense_strategy=settings.DOCENTE_DEFAULT_DENSE,
            sparse_strategy=settings.DOCENTE_DEFAULT_SPARSE,
            mensaje_bienvenida=tpl.get("mensaje_bienvenida"),
            mensaje_reencuentro=tpl.get("mensaje_reencuentro"),
            max_actividades_sesion=req.max_actividades_sesion,
            func_notas=req.func_notas,
            func_documentos=req.func_documentos,
            func_navegacion=req.func_navegacion,
        )
    except ValueError as e:
        if crear_coleccion:
            try:
                get_qdrant_admin().delete_collection(coleccion, force=True)
            except Exception:
                pass
        raise HTTPException(400, str(e))

    job_id = None
    if crear_coleccion:
        # Se lanza en segundo plano; `_docente_jobs[courseid]` se llena en
        # cuanto download_and_validate crea el job (el panel lo muestra).
        asyncio.create_task(
            _vectorizar_curso_docente(courseid, coleccion, settings.DOCENTE_DEFAULT_DENSE)
        )

    return {
        "asistente": asistente,
        "url_publica": f"{settings.resolved_url_prefix}/asistente/{asistente.get('token')}",
        "job_id": job_id,
    }


# ═══ CONOCIMIENTO idanIA (panel del docente) ═══════════════════════════════

def _strip_moodle_token(url: Optional[str]) -> Optional[str]:
    """Quita `token`/`wstoken` de un fileurl de Moodle antes de exponerlo."""
    if not url:
        return None
    try:
        from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
        p = urlsplit(url)
        q = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True) if k.lower() not in ("token", "wstoken")]
        return urlunsplit((p.scheme, p.netloc, p.path, urlencode(q), p.fragment))
    except Exception:
        return None


def _norm_order(text: Optional[str]) -> str:
    """Clave de comparación: sin mayúsculas, tildes ni puntuación."""
    text = unicodedata.normalize("NFKD", (text or "").lower())
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return " ".join(text.split())


def _html_a_texto(html: Optional[str]) -> str:
    """Resumen de sección de Moodle (HTML crudo) a texto plano legible."""
    if not html:
        return ""
    import html as _htmlmod
    # Los bloques se convierten en saltos de línea ANTES de extraer el texto;
    # los elementos inline (<b>, <span>) quedan unidos con un espacio.
    preparado = re.sub(
        r"<\s*(br|/p|/li|/div|/h[1-6]|/tr)\s*/?>", "\n", html, flags=re.IGNORECASE
    )
    try:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(preparado, "html.parser")
        for tag in soup(["script", "style"]):
            tag.decompose()
        text = soup.get_text(" ")
    except Exception:
        text = re.sub(r"<[^>]+>", " ", preparado)
    text = _htmlmod.unescape(text).replace("\xa0", " ")
    lineas = []
    for line in text.splitlines():
        line = re.sub(r"[ \t]+", " ", line).strip()
        if line:
            lineas.append(line)
    return "\n".join(lineas)


def _estado_archivo(f, ledger_row, excluidos):
    """Prioridad: excluido > vectorizado > no_soportado > pendiente."""
    fn = f["filename"]
    if fn in excluidos:
        return "excluido"
    if f.get("is_duplicate"):
        return "vectorizado"
    if (ledger_row or {}).get("estado") == "no_soportado":
        return "no_soportado"
    return "pendiente"


def _conocimiento_modules(preview, por_filename, excluidos):
    """Devuelve `modules` en el MISMO orden que Moodle (igual que
    /api/auto/preview), con `estado` por archivo y el `resumen`. Función
    pura (sin DB) para poder probarla aislada."""
    modules = []
    resumen = {"total": 0, "vectorizados": 0, "pendientes": 0, "no_soportados": 0, "excluidos": 0}
    mapa = {
        "vectorizado": "vectorizados", "pendiente": "pendientes",
        "no_soportado": "no_soportados", "excluido": "excluidos",
    }
    for m in preview.get("modules", []):
        files = []
        for f in m.get("files", []):
            estado = _estado_archivo(f, por_filename.get(f["filename"]), excluidos)
            files.append({
                "filename": f["filename"],
                "file_type": f.get("file_type"),
                "filesize": f.get("filesize"),
                "fileurl": _strip_moodle_token(f.get("fileurl")),
                "is_duplicate": bool(f.get("is_duplicate")),
                "existing_chunks": f.get("existing_chunks") or 0,
                "existing_date": f.get("existing_date"),
                "existing_source_type": f.get("existing_source_type"),
                "moodle_upload_date": f.get("moodle_upload_date"),
                "estado": estado,
            })
            resumen["total"] += 1
            resumen[mapa[estado]] += 1
        modules.append({
            "module_name": m.get("module_name") or "General",
            "resource_id": m.get("resource_id"),
            "files": files,
        })
    return modules, resumen


async def _conocimiento_payload(
    courseid: int, coleccion: Optional[str], asistente_id: Optional[int]
) -> Dict[str, Any]:
    """Todos los materiales del curso (Moodle), cruzados con el estado de
    vectorización y las exclusiones del curso, ordenados por semana y nombre."""
    loop = asyncio.get_event_loop()
    try:
        preview = await loop.run_in_executor(
            None, AutoCore.preview_course, courseid, coleccion, settings.DOCENTE_DEFAULT_DENSE
        )
    except Exception as e:
        raise HTTPException(502, f"No se pudo consultar Moodle: {e}")

    ledger_rows = await db.run(ledger.list_curso_archivos, coleccion) if coleccion else []
    por_filename = {r["filename"]: r for r in ledger_rows}
    excluidos = set()
    if asistente_id:
        excluidos = set(await db.run(assistants.get_excluded_filenames, asistente_id, courseid))

    modules, resumen = _conocimiento_modules(preview, por_filename, excluidos)

    semana_actual = None
    if coleccion:
        try:
            sw = await loop.run_in_executor(
                None, lambda: query_router.get_current_week_summary(coleccion, courseid)
            )
            if isinstance(sw, dict):
                semana_actual = sw.get("week_number")
        except Exception:
            semana_actual = None

    job = None
    jid = _docente_jobs.get(courseid)
    if jid == "__starting__":
        job = {"job_id": None, "estado": "iniciando", "progreso": 0}
    elif jid:
        j = get_job(jid) or {}
        job = {
            "job_id": jid,
            "estado": j.get("status") or "desconocido",
            "progreso": 1 if j.get("status") == "completado" else 0,
        }

    return {
        "collection_name": coleccion,
        "semana_actual": semana_actual,
        "resumen": resumen,
        "modules": modules,
        "job_vectorizacion": job,
    }


@app.get("/api/docente/cursos/{courseid}/conocimiento", tags=["Docente"])
async def docente_conocimiento(courseid: int, request: Request):
    auth.require_docente_curso(request, courseid)
    coleccion = await db.run(rds.resolve_collection_para_curso, courseid)
    asistente = await db.run(assistants.get_asistente_para_curso, courseid)
    data = await _conocimiento_payload(courseid, coleccion, asistente["id"] if asistente else None)
    return data


@app.get("/api/docente/cursos/{courseid}/pruebas", tags=["Docente"])
async def docente_pruebas(courseid: int, request: Request):
    """Últimas conversaciones de prueba de ese curso (pestaña Resumen)."""
    auth.require_docente_curso(request, courseid)
    asistente = await db.run(assistants.get_asistente_para_curso, courseid)
    if not asistente:
        return {"pruebas": []}
    return {
        "pruebas": await db.run(
            assistants.list_pruebas_por_curso, asistente["id"], courseid, 5
        )
    }


_FUNC_TIPOS_CALIFICABLES = {"assign", "quiz", "workshop", "lesson", "forum"}


def _navegacion_payload(est: Dict[str, Any], fechas: Dict[str, Any]) -> Dict[str, Any]:
    """Estructura la navegación del curso (semanas/actividades/fechas) para
    el panel docente y el admin. Función pura: no consulta Moodle."""
    fechas_status = (fechas or {}).get("status")
    fechas_message = (fechas or {}).get("message")

    vence_por_nombre: Dict[str, str] = {}
    proximas_entregas: List[Dict[str, Any]] = []
    if fechas_status != "unavailable":
        import time as _time
        ahora = _time.time()
        eventos = (fechas or {}).get("events") or []
        for e in eventos:
            key = _norm_order(e.get("name"))
            if key:
                vence_por_nombre.setdefault(key, e.get("due_date"))
        futuros = [e for e in eventos if (e.get("due_epoch") or 0) >= ahora]
        futuros.sort(key=lambda e: e.get("due_epoch") or 0)
        proximas_entregas = [
            {"nombre": e.get("name"), "vence": e.get("due_date"), "due_epoch": e.get("due_epoch")}
            for e in futuros[:3]
        ]

    semana_actual = est.get("semana_actual")
    secciones = []
    tipos_count: Dict[str, int] = {}
    for s in est.get("sections", []):
        acts = []
        for a in s.get("activities", []):
            tipo = a.get("modname")
            acts.append({
                "tipo": tipo,
                "tipo_label": query_router.MODNAME_LABELS.get(tipo, tipo),
                "nombre": a.get("name"),
                "url": a.get("url"),
                "visible": a.get("visible"),
                "vence": (
                    vence_por_nombre.get(_norm_order(a.get("name")))
                    if fechas_status != "unavailable" else None
                ),
            })
            tipos_count[tipo] = tipos_count.get(tipo, 0) + 1
        secciones.append({
            "numero": s.get("section_number"),
            "nombre": s.get("name"),
            "visible": s.get("visible", True),
            "es_actual": (s.get("section_number") == semana_actual),
            "resumen_texto": _html_a_texto(s.get("summary")),
            "resumen_html": s.get("summary") or "",
            "actividades": acts,
        })

    return {
        "funcionalidad": "navegacion",
        "semana_actual": semana_actual,
        "secciones": secciones,
        "proximas_entregas": proximas_entregas,
        "fechas_status": fechas_status,
        "fechas_message": fechas_message,
        "tipos": tipos_count,
    }


@app.get("/api/docente/cursos/{courseid}/funcionalidad/{nombre}", tags=["Docente"])
async def docente_funcionalidad(courseid: int, nombre: str, request: Request):
    """Contenido real que el asistente usará con cada funcionalidad."""
    auth.require_docente_curso(request, courseid)
    coleccion = await db.run(rds.resolve_collection_para_curso, courseid)

    if nombre == "navegacion":
        est = await _kn_estructura(courseid, coleccion)
        fechas = await _kn_fechas(courseid)
        return _navegacion_payload(est, fechas)

    if nombre == "notas":
        est = await _kn_estructura(courseid, coleccion)
        actividades = [
            {
                "semana": s["section_number"], "seccion": s["name"],
                "tipo": a["modname"], "nombre": a["name"], "url": a["url"],
            }
            for s in est["sections"]
            for a in s["activities"]
            if a["modname"] in _FUNC_TIPOS_CALIFICABLES
        ]
        return {
            "funcionalidad": "notas",
            "actividades": actividades,
            "nota": "Cada alumno ve solo sus propias notas; el asistente las consulta en vivo en Moodle.",
        }

    if nombre == "documentos":
        asistente = await db.run(assistants.get_asistente_para_curso, courseid)
        data = await _conocimiento_payload(
            courseid, coleccion, asistente["id"] if asistente else None
        )
        return {
            "funcionalidad": "documentos",
            "resumen": data["resumen"],
            "coleccion": coleccion,
        }

    raise HTTPException(404, "Funcionalidad desconocida (usa navegacion, notas o documentos).")


class VectorizarRequest(BaseModel):
    filenames: Optional[List[str]] = None


@app.post("/api/docente/cursos/{courseid}/vectorizar", tags=["Docente"])
async def docente_vectorizar(courseid: int, req: VectorizarRequest, request: Request):
    auth.require_docente_curso(request, courseid)
    if _docente_jobs.get(courseid):
        raise HTTPException(409, "Ya hay una vectorización en curso para este curso.")

    coleccion = await db.run(rds.resolve_collection_para_curso, courseid)
    if not coleccion:
        coleccion = f"Curso_{courseid}"
        await _create_normal_collection(
            coleccion,
            courseid,
            settings.DOCENTE_DEFAULT_DENSE,
            settings.DOCENTE_DEFAULT_SPARSE,
            distance="Cosine",
            description=f"Colección normal del curso {courseid} (asistente Idania).",
        )

    asistente = await db.run(assistants.get_asistente_para_curso, courseid)
    if req.filenames is None:
        data = await _conocimiento_payload(courseid, coleccion, asistente["id"] if asistente else None)
        filenames = [
            a["filename"]
            for m in data["modules"]
            for a in m["files"]
            if a["estado"] == "pendiente"
        ]
    else:
        filenames = [f for f in req.filenames if f]

    if not filenames:
        return {"job_id": None, "encolados": 0}

    # Registro inmediato para que la UI vea el estado antes de que el job
    # real exista (download_and_validate lo crea después).
    _docente_jobs[courseid] = "__starting__"
    asyncio.create_task(
        _vectorizar_curso_docente(courseid, coleccion, settings.DOCENTE_DEFAULT_DENSE, filenames)
    )
    return {"job_id": None, "encolados": len(filenames)}


@app.get("/api/docente/cursos/{courseid}/vectorizar/estado", tags=["Docente"])
async def docente_vectorizar_estado(courseid: int, request: Request):
    auth.require_docente_curso(request, courseid)
    jid = _docente_jobs.get(courseid)
    if not jid:
        return {"job_id": None, "estado": "inactivo", "progreso": 0}
    if jid == "__starting__":
        return {"job_id": None, "estado": "iniciando", "progreso": 0}
    job = get_job(jid) or {}
    return {
        "job_id": jid,
        "estado": job.get("status") or "desconocido",
        "progreso": 1 if job.get("status") == "completado" else 0,
        "message": job.get("message"),
    }


# ═══════════════════════════════════════════════════════════════════
# COMPARTIDOS
# ═══════════════════════════════════════════════════════════════════

@app.get("/api/health", tags=["Sistema"])
async def health():
    return {
        "status":        "healthy",
        "version":       settings.API_VERSION,
        "default_model": settings.EMBEDDING_MODEL,
        "docs":          f"http://{settings.API_HOST}:{settings.API_PORT}/docs",
    }


@app.get("/api/models", tags=["Sistema"])
async def list_models():
    """
    Lista el catálogo completo de modelos de embeddings disponibles,
    todos especializados o validados para texto en español.

    Retorna:
      · default      — modelo activo según .env
      · models[]     — catálogo con nombre, dimensiones y descripción
    """
    return {
        "default": settings.EMBEDDING_MODEL,
        "models": [
            {
                "name":        name,
                "dimensions":  info["dimensions"],
                "description": info["description"],
                "use_prefix":  info["use_prefix"],
            }
            for name, info in AVAILABLE_EMBEDDING_MODELS.items()
        ],
    }


@app.get("/api/collections", tags=["Compartido"])
async def list_collections(
    detail: bool = Query(
        False,
        description=(
            "Si es true, retorna el listado enriquecido (esquema, puntos, "
            "estado, descripción) desde el gestor de administración de "
            "Qdrant en vez de solo los nombres."
        ),
    ),
):
    """Lista todas las colecciones disponibles en Qdrant."""
    if detail:
        from qdrant_admin import get_qdrant_admin

        collections = [c.model_dump() for c in get_qdrant_admin().list_collections()]
        # LEFT JOIN: una colección "normal" (rd_id NULL) no tiene fila en
        # rds — con INNER JOIN quedaba afuera del listado por completo.
        links = await db.run(
            db.fetch_all,
            "SELECT c.id AS link_id, c.qdrant_collection_name, c.rd_id, c.moodle_courseid, "
            "c.vector_schema AS vector_schema_db, r.nombre AS rd_nombre, "
            "c.migracion_modo, c.migracion_intervalo_minutos, c.migracion_estado, "
            "c.migracion_ultima_at, c.migracion_ultimo_resultado "
            "FROM colecciones c LEFT JOIN rds r ON r.id = c.rd_id "
            "WHERE c.qdrant_collection_name IS NOT NULL",
        )
        links_by_name = {l["qdrant_collection_name"]: l for l in links}
        for c in collections:
            link = links_by_name.get(c["name"])
            c["link_id"] = link["link_id"] if link else None
            c["rd_id"] = link["rd_id"] if link else None
            c["rd_nombre"] = link["rd_nombre"] if link else None
            c["moodle_courseid"] = link["moodle_courseid"] if link else None
            # vector_schema_db = lo persistido en Postgres; c["vector_schema"]
            # (ya viene del model_dump de QdrantAdminManager) es el valor VIVO
            # leído de Qdrant — se mantienen separados para poder detectar
            # desincronización en vez de esconderla.
            c["vector_schema_db"] = link["vector_schema_db"] if link else None
            c["migracion_modo"] = link["migracion_modo"] if link else "manual"
            c["migracion_intervalo_minutos"] = link["migracion_intervalo_minutos"] if link else None
            c["migracion_estado"] = link["migracion_estado"] if link else None
            c["migracion_ultima_at"] = link["migracion_ultima_at"] if link else None
            c["migracion_ultimo_resultado"] = link["migracion_ultimo_resultado"] if link else None
        return {"collections": collections}
    service = get_service()
    return {"collections": service.list_collections()}


# ═══════════════════════════════════════════════════════════════════
# GESTIÓN QDRANT — administración de colecciones (dense y dense+sparse)
# Absorbe el proyecto standalone "gestion quadrant": mismas 8 rutas,
# apuntando a qdrant_admin.QdrantAdminManager en vez de un microservicio
# aparte, con soporte adicional para el esquema híbrido.
# ═══════════════════════════════════════════════════════════════════

class CollectionMigracionRequest(BaseModel):
    migracion_modo: str  # 'manual' | 'periodica'
    migracion_intervalo_minutos: Optional[int] = None  # requerido si modo='periodica' — 5, 60 o 1440


@app.patch("/api/collections/{collection_name}/migracion", tags=["Gestión Qdrant"])
async def qdrant_set_migracion(collection_name: str, req: CollectionMigracionRequest):
    """Fija el modo de migración (re-sincronización automática con Moodle)
    de una colección: 'manual' (nunca se toca sola) o 'periodica' (el
    scheduler la revisa cada `migracion_intervalo_minutos`, ver
    migracion.py)."""
    if not get_qdrant_admin().collection_exists(collection_name):
        raise HTTPException(404, f"Colección '{collection_name}' no existe")
    try:
        return await db.run(
            rds.set_migracion_config, collection_name, req.migracion_modo, req.migracion_intervalo_minutos
        )
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.post("/api/collections/{collection_name}/migracion/ejecutar", tags=["Gestión Qdrant"])
async def qdrant_ejecutar_migracion(collection_name: str, background_tasks: BackgroundTasks):
    """Dispara la migración de esta colección ahora mismo (funciona en
    cualquier modo, manual o periódica) — requiere que la colección tenga
    un courseid real de Moodle detrás."""
    row = await db.run(
        db.fetch_one, "SELECT * FROM colecciones WHERE qdrant_collection_name=%s", (collection_name,)
    )
    if not row:
        raise HTTPException(404, f"Colección '{collection_name}' no existe")
    if not row.get("moodle_courseid"):
        raise HTTPException(400, "Esta colección no tiene un curso de Moodle real — no se puede migrar")
    if row.get("migracion_estado") == "corriendo":
        raise HTTPException(409, "Esta colección ya tiene una migración en curso")
    import migracion

    # ejecutar_migracion_en_hilo (sync, no la coroutine ejecutar_migracion
    # directa): BackgroundTasks corre las tareas sync en un threadpool
    # aparte automáticamente — así esta migración manual tampoco bloquea
    # el event loop principal mientras corre, igual que la automática del
    # scheduler (ver migracion.py).
    background_tasks.add_task(migracion.ejecutar_migracion_en_hilo, row)
    return {"status": "iniciado", "collection": collection_name}

@app.post("/api/collections", status_code=201, tags=["Gestión Qdrant"])
async def qdrant_create_collection(req: CollectionCreateRequest):
    """Crea una colección en Qdrant. `vector_schema`: "hybrid" (dense+sparse,
    requerido para el pipeline RAG configurable) o "legacy" (vector único,
    compatible con el flujo anterior). `moodle_courseid` siempre es
    obligatorio; `rd_id` es opcional y determina el tipo de colección:

    - Sin `rd_id` ("normal"): independiente, ningún asistente la encuentra.
      Esquema/distancia/embedding los elige el cliente libremente.
    - Con `rd_id` y la RD SIN embedding maestro fijado ("padre"): es su
      primera colección real — `dense_strategy` es obligatorio y queda
      fijado (junto a `sparse_strategy`/`distance`) como el embedding
      maestro de esa RD de ahí en adelante.
    - Con `rd_id` y la RD YA con embedding maestro fijado ("hija"): hereda
      dense/sparse/distance de la RD — ignora lo que venga en el request
      para esos campos, todas las colecciones de una RD deben ser
      intercambiables entre sí.

    Si el RD/curso no existe, ya tiene otra colección asignada, o falla
    fijar el embedding maestro, la colección recién creada en Qdrant se
    revierte para no dejar huérfanos."""
    dense_size = req.dense_size
    distance = req.distance
    lock_in_embedding: Optional[tuple] = None  # (dense_strategy, sparse_strategy, distance) a fijar tras crear
    rd = None

    if req.rd_id is not None:
        rd = await db.run(rds.get_rd, req.rd_id)
        if not rd:
            raise HTTPException(400, f"La RD {req.rd_id} no existe")
        if req.moodle_courseid is None:
            raise HTTPException(400, "moodle_courseid es obligatorio para una colección con RD")

    # Colección "normal" (sin RD) sin curso de Moodle detrás (p.ej. un
    # asistente institucional tipo FAQ) -> sentinel 0 ("sin curso"). Sigue
    # siendo un INTEGER NOT NULL en `colecciones`, así que 0 evita una
    # migración de esquema mientras deja la intención clara.
    moodle_courseid = req.moodle_courseid if req.moodle_courseid is not None else 0

    if req.vector_schema == "hybrid":
        if rd and rd.get("dense_strategy"):
            # Hija: hereda todo de la RD, ignora lo que venga en el request.
            dense_size = get_model_dimensions(rd["dense_strategy"])
            distance = rd.get("distance") or req.distance
        else:
            # Padre (RD sin embedding fijado) o Normal (sin RD): el cliente elige.
            if not req.dense_strategy:
                raise HTTPException(
                    400,
                    "Se requiere 'dense_strategy' para esta colección (es la primera "
                    "de su RD, o no tiene RD).",
                )
            try:
                get_strategy_info("dense", req.dense_strategy)
            except ValueError as e:
                raise HTTPException(400, str(e))
            dense_size = get_model_dimensions(req.dense_strategy)
            sparse_strategy = req.sparse_strategy or settings.SPARSE_EMBEDDING_STRATEGY
            if rd:  # Padre: queda fijado como embedding maestro de la RD.
                lock_in_embedding = (req.dense_strategy, sparse_strategy, distance)

    try:
        result = get_qdrant_admin().create_collection(
            name=req.name,
            description=req.description,
            vector_schema=req.vector_schema,
            dense_size=dense_size,
            distance=distance,
        )
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(500, str(e))

    try:
        await db.run(rds.link_collection, req.name, req.rd_id, moodle_courseid, req.vector_schema)
        if lock_in_embedding:
            await db.run(rds.set_embedding, req.rd_id, *lock_in_embedding)
    except ValueError as e:
        try:
            get_qdrant_admin().delete_collection(req.name, force=True)
        except Exception:
            pass
        raise HTTPException(400, str(e))

    return result


@app.get("/api/collections/{collection_name}", tags=["Gestión Qdrant"])
async def qdrant_get_collection(collection_name: str):
    """Información detallada de una colección (esquema, puntos, estado)."""
    try:
        return get_qdrant_admin().get_collection_info(collection_name)
    except ValueError as e:
        raise HTTPException(404, str(e))
    except Exception as e:
        raise HTTPException(500, str(e))


@app.patch("/api/collections/{collection_name}", tags=["Gestión Qdrant"])
async def qdrant_update_collection(collection_name: str, req: CollectionUpdateRequest):
    """Actualiza la descripción de una colección."""
    try:
        return get_qdrant_admin().update_collection(collection_name, req.description)
    except ValueError as e:
        raise HTTPException(404, str(e))
    except Exception as e:
        raise HTTPException(500, str(e))


class CollectionRdReassignRequest(BaseModel):
    rd_id: Optional[int] = None
    moodle_courseid: int


@app.patch("/api/collections/{collection_name}/rd", tags=["Gestión Qdrant"])
async def qdrant_reassign_collection_rd(collection_name: str, req: CollectionRdReassignRequest):
    """Reasigna a qué RD/courseid pertenece una colección ya creada."""
    if not get_qdrant_admin().collection_exists(collection_name):
        raise HTTPException(404, f"Colección '{collection_name}' no existe")
    try:
        return await db.run(rds.reassign_collection, collection_name, req.rd_id, req.moodle_courseid)
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.delete("/api/collections/{collection_name}", tags=["Gestión Qdrant"])
async def qdrant_delete_collection(
    collection_name: str,
    force: bool = Query(False, description="Forzar eliminación aunque tenga vectores"),
):
    """Elimina una colección de Qdrant y su vínculo con la RD (si tenía)."""
    try:
        result = get_qdrant_admin().delete_collection(collection_name, force=force)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(500, str(e))
    await db.run(rds.unlink_collection, collection_name)
    return result


@app.post("/api/collections/{collection_name}/clear", tags=["Gestión Qdrant"])
async def qdrant_clear_collection(collection_name: str):
    """Elimina todos los vectores de una colección, conservando su
    configuración (esquema legacy o híbrido)."""
    try:
        return get_qdrant_admin().clear_collection(collection_name)
    except ValueError as e:
        raise HTTPException(404, str(e))
    except Exception as e:
        raise HTTPException(500, str(e))


@app.get("/api/collections/{collection_name}/exists", tags=["Gestión Qdrant"])
async def qdrant_collection_exists(collection_name: str):
    """Verifica si una colección existe."""
    return {
        "collection_name": collection_name,
        "exists": get_qdrant_admin().collection_exists(collection_name),
    }


@app.get("/api/collections/{collection_name}/documents", tags=["Gestión Qdrant"])
async def qdrant_list_documents(collection_name: str):
    """Lista los documentos únicos vectorizados en una colección."""
    try:
        return get_qdrant_admin().list_documents_in_collection(collection_name)
    except ValueError as e:
        raise HTTPException(404, str(e))
    except Exception as e:
        raise HTTPException(500, str(e))


@app.delete("/api/collections/{collection_name}/documents/{filename:path}", tags=["Gestión Qdrant"])
async def qdrant_delete_document(collection_name: str, filename: str):
    """Elimina todos los chunks de un documento (por filename) de una colección."""
    try:
        return get_qdrant_admin().delete_document_by_filename(collection_name, filename)
    except ValueError as e:
        raise HTTPException(404, str(e))
    except Exception as e:
        raise HTTPException(500, str(e))


@app.get("/api/collections/{collection_name}/documents/{filename:path}/chunks", tags=["Gestión Qdrant"])
async def qdrant_document_chunks(collection_name: str, filename: str):
    """Detalle de cada chunk (texto y metadata) de un documento — para
    inspección puntual, a diferencia del resumen agregado de /documents."""
    try:
        return get_qdrant_admin().get_document_chunks(collection_name, filename)
    except ValueError as e:
        raise HTTPException(404, str(e))
    except Exception as e:
        raise HTTPException(500, str(e))


@app.patch("/api/collections/{collection_name}/documents/{filename:path}/source-type", tags=["Gestión Qdrant"])
async def qdrant_set_document_source_type(
    collection_name: str, filename: str,
    source_type: str = Query(..., description="'curso_propio' o 'bibliografia'"),
):
    """Recategoriza un documento ya vectorizado (curso_propio/bibliografia)
    sin tener que re-vectorizarlo — actualiza el payload de todos sus chunks."""
    try:
        return get_qdrant_admin().set_document_source_type(collection_name, filename, source_type)
    except ValueError as e:
        raise HTTPException(404, str(e))
    except Exception as e:
        raise HTTPException(500, str(e))


# ═══════════════════════════════════════════════════════════════════
# RD (aulas)  /api/rds/*
# ═══════════════════════════════════════════════════════════════════

class RdCreateRequest(BaseModel):
    nombre: str
    moodle_courseid: int
    moodle_course_url: Optional[str] = None


class RdUpdateRequest(BaseModel):
    nombre: Optional[str] = None
    moodle_courseid: Optional[int] = None
    moodle_course_url: Optional[str] = None
    # Embedding maestro de la RD — solo se puede cambiar mientras no tenga
    # colecciones reales creadas (ver rds.update_rd).
    dense_strategy: Optional[str] = None
    sparse_strategy: Optional[str] = None
    distance: Optional[str] = None


@app.get("/api/rds", tags=["RD"])
async def list_rds_endpoint():
    """Todas las RD, con conteo de colecciones y asistentes asignados."""
    return {"rds": await db.run(rds.list_rds)}


@app.get("/api/rds/{rd_id}", tags=["RD"])
async def get_rd_endpoint(rd_id: int):
    """Detalle de una RD: sus colecciones y asistentes asignados."""
    detail = await db.run(rds.get_rd_detail, rd_id)
    if not detail:
        raise HTTPException(404, f"La RD {rd_id} no existe")
    return detail


@app.post("/api/rds", status_code=201, tags=["RD"])
async def create_rd_endpoint(req: RdCreateRequest):
    # Si no la mandan (o falló la consulta a Moodle en el navegador), se
    # deriva del MOODLE_URL del entorno — nunca queda una RD sin URL.
    url = req.moodle_course_url or f"{settings.MOODLE_URL.rstrip('/')}/course/view.php?id={req.moodle_courseid}"
    return await db.run(rds.create_rd, req.nombre, req.moodle_courseid, url)


@app.patch("/api/rds/{rd_id}", tags=["RD"])
async def update_rd_endpoint(rd_id: int, req: RdUpdateRequest):
    fields = req.model_dump(exclude_unset=True)
    strategy_kwargs = {k: fields[k] for k in ("dense_strategy", "sparse_strategy", "distance") if k in fields}
    try:
        return await db.run(
            rds.update_rd, rd_id, req.nombre, req.moodle_courseid, req.moodle_course_url, **strategy_kwargs
        )
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.delete("/api/rds/{rd_id}", tags=["RD"])
async def delete_rd_endpoint(rd_id: int):
    try:
        await db.run(rds.delete_rd, rd_id)
        return {"success": True}
    except ValueError as e:
        raise HTTPException(400, str(e))


class RdCourseidRequest(BaseModel):
    moodle_courseid: int


class RdCursoActivoRequest(BaseModel):
    activo: bool


@app.post("/api/rds/{rd_id}/courseids", status_code=201, tags=["RD"])
async def add_rd_courseid_endpoint(rd_id: int, req: RdCourseidRequest):
    """Registra un courseid más bajo esta RD (todavía sin colección)."""
    try:
        return await db.run(rds.add_courseid, rd_id, req.moodle_courseid)
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.delete("/api/rds/courseids/{entry_id}", tags=["RD"])
async def remove_rd_courseid_endpoint(entry_id: int):
    """Quita un courseid de la lista de una RD (solo si no tiene colección asignada)."""
    try:
        await db.run(rds.remove_courseid_entry, entry_id)
        return {"success": True}
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.patch("/api/rds/courseids/{entry_id}/activo", tags=["RD"])
async def set_rd_courseid_activo_endpoint(entry_id: int, req: RdCursoActivoRequest):
    """Activa/desactiva el asistente para un courseid puntual de una RD,
    sin tocar los demás cursos de esa misma RD ni el `activo` del
    asistente completo — ver rds.resolve_collection()."""
    try:
        return await db.run(rds.set_curso_activo, entry_id, req.activo)
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.get("/api/moodle/curso/{courseid}", tags=["RD"])
async def moodle_curso_endpoint(courseid: int):
    """Nombre real del curso de Moodle — usado por el wizard de asistentes
    para confirmar el Course ID antes de asociarlo a una RD, y por el
    formulario de creación de RD para autocompletar nombre/URL."""
    loop = asyncio.get_event_loop()
    fullname = await loop.run_in_executor(None, get_moodle_course_name, courseid)
    if not fullname:
        raise HTTPException(404, f"Moodle no reconoce el Course ID {courseid}.")
    return {
        "id": courseid,
        "fullname": fullname,
        # URL pública del curso, armada con el MOODLE_URL del entorno —
        # mismo patrón que las RD sembradas en db_schema.sql. Así el admin
        # no la escribe a mano y no se equivoca de dominio entre dev/prod.
        "url": f"{settings.MOODLE_URL.rstrip('/')}/course/view.php?id={courseid}",
    }


@app.get("/api/moodle/curso/{courseid}/estructura", tags=["RD"])
async def moodle_curso_estructura_endpoint(
    courseid: int, refrescar: bool = Query(False),
    collection_name: Optional[str] = Query(None, description="Si se pasa, incluye los campos indexados (auto+manual) de esa colección."),
):
    """Course Map real del curso (secciones + actividades reales de
    Moodle, con cmid/url/visibilidad) — expone course_map.get_course_map()
    (ya usada internamente por el Query Router/Índice de Campos
    Estructurados desde el gestor, en vez de solo desde scripts. Sirve
    para cualquier curso, no solo 1472/1541. `refrescar=true` ignora la
    caché y vuelve a consultar Moodle en vivo."""
    loop = asyncio.get_event_loop()
    try:
        course = await loop.run_in_executor(
            None, lambda: course_map.get_course_map(courseid, force_refresh=refrescar)
        )
    except Exception as e:
        raise HTTPException(502, f"Error consultando Moodle: {e}")
    try:
        campos_generales = await loop.run_in_executor(
            None, lambda: course_map.parse_general_section_fields(courseid)
        )
    except Exception:
        campos_generales = {}
    campos_indexados: List[Dict[str, Any]] = []
    if collection_name:
        try:
            campos_indexados = await loop.run_in_executor(
                None, lambda: campos_estructurados.list_general_section_fields(collection_name)
            )
        except Exception:
            campos_indexados = []
    return {
        "course_id": course.course_id,
        "fetched_at": course.fetched_at,
        "campos_generales": campos_generales,
        "campos_generales_indexados": campos_indexados,
        "sections": [
            {
                "section_number": s.section_number,
                "name": s.name,
                "visible": s.visible,
                "activities": [
                    {
                        "cmid": a.cmid, "name": a.name, "modname": a.modname,
                        "url": a.url, "visible": a.visible,
                        "completion_tracking": a.completion_tracking,
                    }
                    for a in s.activities
                ],
            }
            for s in course.sections
        ],
    }


async def _kn_fechas(courseid: int) -> Dict[str, Any]:
    """Fechas de entrega reales de Moodle (helper compartido admin/docente)."""
    return await asyncio.get_event_loop().run_in_executor(
        None, lambda: moodle_calendar.get_due_dates(courseid)
    )


async def _kn_estructura(courseid: int, coleccion: Optional[str]) -> Dict[str, Any]:
    """Course Map + estado de cada actividad (helper compartido admin/docente)."""
    loop = asyncio.get_event_loop()
    try:
        course = await loop.run_in_executor(
            None, lambda: course_map.get_course_map(courseid)
        )
    except Exception as e:
        raise HTTPException(502, f"Error consultando Moodle: {e}")
    cache = await db.run(
        db.fetch_one,
        "SELECT fetched_at, invalidated_at, ttl_minutes FROM course_map_cache WHERE moodle_courseid = %s",
        (courseid,),
    )
    archivos = await db.run(ledger.list_curso_archivos, coleccion) if coleccion else []
    asistente = await db.run(assistants.get_asistente_para_curso, courseid)
    excluidos = set()
    if asistente:
        excluidos = set(await db.run(assistants.get_excluded_filenames, asistente["id"], courseid))
    por_resource = {r["resource_id"]: r for r in archivos if r.get("resource_id")}

    def _considerado(a):
        reg = por_resource.get(a.cmid)
        if reg:
            if reg.get("filename") in excluidos:
                return "Excluido por el docente"
            if reg.get("estado") == "vectorizado":
                return "Vectorizado"
            if reg.get("estado") == "no_soportado":
                return "No soportado"
            return "No vectorizado"
        return "Solo estructura"

    semana_actual = None
    if coleccion:
        try:
            sw = await loop.run_in_executor(
                None, lambda: query_router.get_current_week_summary(coleccion, courseid)
            )
            if isinstance(sw, dict):
                semana_actual = sw.get("week_number")
        except Exception:
            semana_actual = None

    return {
        "coleccion": coleccion,
        "fetched_at": getattr(course, "fetched_at", None),
        "cache": cache,
        "semana_actual": semana_actual,
        "sections": [
            {
                "section_number": s.section_number,
                "name": s.name,
                "visible": s.visible,
                "summary": getattr(s, "summary", "") or "",
                "activities": [
                    {
                        "cmid": a.cmid,
                        "name": a.name,
                        "modname": a.modname,
                        "url": a.url,
                        "visible": a.visible,
                        "uservisible": getattr(a, "uservisible", None),
                        "considerado": _considerado(a),
                    }
                    for a in s.activities
                ],
            }
            for s in course.sections
        ],
    }


@app.get("/api/admin/cursos/{courseid}/conocimiento", tags=["Admin"])
async def admin_conocimiento(
    courseid: int,
    seccion: str = Query("estructura", description="estructura|general|campos|fechas|archivos|autoridad"),
    collection_name: Optional[str] = Query(None),
    document: Optional[str] = Query(None),
    q: Optional[str] = Query(None),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    user=Depends(auth.require_admin),
):
    """Visor de lo que el sistema SABE y USA de un curso (solo admin). Una
    sección por llamada, para que el fallo de una no tumbe las demás."""
    loop = asyncio.get_event_loop()
    coleccion = collection_name or await db.run(rds.resolve_collection_para_curso, courseid)

    if seccion == "general":
        campos = await loop.run_in_executor(
            None, lambda: course_map.parse_general_section_fields(courseid)
        )
        indexados: List[Dict[str, Any]] = []
        if coleccion:
            try:
                indexados = await loop.run_in_executor(
                    None, lambda: campos_estructurados.list_general_section_fields(coleccion)
                )
            except Exception:
                indexados = []
        return {
            "seccion": "general", "course_id": courseid, "coleccion": coleccion,
            "campos_generales": campos, "campos_generales_indexados": indexados,
        }

    if seccion == "fechas":
        data = await _kn_fechas(courseid)
        return {"seccion": "fechas", "course_id": courseid, **data}

    if seccion == "archivos":
        archivos = await db.run(ledger.list_curso_archivos, coleccion) if coleccion else []
        return {
            "seccion": "archivos", "course_id": courseid,
            "coleccion": coleccion, "archivos": archivos,
        }

    if seccion == "campos":
        if not coleccion:
            return {"seccion": "campos", "course_id": courseid, "coleccion": None, "total": 0, "rows": []}
        try:
            data = await loop.run_in_executor(
                None,
                lambda: campos_estructurados.list_rows(
                    coleccion, document=document, q=q, limit=limit, offset=offset
                ),
            )
        except Exception as e:
            raise HTTPException(500, f"Error leyendo el índice de campos: {e}")
        return {"seccion": "campos", "course_id": courseid, "coleccion": coleccion, **data}

    if seccion == "autoridad":
        rows = await db.run(
            db.fetch_all,
            "SELECT source_type, prioridad, activo, updated_at FROM fuente_autoridad ORDER BY prioridad",
        )
        return {"seccion": "autoridad", "course_id": courseid, "autoridad": rows}

    # estructura (default): desde course_map_cache, no de Moodle en vivo.
    data = await _kn_estructura(courseid, coleccion)
    return {"seccion": "estructura", "course_id": courseid, **data}


@app.post("/api/moodle/curso/{courseid}/sincronizar-general", tags=["RD"])
async def moodle_sincronizar_general_endpoint(courseid: int, collection_name: str = Query(...)):
    """Dispara manualmente campos_estructurados.sync_general_section_fields()
    — mismo procedimiento que ya corre automáticamente cuando
    ledger.sync_curso_archivos() detecta cambios, expuesto acá para
    poder correrlo bajo demanda desde el gestor sin esperar un sync de
    archivos."""
    loop = asyncio.get_event_loop()
    try:
        result = await loop.run_in_executor(
            None, lambda: campos_estructurados.sync_general_section_fields(collection_name, courseid)
        )
    except Exception as e:
        raise HTTPException(502, f"Error sincronizando campos de la sección General: {e}")
    return result


class CampoGeneralRequest(BaseModel):
    collection_name: str
    field: str
    value: str


@app.post("/api/moodle/curso/{courseid}/campo-general", tags=["RD"])
async def set_campo_general_endpoint(courseid: int, req: CampoGeneralRequest):
    """Crea/actualiza A MANO un campo de "Datos generales del aula" (Parte
    2.5). Genérico: el nombre del campo es libre. Se guarda con
    source_type='seccion_general_manual', así NO lo borra un re-sync
    automático desde Moodle."""
    try:
        return await db.run(
            campos_estructurados.set_general_section_field,
            req.collection_name, req.field, req.value,
        )
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.delete("/api/moodle/curso/{courseid}/campo-general/{field}", tags=["RD"])
async def delete_campo_general_endpoint(courseid: int, field: str, collection_name: str = Query(...)):
    """Borra UN campo cargado a mano (nunca toca los automáticos)."""
    return await db.run(
        campos_estructurados.delete_general_section_field, collection_name, field,
    )


# ═══════════════════════════════════════════════════════════════════
# ASISTENTES (CRUD)  /api/asistentes/*
# ═══════════════════════════════════════════════════════════════════

_ASISTENTE_STRATEGY_FIELDS = (
    "etl_document_strategy",
    "etl_audio_strategy",
    "contextual_strategy",
    "rerank_strategy",
    "generation_strategy",
    "dense_strategy",
    "sparse_strategy",
)


class AsistenteCreateRequest(BaseModel):
    nombre: str
    # rd_id=None -> asistente "normal": moodle_courseid pasa a ser
    # obligatorio (resuelve su única colección directo por courseid).
    rd_id: Optional[int] = None
    moodle_courseid: Optional[int] = None
    prompt_maestro: Optional[str] = None
    # Config RAG propia del asistente — None/omitido = usa el valor por
    # defecto del sistema. etl_document/etl_audio/contextual también se
    # consultan automáticamente al vectorizar (ver core._build_pipeline_config).
    # dense/sparse: con RD, son el embedding maestro de la RD
    # (rds.dense_strategy/sparse_strategy) y estos campos se ignoran. Sin
    # RD ("normal"), SÍ tienen efecto — una colección "normal" no persiste
    # su propio embedding en ningún otro lado, así que deben coincidir con
    # el que se usó para crear/elegir la colección de este asistente (ver
    # asistente_enviar_mensaje).
    etl_document_strategy: Optional[str] = None
    etl_audio_strategy: Optional[str] = None
    contextual_strategy: Optional[str] = None
    rerank_strategy: Optional[str] = None
    generation_strategy: Optional[str] = None
    dense_strategy: Optional[str] = None
    sparse_strategy: Optional[str] = None
    # None/omitido = "soy {nombre}. Pregúntame lo que necesites sobre el
    # curso." — se agrega después de "Hola {nombre del alumno}, ".
    mensaje_bienvenida: Optional[str] = None
    # Se muestra (no se guarda) al reabrir una conversación con historial.
    mensaje_reencuentro: Optional[str] = None
    max_actividades_sesion: int = 10
    func_notas: bool = True
    func_documentos: bool = True
    func_navegacion: bool = True
    # None/omitido = se genera uno aleatorio como antes; si se pasa, se usa
    # tal cual (rechaza si ya lo tiene otro asistente).
    token: Optional[str] = None


class AsistenteUpdateRequest(BaseModel):
    nombre: Optional[str] = None
    rd_id: Optional[int] = None
    moodle_courseid: Optional[int] = None
    prompt_maestro: Optional[str] = None
    activo: Optional[bool] = None
    # Si el campo no viene en el body, la etapa queda intacta; si viene como
    # null explícito, esa etapa vuelve a usar el valor por defecto (ver
    # assistants.update_asistente — usa model_dump(exclude_unset=True)).
    etl_document_strategy: Optional[str] = None
    etl_audio_strategy: Optional[str] = None
    contextual_strategy: Optional[str] = None
    rerank_strategy: Optional[str] = None
    generation_strategy: Optional[str] = None
    dense_strategy: Optional[str] = None
    sparse_strategy: Optional[str] = None
    mensaje_bienvenida: Optional[str] = None
    mensaje_reencuentro: Optional[str] = None
    max_actividades_sesion: Optional[int] = None
    func_notas: Optional[bool] = None
    func_documentos: Optional[bool] = None
    func_navegacion: Optional[bool] = None
    # Cambiar el token invalida cualquier enlace ya compartido con el viejo.
    token: Optional[str] = None


@app.get("/api/asistentes", tags=["Asistentes"])
async def list_asistentes_endpoint():
    return {"asistentes": await db.run(assistants.list_asistentes)}


@app.get("/api/asistentes/plantilla", tags=["Asistentes"])
async def get_plantilla_asistente(user=Depends(auth.require_admin)):
    """Plantilla "Idania" (prompt y saludos) para precargar el wizard/edición."""
    return _load_plantilla_idania()


@app.get("/api/asistentes/{asistente_id}", tags=["Asistentes"])
async def get_asistente_endpoint(asistente_id: int):
    a = await db.run(assistants.get_asistente, asistente_id)
    if not a:
        raise HTTPException(404, f"El asistente {asistente_id} no existe")
    return a


@app.post("/api/asistentes", status_code=201, tags=["Asistentes"])
async def create_asistente_endpoint(req: AsistenteCreateRequest, user=Depends(auth.get_current_user)):
    strategy_kwargs = {field: getattr(req, field) for field in _ASISTENTE_STRATEGY_FIELDS}
    strategy_kwargs["mensaje_bienvenida"] = req.mensaje_bienvenida
    strategy_kwargs["mensaje_reencuentro"] = req.mensaje_reencuentro
    strategy_kwargs["max_actividades_sesion"] = req.max_actividades_sesion
    strategy_kwargs["func_notas"] = req.func_notas
    strategy_kwargs["func_documentos"] = req.func_documentos
    strategy_kwargs["func_navegacion"] = req.func_navegacion
    try:
        return await db.run(
            assistants.create_asistente,
            nombre=req.nombre,
            rd_id=req.rd_id,
            moodle_courseid=req.moodle_courseid,
            prompt_maestro=req.prompt_maestro,
            created_by=user["id"],
            token=req.token,
            **strategy_kwargs,
        )
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.patch("/api/asistentes/{asistente_id}", tags=["Asistentes"])
async def update_asistente_endpoint(asistente_id: int, req: AsistenteUpdateRequest):
    if not await db.run(assistants.get_asistente, asistente_id):
        raise HTTPException(404, f"El asistente {asistente_id} no existe")
    fields = req.model_dump(exclude_unset=True)
    # rd_id/moodle_courseid entran por exclude_unset igual que las
    # estrategias: sin esto es imposible mandarlos como null explícito (=
    # volver el asistente "normal"), porque el default None del modelo es
    # indistinguible de "no vino en el request".
    kwargs = {
        k: fields[k]
        for k in (
            *_ASISTENTE_STRATEGY_FIELDS, "mensaje_bienvenida", "mensaje_reencuentro",
            "max_actividades_sesion", "rd_id", "moodle_courseid",
            "func_notas", "func_documentos", "func_navegacion",
        )
        if k in fields
    }
    try:
        return await db.run(
            assistants.update_asistente,
            asistente_id,
            nombre=req.nombre,
            prompt_maestro=req.prompt_maestro,
            activo=req.activo,
            token=req.token,
            **kwargs,
        )
    except ValueError as e:
        # Con el pre-check de existencia arriba, cualquier ValueError acá es
        # de validación ("La RD X no existe", "requiere un Course ID",
        # "token ya en uso") — 400, no 404.
        raise HTTPException(400, str(e))


@app.get("/api/asistentes/{asistente_id}/calificaciones", tags=["Asistentes"])
async def get_asistente_calificaciones_endpoint(asistente_id: int):
    """Desglose de la calificación de utilidad por curso de Moodle, más el
    total general — el listado de asistentes solo muestra el agregado."""
    a = await db.run(assistants.get_asistente, asistente_id)
    if not a:
        raise HTTPException(404, f"El asistente {asistente_id} no existe")
    data = await db.run(assistants.get_calificaciones_por_curso, asistente_id)
    return {"asistente_id": asistente_id, "nombre": a["nombre"], **data}


@app.delete("/api/asistentes/{asistente_id}", tags=["Asistentes"])
async def delete_asistente_endpoint(asistente_id: int):
    try:
        await db.run(assistants.delete_asistente, asistente_id)
        return {"success": True}
    except ValueError as e:
        raise HTTPException(404, str(e))


# ═══════════════════════════════════════════════════════════════════
# ASISTENTE PÚBLICO (sin login)  /asistente/{token}
# ═══════════════════════════════════════════════════════════════════

def _build_asistente_context(
    asistente: Dict[str, Any], rd: Dict[str, Any], student_fullname: Optional[str] = None
) -> str:
    """Contexto de identidad que se antepone al `prompt_maestro` del
    asistente antes de mandarlo como `extra_system_prompt` al generador.
    Sin esto, el LLM no sabe su propio nombre ni que ya se presentó (la
    burbuja de saludo la pinta el frontend, nunca llega como mensaje al
    LLM) y termina reintroduciéndose o confundiendo su nombre con el del
    estudiante en cada respuesta."""
    partes = [
        f'Te llamas "{asistente["nombre"]}" y eres el tutor virtual de este curso.',
    ]
    if student_fullname:
        partes.append(f"Estás conversando con el estudiante {student_fullname}.")
    if rd.get("nombre"):
        partes.append(f"El curso es: {rd['nombre']}.")
    partes.append(
        "Ya te presentaste al estudiante al inicio de la conversación (fuera de este "
        "historial), así que NO vuelvas a saludar ni a repetir tu presentación en tus "
        "respuestas: continúa la conversación de forma natural y directa, como si ya se "
        "conocieran."
    )
    if asistente.get("prompt_maestro"):
        partes.append(asistente["prompt_maestro"])
    return "\n".join(partes)


def _safe_week_info(collection_name: Optional[str], courseid: int) -> Optional[Dict[str, Any]]:
    """Info de semana actual para personalizar el saludo — best-effort:
    nunca debe romper la carga del chat. Sin colección/courseid reales
    (asistente "normal" sin curso, sentinel 0) devuelve None sin más."""
    if not collection_name or not courseid:
        return None
    try:
        return query_router.get_current_week_summary(collection_name, courseid)
    except Exception as e:
        print(f"⚠️ no se pudo calcular la semana actual para el saludo (se ignora): {e}")
        return None


def _chat_identidad(request: Request, token: str) -> Dict[str, int]:
    """`{userid, courseid}` ya resueltos en la carga de `GET /asistente/{token}`
    (desde el token de Moodle, o desde la sesión del gestor en modo prueba —
    ver asistente_chat_page) — la única fuente de identidad para los
    endpoints hijos (/mensaje, /conversaciones, /carpetas...). Ya no se
    acepta `courseid`/`userid` por query/body: con un token de 5 min y un
    solo uso no se puede volver a pedir en cada clic del chat, y aceptarlos
    del cliente sin más era exactamente el hueco que permitía leer la
    conversación de otro alumno con solo adivinar sus IDs."""
    identidad = request.session.get(f"chat_{token}")
    if not identidad:
        raise HTTPException(403, "Enlace no válido o caducado. Vuelve a abrirlo desde Moodle.")
    return identidad


@app.get("/asistente/{token}", response_class=HTMLResponse, tags=["Asistente público"])
async def asistente_chat_page(
    token: str,
    request: Request,
    courseid: int = Query(0, description="Solo para 'Probar chat' (prueba=1) desde el gestor — ignorado para alumnos/docentes reales, que llegan vía token de Moodle."),
    prueba: Optional[str] = Query(None, description="'1' = conversación de prueba del docente/admin desde el gestor (no cuenta en estadísticas)."),
):
    asistente = await db.run(assistants.get_asistente_by_token, token)
    if not asistente:
        return templates.TemplateResponse(
            "asistente_chat.html",
            {"request": request, "error": "Este asistente no existe o no está activo."},
            status_code=404,
        )

    # ── Modo prueba (botón "Probar chat"): misma cookie del gestor, no
    # pasa por Moodle/secureurl — sigue igual que antes. ─────────
    usuario_gestor = request.session.get("user")
    es_prueba = bool(
        usuario_gestor and (prueba or "").strip().lower() in ("1", "true", "si", "sí")
    )
    if es_prueba:
        rol_gestor = usuario_gestor.get("rol") or (
            "admin" if usuario_gestor.get("is_admin") else "docente"
        )
        if rol_gestor != "admin" and not usuario_gestor.get("is_admin"):
            try:
                auth.require_docente_curso(
                    request, courseid or asistente.get("moodle_courseid")
                )
            except HTTPException:
                es_prueba = False  # sin acceso: se ignora `prueba`

    if es_prueba:
        rol_gestor = usuario_gestor.get("rol") or (
            "admin" if usuario_gestor.get("is_admin") else "docente"
        )
        if rol_gestor == "admin" or usuario_gestor.get("is_admin"):
            effective_userid = -int(usuario_gestor["id"])
        else:
            effective_userid = int(
                usuario_gestor.get("moodle_userid") or -int(usuario_gestor["id"])
            )
        moodle_username = usuario_gestor.get("username")
        fullname = usuario_gestor.get("nombre") or moodle_username
        # Mismo namespace de sesión que el flujo real (ver abajo), para que
        # los endpoints hijos (/mensaje, /conversaciones, /carpetas...) no
        # necesiten distinguir "prueba" de "real": siempre leen de acá.
        request.session[f"chat_{token}"] = {
            "userid": effective_userid,
            "courseid": courseid or asistente.get("moodle_courseid") or 0,
        }
    else:
        # Alumno/docente real, embebido desde Moodle (plugin idanIA,
        # mod_secureurl) — token obligatorio, de un solo uso y 5 min de
        # vida. Se canjea UNA vez por una entrada en la sesión propia
        # (namespaced por `token` del asistente, no el de Moodle) y se
        # redirige sin el `?token=` en la URL; de ahí en más (y en todos
        # los endpoints hijos: /mensaje, /conversaciones, /carpetas...) la
        # identidad viene de esa sesión, nunca de query params/body que el
        # cliente pueda manipular.
        chat_key = f"chat_{token}"
        secure_token = _qparam(request, "token")
        if secure_token:
            claims = auth.verify_secureurl(
                secure_token, scope="activity",
                course=asistente["moodle_courseid"] if asistente["rd_id"] is None else None,
            )
            try:
                courseid = int(claims.get("courseid"))
                userid_moodle = int(claims.get("userid"))
            except (TypeError, ValueError):
                raise HTTPException(
                    400, "El token no incluye userid/courseid — revisa la configuración de la actividad en Moodle.",
                )
            request.session[chat_key] = {"userid": userid_moodle, "courseid": courseid}
            return RedirectResponse(f"{settings.resolved_url_prefix}/asistente/{token}", status_code=303)
        identidad = request.session.get(chat_key)
        if not identidad:
            raise HTTPException(403, "Enlace no válido o caducado. Vuelve a abrirlo desde Moodle.")
        courseid = identidad["courseid"]
        effective_userid = identidad["userid"]
        loop = asyncio.get_event_loop()
        moodle_info = await loop.run_in_executor(None, get_moodle_user_info, effective_userid)
        moodle_username = moodle_info["username"]
        fullname = moodle_info["fullname"]

    # rd_id=None -> asistente "normal": resuelve directo por su propio
    # courseid (ignora el courseid de la URL, que puede venir de un curso
    # cualquiera del alumno) en vez de RD+courseid.
    if asistente["rd_id"] is not None:
        collection_name = await db.run(rds.resolve_collection, asistente["rd_id"], courseid)
    else:
        collection_name = await db.run(rds.resolve_collection_normal, asistente["moodle_courseid"])
    if not collection_name:
        return templates.TemplateResponse(
            "asistente_chat.html",
            {
                "request": request,
                "error": f"No hay ninguna colección asignada a este curso (id {courseid}) en la RD de este asistente.",
            },
            status_code=404,
        )

    # Activación POR CURSO (override del docente): no afecta a las demás
    # aulas de la RD ni al asistente global.
    courseid_cfg = asistente["moodle_courseid"] if asistente["rd_id"] is None else courseid
    cfg_curso = await db.run(assistants.get_curso_config, asistente["id"], courseid_cfg or 0)
    if cfg_curso is not None and cfg_curso.get("activo") is False:
        return templates.TemplateResponse(
            "asistente_chat.html",
            {"request": request, "error": "Este asistente no está habilitado para este curso."},
            status_code=403,
        )

    week_info = await db.run(_safe_week_info, collection_name, courseid)
    saludo = assistants.compose_saludo(asistente["nombre"], asistente.get("mensaje_bienvenida"), fullname, week_info)

    # ¿Ya existía esta sesión con historial antes de este load? -> se
    # muestra (no se guarda) el mensaje de reencuentro en vez del saludo.
    existing_sesion = await db.run(
        db.fetch_one,
        "SELECT id FROM asistente_sesiones WHERE asistente_id=%s AND moodle_courseid=%s "
        "AND moodle_userid=%s AND es_prueba=%s "
        "ORDER BY started_at DESC LIMIT 1",
        (asistente["id"], courseid, effective_userid, es_prueba),
    )
    tenia_historial = False
    if existing_sesion:
        tenia_historial = bool(await db.run(assistants.count_preguntas, existing_sesion["id"]))

    sesion = await db.run(
        assistants.get_or_create_sesion,
        asistente["id"], courseid, effective_userid, moodle_username, fullname, collection_name, saludo,
        False, es_prueba, usuario_gestor.get("id") if es_prueba else None,
    )
    historial_rows = await db.run(assistants.get_mensajes, sesion["id"])
    # El saludo (siempre el primer mensaje, ver assistants.split_welcome) se
    # separa acá — se muestra en su propio bloque fijo, no como un turno
    # más de la lista de mensajes que scrollea (ver asistente_chat.html).
    _, historial_rows = assistants.split_welcome(historial_rows)
    # id/rol/contenido/calificacion: escalares simples, no rompen el
    # `tojson` de Jinja (a diferencia de fuentes_json/config_usado_json/
    # created_at, que sí traen tipos no serializables).
    historial = [
        {"id": m["id"], "rol": m["rol"], "contenido": m["contenido"], "calificacion": m["calificacion"]}
        for m in historial_rows
    ]

    reencuentro = None
    if tenia_historial:
        reencuentro = assistants.compose_saludo(
            asistente["nombre"], asistente.get("mensaje_reencuentro"), fullname, week_info,
            is_reencuentro=True,
        )

    return templates.TemplateResponse(
        "asistente_chat.html",
        {
            "request": request,
            "error": None,
            "token": token,
            "asistente_nombre": asistente["nombre"],
            "mensaje_bienvenida": asistente.get("mensaje_bienvenida"),
            "sesion_id": sesion["id"],
            "saludo_nombre": fullname or "",
            "saludo": saludo,
            "historial": historial,
            "reencuentro": reencuentro,
            "max_actividades_sesion": (
                50 if es_prueba
                else (cfg_curso["max_actividades_sesion"] if cfg_curso else asistente.get("max_actividades_sesion", 10))
            ),
            "preguntas_usadas": await db.run(assistants.count_preguntas, sesion["id"]),
            "es_prueba": es_prueba,
            "courseid": courseid,
            "userid": effective_userid,
        },
    )


class AsistenteMensajeRequest(BaseModel):
    sesion_id: int
    pregunta: str


@app.post("/asistente/{token}/mensaje", tags=["Asistente público"])
async def asistente_enviar_mensaje(token: str, req: AsistenteMensajeRequest):
    asistente = await db.run(assistants.get_asistente_by_token, token)
    if not asistente:
        raise HTTPException(404, "Este asistente no existe o no está activo.")

    sesion = await db.run(assistants.get_sesion, req.sesion_id)
    if not sesion or sesion["asistente_id"] != asistente["id"]:
        raise HTTPException(404, "Sesión no válida para este asistente.")

    if not req.pregunta or not req.pregunta.strip():
        raise HTTPException(400, "La pregunta no puede estar vacía.")

    courseid_sesion = sesion.get("moodle_courseid") or 0
    cfg = await db.run(assistants.get_curso_config, asistente["id"], courseid_sesion)
    if cfg is not None and cfg.get("activo") is False:
        raise HTTPException(403, "Este asistente está deshabilitado para este curso.")
    if sesion.get("es_prueba"):
        # En modo prueba el docente puede conversar más allá del tope normal.
        max_actividades = 50
    elif cfg is not None and cfg.get("max_actividades_sesion") is not None:
        max_actividades = int(cfg["max_actividades_sesion"])
    else:
        max_actividades = asistente.get("max_actividades_sesion") or 10
    preguntas_usadas = await db.run(assistants.count_preguntas, req.sesion_id)
    if preguntas_usadas >= max_actividades:
        raise HTTPException(
            429,
            f"Llegaste al máximo de {max_actividades} preguntas para esta conversación. "
            "Iniciá una nueva conversación para seguir preguntando.",
        )

    # Se obtiene el historial ANTES de guardar la pregunta actual, para no
    # duplicarla (memoria acotada: 6 pares y solo las últimas 6 horas — un
    # historial viejo contaminaba la respuesta copiando enlaces/temas previos).
    history = await db.run(assistants.get_recent_history, req.sesion_id)
    await db.run(assistants.save_mensaje, req.sesion_id, "user", req.pregunta)

    # dense/sparse son el embedding maestro de la RD (no del asistente):
    # todas las colecciones de la RD deben compartirlo para que un mismo
    # asistente pueda responder con cualquiera de ellas indistintamente.
    # Para un asistente "normal" (sin RD) no hay RD de la que heredarlo —
    # y a diferencia de una RD, una colección "normal" no persiste su
    # propio embedding en ningún otro lado una vez creada, así que cae al
    # dense/sparse_strategy fijado en el propio asistente al crearlo (ver
    # assistants.create_asistente). Si tampoco hay eso, recién ahí usa el
    # valor por defecto del sistema.
    rd = await db.run(rds.get_rd_for_collection, sesion["qdrant_collection_name"]) or {}

    # Configuración y exclusiones POR CURSO (override del docente). Si no hay
    # override, se usan los valores globales del asistente — mismo
    # comportamiento que antes. `cfg` ya se cargó arriba (tope de preguntas).
    funcionalidades = {
        "notas": bool(cfg["func_notas"] if cfg else asistente.get("func_notas", True)),
        "documentos": bool(cfg["func_documentos"] if cfg else asistente.get("func_documentos", True)),
        "navegacion": bool(cfg["func_navegacion"] if cfg else asistente.get("func_navegacion", True)),
    }
    excluded_filenames = await db.run(
        assistants.get_excluded_filenames, asistente["id"], courseid_sesion
    )

    try:
        result = await rag_pipeline.answer_query(
            collection_name=sesion["qdrant_collection_name"],
            query=req.pregunta,
            dense_strategy_name=rd.get("dense_strategy") or asistente.get("dense_strategy"),
            sparse_strategy_name=rd.get("sparse_strategy") or asistente.get("sparse_strategy"),
            rerank_strategy_name=asistente.get("rerank_strategy"),
            generation_strategy_name=asistente.get("generation_strategy"),
            extra_system_prompt=_build_asistente_context(asistente, rd, sesion.get("moodle_fullname")),
            history=history,
            # sentinel 0 = asistente "normal" sin curso real (ver
            # AsistenteMensajeRequest/asistente_chat_page) — el Query
            # Router necesita un courseid real de Moodle para navegación,
            # así que se pasa None en ese caso (mismo efecto que hoy: cae
            # a RAG normal para cualquier pregunta de navegación).
            course_id=sesion["moodle_courseid"] or None,
            # SIEMPRE el moodle_userid de esta sesión — nunca un valor que
            # venga del cliente en la pregunta misma, para que PROGRESS no
            # pueda usarse para consultar el avance de otro estudiante.
            user_id=sesion["moodle_userid"] or None,
            funcionalidades=funcionalidades,
            excluded_filenames=excluded_filenames,
        )
    except Exception as e:
        raise HTTPException(502, f"Error generando la respuesta: {e}")

    tiempo_respuesta_ms = result.get("config_used", {}).get("timings_ms", {}).get("total_ms")
    saved = await db.run(
        assistants.save_mensaje,
        req.sesion_id, "assistant", result["answer"], result["sources"], result["config_used"],
        result.get("tokens_consumidos"), tiempo_respuesta_ms,
    )
    result["preguntas_usadas"] = preguntas_usadas + 1
    result["max_actividades_sesion"] = max_actividades
    result["mensaje_id"] = saved["id"]

    return result


@app.post("/asistente/{token}/nueva-conversacion", tags=["Asistente público"])
async def asistente_nueva_conversacion(token: str, request: Request):
    """Crea una sesión nueva (sin arrastrar la memoria de la anterior) para
    el mismo asistente/curso/usuario — botón "Nueva conversación"."""
    asistente = await db.run(assistants.get_asistente_by_token, token)
    if not asistente:
        raise HTTPException(404, "Este asistente no existe o no está activo.")
    identidad = _chat_identidad(request, token)
    courseid, userid = identidad["courseid"], identidad["userid"]

    if asistente["rd_id"] is not None:
        collection_name = await db.run(rds.resolve_collection, asistente["rd_id"], courseid)
    else:
        collection_name = await db.run(rds.resolve_collection_normal, asistente["moodle_courseid"])
    if not collection_name:
        raise HTTPException(404, f"No hay ninguna colección asignada al curso {courseid} en esta RD.")

    # Reusa la identidad (moodle_username/fullname) de la última sesión de
    # este mismo usuario en vez de volver a resolverla — evita otra llamada
    # a Moodle y preserva el nombre con que ya se saludó antes.
    prev_sesion = await db.run(
        db.fetch_one,
        "SELECT moodle_username, moodle_fullname FROM asistente_sesiones "
        "WHERE asistente_id=%s AND moodle_courseid=%s AND moodle_userid=%s "
        "ORDER BY started_at DESC LIMIT 1",
        (asistente["id"], courseid, userid),
    )
    if prev_sesion:
        moodle_username = prev_sesion["moodle_username"]
        fullname = prev_sesion["moodle_fullname"]
    else:
        loop = asyncio.get_event_loop()
        moodle_info = await loop.run_in_executor(None, get_moodle_user_info, userid)
        moodle_username = moodle_info["username"]
        fullname = moodle_info["fullname"]
    week_info = await db.run(_safe_week_info, collection_name, courseid)
    saludo = assistants.compose_saludo(asistente["nombre"], asistente.get("mensaje_bienvenida"), fullname, week_info)

    sesion = await db.run(
        assistants.get_or_create_sesion,
        asistente["id"], courseid, userid, moodle_username, fullname, collection_name, saludo,
        force_new=True,
    )
    return {"sesion_id": sesion["id"], "saludo": saludo}


@app.get("/asistente/{token}/conversaciones", tags=["Asistente público"])
async def asistente_listar_conversaciones(token: str, request: Request):
    """Sidebar de historial: todas las conversaciones de este alumno con
    este asistente en este curso, más recientes primero, con vista previa."""
    asistente = await db.run(assistants.get_asistente_by_token, token)
    if not asistente:
        raise HTTPException(404, "Este asistente no existe o no está activo.")
    identidad = _chat_identidad(request, token)
    sesiones = await db.run(assistants.list_sesiones, asistente["id"], identidad["courseid"], identidad["userid"])
    return {"conversaciones": sesiones}


@app.get("/asistente/{token}/conversaciones/{sesion_id}", tags=["Asistente público"])
async def asistente_ver_conversacion(token: str, sesion_id: int, request: Request):
    """Mensajes de una conversación anterior — valida que pertenezca a
    este asistente/curso/usuario antes de devolverlos."""
    asistente = await db.run(assistants.get_asistente_by_token, token)
    if not asistente:
        raise HTTPException(404, "Este asistente no existe o no está activo.")
    identidad = _chat_identidad(request, token)
    sesion = await db.run(assistants.get_sesion, sesion_id)
    if (
        not sesion
        or sesion["asistente_id"] != asistente["id"]
        or sesion["moodle_courseid"] != identidad["courseid"]
        or sesion["moodle_userid"] != identidad["userid"]
    ):
        raise HTTPException(404, "Conversación no encontrada.")
    historial = await db.run(assistants.get_mensajes, sesion_id)
    saludo, historial = assistants.split_welcome(historial)
    return {"sesion_id": sesion_id, "historial": historial, "saludo": saludo}


async def _validar_sesion_del_alumno(request: Request, token: str, sesion_id: int) -> Dict[str, Any]:
    """Mismo chequeo de acceso que `asistente_ver_conversacion`, reusado
    por los endpoints de renombrar/mover — el alumno solo puede tocar sus
    propias sesiones de este asistente/curso (identidad de la sesión
    propia, ver `_chat_identidad`, nunca de lo que mande el cliente)."""
    asistente = await db.run(assistants.get_asistente_by_token, token)
    if not asistente:
        raise HTTPException(404, "Este asistente no existe o no está activo.")
    identidad = _chat_identidad(request, token)
    sesion = await db.run(assistants.get_sesion, sesion_id)
    if (
        not sesion
        or sesion["asistente_id"] != asistente["id"]
        or sesion["moodle_courseid"] != identidad["courseid"]
        or sesion["moodle_userid"] != identidad["userid"]
    ):
        raise HTTPException(404, "Conversación no encontrada.")
    return asistente


class CalificarMensajeRequest(BaseModel):
    calificacion: int  # 1 = útil, -1 = no útil
    comentario: Optional[str] = None  # solo se usa si calificacion == -1


@app.patch("/asistente/{token}/mensajes/{mensaje_id}/calificar", tags=["Asistente público"])
async def asistente_calificar_mensaje(token: str, mensaje_id: int, req: CalificarMensajeRequest, request: Request):
    """El alumno indica si la respuesta le fue útil (no califica al
    mentor/RD, solo la interacción). Solo mensajes 'assistant' de una
    sesión que pertenezca a este token + la identidad de su propia sesión."""
    if req.calificacion not in (1, -1):
        raise HTTPException(400, "calificacion debe ser 1 o -1")

    asistente = await db.run(assistants.get_asistente_by_token, token)
    if not asistente:
        raise HTTPException(404, "Este asistente no existe o no está activo.")
    identidad = _chat_identidad(request, token)

    mensaje = await db.run(assistants.get_mensaje_con_sesion, mensaje_id)
    if (
        not mensaje
        or mensaje["rol"] != "assistant"
        or mensaje["asistente_id"] != asistente["id"]
        or mensaje["moodle_courseid"] != identidad["courseid"]
        or mensaje["moodle_userid"] != identidad["userid"]
    ):
        raise HTTPException(404, "Mensaje no encontrado.")

    try:
        actualizado = await db.run(assistants.calificar_mensaje, mensaje_id, req.calificacion, req.comentario)
    except ValueError as e:
        raise HTTPException(400, str(e))

    return {"mensaje_id": mensaje_id, "calificacion": actualizado["calificacion"]}


class RenombrarSesionRequest(BaseModel):
    nombre: Optional[str] = None  # None/vacío = vuelve al nombre por defecto


@app.patch("/asistente/{token}/conversaciones/{sesion_id}/nombre", tags=["Asistente público"])
async def asistente_renombrar_sesion(token: str, sesion_id: int, req: RenombrarSesionRequest, request: Request):
    await _validar_sesion_del_alumno(request, token, sesion_id)
    sesion = await db.run(assistants.rename_sesion, sesion_id, req.nombre)
    return {"sesion_id": sesion["id"], "nombre": sesion["nombre"]}


class MoverSesionRequest(BaseModel):
    carpeta_id: Optional[int] = None  # None = sacar de cualquier carpeta


@app.patch("/asistente/{token}/conversaciones/{sesion_id}/carpeta", tags=["Asistente público"])
async def asistente_mover_sesion(token: str, sesion_id: int, req: MoverSesionRequest, request: Request):
    await _validar_sesion_del_alumno(request, token, sesion_id)
    sesion = await db.run(assistants.mover_sesion_a_carpeta, sesion_id, req.carpeta_id)
    return {"sesion_id": sesion["id"], "carpeta_id": sesion["carpeta_id"]}


class CrearCarpetaRequest(BaseModel):
    nombre: str


@app.post("/asistente/{token}/carpetas", status_code=201, tags=["Asistente público"])
async def asistente_crear_carpeta(token: str, req: CrearCarpetaRequest, request: Request):
    asistente = await db.run(assistants.get_asistente_by_token, token)
    if not asistente:
        raise HTTPException(404, "Este asistente no existe o no está activo.")
    identidad = _chat_identidad(request, token)
    try:
        carpeta = await db.run(assistants.create_carpeta, asistente["id"], identidad["userid"], req.nombre)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return carpeta


@app.get("/asistente/{token}/carpetas", tags=["Asistente público"])
async def asistente_listar_carpetas(token: str, request: Request):
    asistente = await db.run(assistants.get_asistente_by_token, token)
    if not asistente:
        raise HTTPException(404, "Este asistente no existe o no está activo.")
    identidad = _chat_identidad(request, token)
    carpetas = await db.run(assistants.list_carpetas, asistente["id"], identidad["userid"])
    return {"carpetas": carpetas}


@app.delete("/asistente/{token}/carpetas/{carpeta_id}", tags=["Asistente público"])
async def asistente_borrar_carpeta(token: str, carpeta_id: int, request: Request):
    asistente = await db.run(assistants.get_asistente_by_token, token)
    if not asistente:
        raise HTTPException(404, "Este asistente no existe o no está activo.")
    identidad = _chat_identidad(request, token)
    carpeta = await db.run(assistants.get_carpeta, carpeta_id)
    if not carpeta or carpeta["asistente_id"] != asistente["id"] or carpeta["moodle_userid"] != identidad["userid"]:
        raise HTTPException(404, "Carpeta no encontrada.")
    await db.run(assistants.delete_carpeta, carpeta_id)
    return {"success": True}


@app.get("/api/jobs/{job_id}", tags=["Compartido"])
async def job_status(job_id: str):
    """
    Estado de un job:
    - "pendiente_confirmacion" → mostrar resumen al usuario
    - "procesando"             → mostrar spinner
    - "completado"             → mostrar resultado
    - "error"                  → mostrar error
    """
    job = get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job no encontrado")
    return {
        "job_id":  job_id,
        "status":  job["status"],
        "message": job.get("message"),
        "result":  job.get("result"),
    }


# ═══════════════════════════════════════════════════════════════════
# ASISTENTE SEMIAUTOMÁTICO  /api/semi/*
# ═══════════════════════════════════════════════════════════════════

@app.post("/api/semi/upload", tags=["Semiautomático"])
async def semi_upload(
    collection_name: str = Query(..., description="Colección destino"),
    model_name: Optional[str] = Query(
        None,
        description=(
            "Modelo de embeddings en español a usar. "
            "Consulta GET /api/models para ver el catálogo. "
            "Si no se indica, se usa el modelo por defecto del servidor."
        ),
    ),
    source_type: Optional[str] = Query(
        None,
        description=(
            "'curso_propio' (default) o 'bibliografia'. Solo tiene efecto "
            "si la colección destino es híbrida — determina la prioridad "
            "que el tutor le da a este contenido en /api/rag/answer."
        ),
    ),
    files: List[UploadFile] = File(...),
):
    """
    Paso 1 y 2 del asistente semiautomático.
    Sube archivos PDF/DOCX, los guarda en temp y los valida.
    Retorna job_id + resumen de validación por archivo.
    Usuario debe confirmar antes de vectorizar.
    """
    if not files:
        raise HTTPException(400, "No se enviaron archivos")
    if len(files) > 20:
        raise HTTPException(400, "Máximo 20 archivos por lote")

    resolved_model = _resolve_model(model_name)
    service = get_service(resolved_model)

    if not service.collection_exists(collection_name):
        raise HTTPException(404, f"Colección '{collection_name}' no existe en Qdrant")

    # Validación de compatibilidad de dimensiones antes de procesar archivos
    try:
        service.validate_model_for_collection(collection_name)
    except ValueError as e:
        raise HTTPException(422, str(e))

    files_data = []
    errors = []

    for upload in files:
        ext = Path(upload.filename).suffix.lower()
        if ext not in settings.ALLOWED_EXTENSIONS:
            errors.append({"filename": upload.filename, "error": f"Extensión {ext} no permitida"})
            continue

        content = await upload.read()
        if len(content) > settings.MAX_FILE_SIZE:
            errors.append({"filename": upload.filename, "error": "Archivo demasiado grande (máx 50 MB)"})
            continue

        temp_path, original_name = _save_upload_to_temp(upload, content)
        files_data.append({"filename": original_name, "path": temp_path})

    if not files_data and errors:
        raise HTTPException(400, detail={"errors": errors})

    result = await SemiAutoCore.validate_uploads(
        files_data, collection_name, model_name=resolved_model, source_type=source_type
    )
    result["upload_errors"] = errors
    return result


@app.post("/api/semi/confirm/{job_id}", tags=["Semiautomático"])
async def semi_confirm(job_id: str, background_tasks: BackgroundTasks):
    """
    Paso 3: el usuario confirma → vectorización en background.
    La interfaz consulta GET /api/jobs/{job_id} hasta status = 'completado'.
    El modelo usado es el que se guardó al hacer el upload.
    """
    job = get_job(job_id)
    if not job:
        raise HTTPException(404, "Job no encontrado")
    if job["status"] != "pendiente_confirmacion":
        raise HTTPException(400, f"El job está en estado '{job['status']}', no puede confirmarse")

    job["status"] = "procesando"
    background_tasks.add_task(SemiAutoCore.confirm_and_vectorize, job_id)

    return {
        "job_id":          job_id,
        "status":          "procesando",
        "embedding_model": job["context"].get("model_name", settings.EMBEDDING_MODEL),
    }


# ═══════════════════════════════════════════════════════════════════
# COLA DE INDEXACIÓN (curso_archivos)  /api/ledger/*
# Detección de altas/modificaciones/bajas por archivo, a demanda — sin job
# periódico (ver ledger.py). Independiente de Semiautomático/Automático/
# Actualización, no los reemplaza ni se engancha a ellos todavía.
# ═══════════════════════════════════════════════════════════════════

@app.post("/api/ledger/sync", tags=["Cola de indexación"])
async def ledger_sync(
    curid: int = Query(..., description="ID del curso en Moodle"),
    collection_name: str = Query(..., description="Colección a la que pertenecen estos archivos"),
):
    """Compara la lista actual de archivos del curso contra `curso_archivos`
    y actualiza altas/posibles modificaciones/bajas. No vectoriza nada por
    sí mismo — solo deja la cola de trabajo lista para que el pipeline
    (Semiautomático/Automático/Actualización) la consulte."""
    try:
        return await db.run(ledger.sync_curso_archivos, collection_name, curid)
    except Exception as e:
        raise HTTPException(502, f"Error consultando Moodle: {e}")


@app.get("/api/ledger", tags=["Cola de indexación"])
async def ledger_list(collection_name: str = Query(...)):
    """Estado completo de la cola de indexación de una colección."""
    return {"archivos": await db.run(ledger.list_curso_archivos, collection_name)}


# ═══════════════════════════════════════════════════════════════════
# ASISTENTE AUTOMÁTICO  /api/auto/*
# ═══════════════════════════════════════════════════════════════════

@app.get("/api/auto/preview", tags=["Automático"])
async def auto_preview(
    curid: int = Query(..., description="ID del curso en Moodle"),
    collection_name: str = Query(..., description="Colección destino"),
    model_name: Optional[str] = Query(
        None,
        description="Modelo de embeddings a usar. Consulta GET /api/models.",
    ),
):
    """
    Consulta Moodle con el curid y lista los módulos con sus PDFs.
    Marca cuáles ya están vectorizados en la colección (duplicado).
    Usuario selecciona cuáles quiere vectorizar.
    """
    resolved_model = _resolve_model(model_name)
    service = get_service(resolved_model)

    if not service.collection_exists(collection_name):
        raise HTTPException(404, f"Colección '{collection_name}' no existe en Qdrant")

    try:
        service.validate_model_for_collection(collection_name)
    except ValueError as e:
        raise HTTPException(422, str(e))

    try:
        return AutoCore.preview_course(curid, collection_name, model_name=resolved_model)
    except Exception as e:
        raise HTTPException(502, f"Error consultando Moodle: {e}")


@app.get("/api/cobertura", tags=["Automático"])
async def cobertura_report(
    curid: int = Query(..., description="ID del curso en Moodle"),
    collection_name: str = Query(..., description="Colección destino"),
):
    """Reporte de COBERTURA (fix 1.6): recursos/actividades que EXISTEN en el
    aula (Moodle) pero cuyo contenido todavía NO está vectorizado en la
    colección. Reutiliza AutoCore.preview_course(), que ya marca cada
    archivo con is_duplicate (presente en Qdrant o no) — no se construye
    nada nuevo. Sirve para que el gestor muestre qué falta indexar."""
    loop = asyncio.get_event_loop()
    try:
        preview = await loop.run_in_executor(
            None, lambda: AutoCore.preview_course(curid, collection_name)
        )
    except Exception as e:
        raise HTTPException(502, f"Error consultando Moodle: {e}")

    faltantes = []
    for m in preview.get("modules", []):
        for f in m["files"]:
            if not f.get("is_duplicate"):
                faltantes.append({
                    "module_name": m["module_name"],
                    "filename": f["filename"],
                    "fileurl": f.get("fileurl"),
                    "file_type": f.get("file_type"),
                })
    return {
        "curid": curid,
        "collection_name": collection_name,
        "total_recursos": preview.get("total_files"),
        "sin_vectorizar": len(faltantes),
        "faltantes": faltantes,
    }


@app.post("/api/auto/download-validate", tags=["Automático"])
async def auto_download_validate(
    req: AutoVectorizeRequest,
    model_name: Optional[str] = Query(
        None,
        description="Modelo de embeddings a usar. Consulta GET /api/models.",
    ),
):
    """
    Descarga los PDFs seleccionados a temp, los valida
    y retorna job_id + resumen.
    Usuario debe confirmar antes de vectorizar.
    """
    resolved_model = _resolve_model(model_name)
    service = get_service(resolved_model)

    if not service.collection_exists(req.collection_name):
        raise HTTPException(404, f"Colección '{req.collection_name}' no existe en Qdrant")

    if not req.selected_filenames:
        raise HTTPException(400, "No se seleccionaron archivos")

    try:
        service.validate_model_for_collection(req.collection_name)
    except ValueError as e:
        raise HTTPException(422, str(e))

    try:
        return await AutoCore.download_and_validate(
            curid=req.curid,
            collection_name=req.collection_name,
            selected_filenames=req.selected_filenames,
            model_name=resolved_model,
            source_type=req.source_type,
            file_source_types=req.file_source_types,
        )
    except Exception as e:
        raise HTTPException(502, f"Error en descarga/validación: {e}")


@app.post("/api/auto/confirm/{job_id}", tags=["Automático"])
async def auto_confirm(job_id: str, background_tasks: BackgroundTasks):
    """
    Usuario confirma → vectorización en background.
    Consultar GET /api/jobs/{job_id} para el estado.
    """
    job = get_job(job_id)
    if not job:
        raise HTTPException(404, "Job no encontrado")
    if job["status"] != "pendiente_confirmacion":
        raise HTTPException(400, f"El job está en estado '{job['status']}'")

    job["status"] = "procesando"
    background_tasks.add_task(AutoCore.confirm_and_vectorize, job_id)

    return {
        "job_id":          job_id,
        "status":          "procesando",
        "embedding_model": job["context"].get("model_name", settings.EMBEDDING_MODEL),
    }


# ═══════════════════════════════════════════════════════════════════
# ASISTENTE DE ACTUALIZACIÓN  /api/update/*
# ═══════════════════════════════════════════════════════════════════

@app.get("/api/update/list", tags=["Actualización"])
async def update_list(
    collection_name: str = Query(..., description="Colección a consultar"),
    model_name: Optional[str] = Query(None, description="Modelo de embeddings a usar."),
):
    """
    Lista los documentos vectorizados en la colección.
    Usuario selecciona cuál quiere reemplazar.
    """
    resolved_model = _resolve_model(model_name)
    service = get_service(resolved_model)

    if not service.collection_exists(collection_name):
        raise HTTPException(404, f"Colección '{collection_name}' no existe en Qdrant")

    docs = UpdateCore.list_vectorized(collection_name, model_name=resolved_model)
    return {
        "collection":      collection_name,
        "embedding_model": resolved_model,
        "documents":       docs,
    }


@app.post("/api/update/prepare-manual", tags=["Actualización"])
async def update_prepare_manual(
    collection_name: str = Query(...),
    filename_to_replace: str = Query(..., description="Filename del doc a reemplazar en Qdrant"),
    model_name: Optional[str] = Query(None, description="Modelo de embeddings a usar."),
    source_type: Optional[str] = Query(None, description="'curso_propio' (default) o 'bibliografia'."),
    file: UploadFile = File(...),
):
    """
    Modo manual: el usuario sube el archivo nuevo.
    Se guarda en temp, se valida y se retorna job_id + resumen.
    """
    resolved_model = _resolve_model(model_name)
    service = get_service(resolved_model)

    if not service.collection_exists(collection_name):
        raise HTTPException(404, f"Colección '{collection_name}' no existe en Qdrant")

    try:
        service.validate_model_for_collection(collection_name)
    except ValueError as e:
        raise HTTPException(422, str(e))

    ext = Path(file.filename).suffix.lower()
    if ext not in settings.ALLOWED_EXTENSIONS:
        raise HTTPException(400, f"Extensión {ext} no permitida")

    content = await file.read()
    if len(content) > settings.MAX_FILE_SIZE:
        raise HTTPException(400, "Archivo demasiado grande (máx 50 MB)")

    temp_path, original_name = _save_upload_to_temp(file, content)

    try:
        return await UpdateCore.prepare_manual(
            file_path=temp_path,
            original_filename=original_name,
            filename_to_replace=filename_to_replace,
            collection_name=collection_name,
            model_name=resolved_model,
            source_type=source_type,
        )
    except ValueError as e:
        temp_path.unlink(missing_ok=True)
        raise HTTPException(422, str(e))
    except Exception as e:
        temp_path.unlink(missing_ok=True)
        raise HTTPException(500, f"Error validando archivo: {e}")


@app.post("/api/update/prepare-moodle", tags=["Actualización"])
async def update_prepare_moodle(
    collection_name: str = Query(...),
    filename_to_replace: str = Query(..., description="Filename del doc a reemplazar en Qdrant"),
    curid: int = Query(...),
    moodle_filename: str = Query(..., description="Filename del PDF en Moodle"),
    model_name: Optional[str] = Query(None, description="Modelo de embeddings a usar."),
    source_type: Optional[str] = Query(None, description="'curso_propio' (default) o 'bibliografia'."),
):
    """
    Modo Moodle: descarga el archivo desde Moodle,
    lo guarda en temp, lo valida y retorna job_id + resumen.
    """
    resolved_model = _resolve_model(model_name)
    service = get_service(resolved_model)

    if not service.collection_exists(collection_name):
        raise HTTPException(404, f"Colección '{collection_name}' no existe en Qdrant")

    try:
        service.validate_model_for_collection(collection_name)
    except ValueError as e:
        raise HTTPException(422, str(e))

    try:
        return await UpdateCore.prepare_from_moodle(
            curid=curid,
            moodle_filename=moodle_filename,
            filename_to_replace=filename_to_replace,
            collection_name=collection_name,
            model_name=resolved_model,
            source_type=source_type,
        )
    except ValueError as e:
        msg = str(e)
        code = 404 if "no encontrado en curid" in msg else 422
        raise HTTPException(code, msg)
    except Exception as e:
        print("ERROR REAL EN PREPARE_MOODLE:", repr(e))
        raise HTTPException(
            status_code=500,
            detail=f"Error interno procesando archivo: {str(e)}"
        )


@app.get("/api/update/preview-modules", tags=["Actualización"])
async def update_preview_modules(
    curid: int = Query(...),
    collection_name: str = Query(...),
    model_name: Optional[str] = Query(None, description="Modelo de embeddings a usar."),
):
    """
    Lista los módulos y PDFs del curid para que el usuario
    elija cuál descargará como reemplazo (modo Moodle de actualización).
    """
    resolved_model = _resolve_model(model_name)
    try:
        return AutoCore.preview_course(curid, collection_name, model_name=resolved_model)
    except Exception as e:
        raise HTTPException(502, f"Error consultando Moodle: {e}")


@app.post("/api/update/confirm/{job_id}", tags=["Actualización"])
async def update_confirm(job_id: str, background_tasks: BackgroundTasks):
    """
    El usuario confirma la actualización → background task:
    elimina el doc anterior y vectoriza el nuevo con el modelo del job.
    Consultar GET /api/jobs/{job_id} para el estado.
    """
    job = get_job(job_id)
    if not job:
        raise HTTPException(404, "Job no encontrado")
    if job["status"] != "pendiente_confirmacion":
        raise HTTPException(400, f"El job está en estado '{job['status']}'")

    job["status"] = "procesando"
    background_tasks.add_task(UpdateCore.confirm_and_vectorize, job_id)

    return {
        "job_id":          job_id,
        "status":          "procesando",
        "embedding_model": job["context"].get("model_name", settings.EMBEDDING_MODEL),
    }


# ═══════════════════════════════════════════════════════════════════
# BÚSQUEDA (utilitario)
# ═══════════════════════════════════════════════════════════════════

@app.get("/api/search", tags=["Búsqueda"])
async def search(
    query: str = Query(...),
    collection_name: str = Query(...),
    top_k: int = Query(3, ge=1, le=20),
    model_name: Optional[str] = Query(
        None,
        description=(
            "Modelo para vectorizar la consulta. Debe coincidir con el modelo "
            "usado al vectorizar los documentos de esta colección."
        ),
    ),
):
    """
    Búsqueda semántica en una colección.
    El modelo usado para la query debe ser el mismo con el que se
    vectorizaron los documentos (mismas dimensiones).
    """
    resolved_model = _resolve_model(model_name)
    service = get_service(resolved_model)

    if not service.collection_exists(collection_name):
        raise HTTPException(404, f"Colección '{collection_name}' no existe")

    try:
        service.validate_model_for_collection(collection_name)
    except ValueError as e:
        raise HTTPException(422, str(e))

    from qdrant_admin import DENSE_VECTOR_NAME, get_qdrant_admin

    vector = await service.embed([query], is_query=True)
    query_kwargs = {"collection_name": collection_name, "query": vector[0], "limit": top_k}
    if get_qdrant_admin().get_vector_schema(collection_name) == "hybrid":
        # Colecciones híbridas usan vectores con nombre (dense/sparse) — sin
        # `using`, Qdrant no sabe contra cuál vector buscar y responde 400.
        query_kwargs["using"] = DENSE_VECTOR_NAME
    hits = service._client.query_points(**query_kwargs).points
    return {
        "query":           query,
        "collection":      collection_name,
        "embedding_model": resolved_model,
        "results": [
            {
                "score":           round(h.score, 4),
                "text":            h.payload.get("text"),
                "filename":        h.payload.get("filename"),
                "course_name":     h.payload.get("course_name"),
                "chunk":           h.payload.get("chunk"),
                "embedding_model": h.payload.get("embedding_model"),
            }
            for h in hits
        ],
    }


# ═══════════════════════════════════════════════════════════════════
# CONFIGURACIÓN RAG (Strategy Pattern)  /api/config/*
# ═══════════════════════════════════════════════════════════════════

_CONFIG_STAGES = list(STAGE_CATALOGS.keys())


class StageConfigRequest(BaseModel):
    strategy_name: str


# ── Claves de API (registradas ANTES de /api/config/{stage}: Starlette
# resuelve por orden de registro, y "credentials" coincidiría con el
# parámetro {stage} si el genérico se registrara primero) ──

class SetCredentialRequest(BaseModel):
    value: str


class TestCredentialRequest(BaseModel):
    value: Optional[str] = None


@app.get("/api/config/credentials", tags=["Configuración RAG"])
async def get_credentials():
    """Estado de las credenciales (API keys) configuradas. Nunca expone el
    valor completo de un secreto — solo si está configurado y sus últimos
    caracteres (masked_value)."""
    return {"credentials": credentials.list_credentials()}


@app.post("/api/config/credentials/{field}", tags=["Configuración RAG"])
async def set_credential_endpoint(field: str, req: SetCredentialRequest):
    """Guarda una credencial: actualiza settings en memoria (efecto
    inmediato) y persiste en .env (sobrevive un reinicio del servidor)."""
    try:
        return credentials.set_credential(field, req.value)
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.post("/api/config/credentials/{field}/test", tags=["Configuración RAG"])
async def test_credential_endpoint(field: str, req: TestCredentialRequest):
    """Prueba la conexión real contra el proveedor correspondiente, usando
    `value` si se manda (prueba temporal, sin guardar) o el valor ya
    guardado en settings si no."""
    try:
        return await credentials.test_credential(field, req.value)
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.get("/api/config/defaults-asistente", tags=["Configuración RAG"])
async def get_defaults_asistente(user=Depends(auth.require_admin)):
    """Embeddings por defecto que usa la creación de asistentes (docente y
    admin) — evita que el wizard admin caiga al default global del sistema."""
    return {
        "dense": settings.DOCENTE_DEFAULT_DENSE,
        "sparse": settings.DOCENTE_DEFAULT_SPARSE,
    }


@app.get("/api/config/{stage}", tags=["Configuración RAG"])
async def get_stage_config(stage: str):
    """
    Selección actual y catálogo completo de una etapa del pipeline RAG.
    `stage` ∈ etl_document | etl_audio | contextual | dense | sparse |
    rerank | generation.
    """
    if stage not in _CONFIG_STAGES:
        raise HTTPException(404, f"Etapa '{stage}' desconocida. Válidas: {_CONFIG_STAGES}")

    current = strategy_registry.resolve_strategy_name(stage)
    catalog = STAGE_CATALOGS[stage]

    return {
        "stage": stage,
        "current": current,
        "options": [
            {
                "name": name,
                **info,
                "usable": strategy_is_usable(stage, name),
            }
            for name, info in catalog.items()
        ],
    }


@app.post("/api/config/{stage}", tags=["Configuración RAG"])
async def set_stage_config(stage: str, req: StageConfigRequest):
    """Cambia la estrategia activa de una etapa (override en memoria).
    Afecta automáticamente la próxima vectorización de los 3 mecanismos
    contra colecciones híbridas, y las próximas consultas de /api/rag/*."""
    if stage not in _CONFIG_STAGES:
        raise HTTPException(404, f"Etapa '{stage}' desconocida. Válidas: {_CONFIG_STAGES}")

    try:
        get_strategy_info(stage, req.strategy_name)  # valida que exista
    except ValueError as e:
        raise HTTPException(400, str(e))

    get_runtime_config().set(stage, req.strategy_name)

    return {
        "stage": stage,
        "current": req.strategy_name,
        "message": f"Estrategia de '{stage}' actualizada a '{req.strategy_name}'",
    }


# ═══════════════════════════════════════════════════════════════════
# RAG: recuperación híbrida + reranking + generación  /api/rag/*
# ═══════════════════════════════════════════════════════════════════

class RagAnswerRequest(BaseModel):
    collection_name: str
    query: str
    dense_strategy: Optional[str] = None
    sparse_strategy: Optional[str] = None
    rerank_strategy: Optional[str] = None
    generation_strategy: Optional[str] = None
    top_n: Optional[int] = None
    asistente_id: Optional[int] = None


@app.get("/api/rag/retrieve", tags=["RAG"])
async def rag_retrieve(
    query: str = Query(...),
    collection_name: str = Query(...),
    top_n: int = Query(5, ge=1, le=20),
    dense_strategy: Optional[str] = Query(None),
    sparse_strategy: Optional[str] = Query(None),
    rerank_strategy: Optional[str] = Query(None),
):
    """
    Recuperación híbrida + reranking + boosting a `curso_propio`, sin
    generación (útil para depurar/previsualizar qué se recuperaría).
    Requiere una colección híbrida (vectores nombrados dense+sparse) —
    usa Gestión Qdrant para crear una si la colección es legacy.
    """
    from qdrant_admin import get_qdrant_admin

    admin = get_qdrant_admin()
    if not admin.collection_exists(collection_name):
        raise HTTPException(404, f"Colección '{collection_name}' no existe")
    if admin.get_vector_schema(collection_name) != "hybrid":
        raise HTTPException(
            422,
            f"La colección '{collection_name}' es legacy (vector único). "
            f"La búsqueda híbrida requiere una colección con esquema 'hybrid' "
            f"— créala o elige otra desde Gestión Qdrant.",
        )

    try:
        candidates = await rag_pipeline.retrieve_rerank_boost(
            collection_name=collection_name,
            query=query,
            dense_strategy_name=dense_strategy,
            sparse_strategy_name=sparse_strategy,
            rerank_strategy_name=rerank_strategy,
            top_n=top_n,
        )
    except ValueError as e:
        raise HTTPException(400, str(e))

    return {
        "query": query,
        "collection": collection_name,
        "results": candidates,
    }


@app.post("/api/rag/answer", tags=["RAG"])
async def rag_answer(req: RagAnswerRequest):
    """
    Pipeline completo del tutor: recuperación híbrida → rerank → boost →
    generación con el system prompt fijo. Usado por "Búsqueda semántica" y
    por "Prueba de Asistente" (si `asistente_id` viene informado, se
    agrega su `prompt_maestro` — misma lógica que la ruta pública).
    """
    from qdrant_admin import get_qdrant_admin

    admin = get_qdrant_admin()
    if not admin.collection_exists(req.collection_name):
        raise HTTPException(404, f"Colección '{req.collection_name}' no existe")
    if admin.get_vector_schema(req.collection_name) != "hybrid":
        raise HTTPException(
            422,
            f"La colección '{req.collection_name}' es legacy (vector único). "
            f"La generación con el tutor requiere una colección con esquema "
            f"'hybrid' — créala o elige otra desde Gestión Qdrant.",
        )

    extra_system_prompt = None
    dense_strategy = req.dense_strategy
    sparse_strategy = req.sparse_strategy
    rerank_strategy = req.rerank_strategy
    generation_strategy = req.generation_strategy
    if req.asistente_id is not None:
        asistente = await db.run(assistants.get_asistente, req.asistente_id)
        if not asistente:
            raise HTTPException(404, f"El asistente {req.asistente_id} no existe")
        if asistente["rd_id"] is not None:
            vinculo = await db.run(
                db.fetch_one,
                "SELECT 1 FROM colecciones WHERE rd_id = %s AND qdrant_collection_name = %s",
                (asistente["rd_id"], req.collection_name),
            )
        else:
            vinculo = await db.run(
                db.fetch_one,
                "SELECT 1 FROM colecciones WHERE rd_id IS NULL AND moodle_courseid = %s AND qdrant_collection_name = %s",
                (asistente["moodle_courseid"], req.collection_name),
            )
        if not vinculo:
            raise HTTPException(
                400,
                f"La colección '{req.collection_name}' no está vinculada a este asistente.",
            )
        # La config propia del asistente aplica salvo que el request la
        # override explícitamente (así "Prueba de Asistente" prueba
        # exactamente lo que respondería en público). dense/sparse: con RD
        # es el embedding maestro de la RD; sin RD ("normal"), cae al
        # dense/sparse_strategy fijado en el propio asistente (mismo
        # fallback que asistente_enviar_mensaje).
        rd = (await db.run(rds.get_rd, asistente["rd_id"]) or {}) if asistente["rd_id"] is not None else {}
        extra_system_prompt = _build_asistente_context(asistente, rd)
        dense_strategy = dense_strategy or rd.get("dense_strategy") or asistente.get("dense_strategy")
        sparse_strategy = sparse_strategy or rd.get("sparse_strategy") or asistente.get("sparse_strategy")
        rerank_strategy = rerank_strategy or asistente.get("rerank_strategy")
        generation_strategy = generation_strategy or asistente.get("generation_strategy")

    try:
        return await rag_pipeline.answer_query(
            collection_name=req.collection_name,
            query=req.query,
            dense_strategy_name=dense_strategy,
            sparse_strategy_name=sparse_strategy,
            rerank_strategy_name=rerank_strategy,
            generation_strategy_name=generation_strategy,
            top_n=req.top_n,
            extra_system_prompt=extra_system_prompt,
        )
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(502, f"Error generando respuesta: {e}")


# ═══════════════════════════════════════════════════════════════════
# ENTRY POINT
# ═══════════════════════════════════════════════════════════════════

# La app se monta bajo settings.resolved_url_prefix cuando corre detrás de
# un nginx compartido con otras funcionalidades (ver settings.py); sin
# prefijo (desarrollo local) sirve directo en la raíz, sin envoltorio.
if settings.resolved_url_prefix:
    # Fix 3.4: el wrapper recibe el MISMO lifespan — uvicorn levanta asgi_app,
    # y el lifespan de una app MONTADA no corre solo. Sin esto, con URL_PREFIX
    # configurado no se ejecutaban bootstrap_admin()/validate_settings()/
    # credentials.load_all_into_settings()/el scheduler (bug real).
    asgi_app = FastAPI(title=settings.API_TITLE, lifespan=lifespan)
    asgi_app.mount(settings.resolved_url_prefix, app)
else:
    asgi_app = app


if __name__ == "__main__":
    import uvicorn
    print("\n" + "=" * 65)
    print("🌐  SISTEMA DE VECTORIZACIÓN DOCUMENTAL  v2")
    print("=" * 65)
    print(f"📍 Interfaz : http://localhost:{settings.API_PORT}/")
    print(f"📖 API Docs : http://localhost:{settings.API_PORT}/docs")
    print(f"🗄️  Qdrant  : {settings.QDRANT_HOST}:{settings.QDRANT_PORT}")
    print(f"🤖 Modelo   : {settings.EMBEDDING_MODEL}")
    print("=" * 65 + "\n")
    uvicorn.run(
        "app:asgi_app",
        host=settings.API_HOST,
        port=settings.API_PORT,
        reload=False,
        log_level="info",
    )
