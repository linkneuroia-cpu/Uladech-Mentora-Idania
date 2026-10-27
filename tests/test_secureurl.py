"""
tests/test_secureurl.py
========================
Integración con mod_secureurl (plugin idanIA de Moodle): /login
(scope="gestor") y /asistente/{token} (scope="activity") ya no confían en
username/userid/courseid de la URL — solo en un token cifrado verificado.
No toca Moodle ni la base real: usa secureurl.issue_token() para generar
tokens de prueba locales, y mockea todo lo que llega a Postgres/Moodle.
"""
import time

import pytest
from fastapi.testclient import TestClient

import app as appmod
import auth
import assistants
import docentes
import rds
import secureurl
from settings import settings

# Clave de 32 bytes SOLO para pruebas — no tiene relación con ninguna clave
# real de Moodle. version 1, mismo formato "SU1".
_TEST_KEY = b"\x01" * 32
_TEST_VERSION = 1


@pytest.fixture(autouse=True)
def _claves_de_prueba(monkeypatch):
    """Reemplaza las claves reales por una de prueba y limpia la caché de
    `jti` entre pruebas (si no, un token reusado en una prueba contaría
    como "ya visto" en la siguiente)."""
    monkeypatch.setattr(auth, "_secureurl_keys_cache", {_TEST_VERSION: _TEST_KEY})
    monkeypatch.setattr(auth, "_jti_cache", secureurl.JtiCache())
    yield


def _token(scope, params, **kw):
    extra = {"scope": scope}
    if "course" in kw:
        extra["course"] = kw.pop("course")
    return secureurl.issue_token(
        settings.SECUREURL_APP_ID, _TEST_VERSION, _TEST_KEY, params, extra=extra, **kw
    )


def _client():
    return TestClient(appmod.app)


# ── auth.verify_secureurl: unitario, sin red ni DB ──────────────────────

def test_verify_secureurl_token_valido():
    tok = _token("gestor", {"username": "123", "userid": 14})
    claims = auth.verify_secureurl(tok, scope="gestor")
    assert claims["username"] == "123"
    assert claims["userid"] == 14


def test_verify_secureurl_scope_distinto_403():
    tok = _token("activity", {"userid": 1, "courseid": 1440})
    with pytest.raises(Exception) as exc:
        auth.verify_secureurl(tok, scope="gestor")
    assert exc.value.status_code == 403


def test_verify_secureurl_vencido_403():
    tok = _token("gestor", {"username": "1", "userid": 1}, now=int(time.time()) - 600, ttl=300)
    with pytest.raises(Exception) as exc:
        auth.verify_secureurl(tok, scope="gestor")
    assert exc.value.status_code == 403


def test_verify_secureurl_alterado_403():
    tok = _token("gestor", {"username": "1", "userid": 1})
    alterado = tok[:-4] + ("AAAA" if tok[-4:] != "AAAA" else "BBBB")
    with pytest.raises(Exception) as exc:
        auth.verify_secureurl(alterado, scope="gestor")
    assert exc.value.status_code == 403


def test_verify_secureurl_aud_distinto_403():
    tok = secureurl.issue_token("otra-app", _TEST_VERSION, _TEST_KEY, {"userid": 1}, extra={"scope": "gestor"})
    with pytest.raises(Exception) as exc:
        auth.verify_secureurl(tok, scope="gestor")
    assert exc.value.status_code == 403


def test_verify_secureurl_curso_no_coincide_403():
    tok = _token("activity", {"userid": 1, "courseid": 1440}, course=1440)
    with pytest.raises(Exception) as exc:
        auth.verify_secureurl(tok, scope="activity", course=1441)
    assert exc.value.status_code == 403


def test_verify_secureurl_curso_coincide_ok():
    tok = _token("activity", {"userid": 1, "courseid": 1440}, course=1440)
    claims = auth.verify_secureurl(tok, scope="activity", course=1440)
    assert claims["courseid"] == 1440


def test_verify_secureurl_jti_reusado_403():
    tok = _token("gestor", {"username": "1", "userid": 1})
    auth.verify_secureurl(tok, scope="gestor")  # primer uso: ok
    with pytest.raises(Exception) as exc:
        auth.verify_secureurl(tok, scope="gestor")  # segundo uso: reuso
    assert exc.value.status_code == 403


# ── /login (scope="gestor") ─────────────────────────────────────────────

def test_login_token_actividad_403():
    tok = _token("activity", {"userid": 1, "courseid": 1440}, course=1440)
    r = _client().get(f"/login?token={tok}")
    assert r.status_code == 403


def test_login_sin_token_muestra_formulario():
    r = _client().get("/login")
    assert r.status_code == 200
    assert "token" not in str(r.url)


def test_login_token_gestor_valido_entra(monkeypatch):
    monkeypatch.setattr(docentes, "validar_docente", lambda u, i: {"id": 14, "fullname": "NILO VELASQUEZ"})
    monkeypatch.setattr(docentes, "cursos_de", lambda doc: [{"courseid": 1440}])
    monkeypatch.setattr(auth, "upsert_docente", lambda *a, **k: {"id": 1, "moodle_userid": 14})

    tok = _token("gestor", {"username": "32919741", "userid": 14})
    r = _client().get(f"/login?token={tok}", follow_redirects=False)
    assert r.status_code == 303
    assert "token=" not in r.headers["location"]


# ── /asistente/{token} (scope="activity") ───────────────────────────────

def _mock_asistente_normal(monkeypatch, moodle_courseid=1440):
    monkeypatch.setattr(
        assistants, "get_asistente_by_token",
        lambda token: {
            "id": 22, "nombre": "Idania", "rd_id": None, "activo": True,
            "moodle_courseid": moodle_courseid, "mensaje_bienvenida": None,
            "mensaje_reencuentro": None, "max_actividades_sesion": 10,
        },
    )
    monkeypatch.setattr(rds, "resolve_collection_normal", lambda cid: "Curso_1440")
    monkeypatch.setattr(assistants, "get_curso_config", lambda aid, cid: None)
    monkeypatch.setattr(appmod, "_safe_week_info", lambda col, cid: None)
    monkeypatch.setattr(appmod, "get_moodle_user_info", lambda uid: {"username": "alumno", "fullname": "Alumno Real"})
    monkeypatch.setattr(appmod.db, "fetch_one", lambda *a, **k: None)
    monkeypatch.setattr(assistants, "count_preguntas", lambda sid: 0)
    monkeypatch.setattr(assistants, "get_or_create_sesion", lambda *a, **k: {"id": 1})
    monkeypatch.setattr(assistants, "get_mensajes", lambda sid: [])
    monkeypatch.setattr(assistants, "split_welcome", lambda rows: (None, rows))


def test_asistente_sin_token_403(monkeypatch):
    _mock_asistente_normal(monkeypatch)
    r = _client().get("/asistente/tok?userid=999&courseid=999")
    assert r.status_code == 403


def test_asistente_token_gestor_en_chat_403(monkeypatch):
    _mock_asistente_normal(monkeypatch)
    tok = _token("gestor", {"username": "1", "userid": 1})
    r = _client().get(f"/asistente/tok?token={tok}")
    assert r.status_code == 403


def test_asistente_token_falta_courseid_400(monkeypatch):
    _mock_asistente_normal(monkeypatch)
    tok = _token("activity", {"userid": 1}, course=1440)  # sin courseid en p
    r = _client().get(f"/asistente/tok?token={tok}")
    assert r.status_code == 400


def test_asistente_curso_no_coincide_403(monkeypatch):
    _mock_asistente_normal(monkeypatch, moodle_courseid=1440)
    tok = _token("activity", {"userid": 1, "courseid": 1441}, course=1441)  # otro curso
    r = _client().get(f"/asistente/tok?token={tok}")
    assert r.status_code == 403


def test_asistente_token_valido_usa_valores_del_token_no_de_la_url(monkeypatch):
    _mock_asistente_normal(monkeypatch, moodle_courseid=1440)
    # La URL pide identidad de otro alumno (999/999) — el token, con
    # valores reales (1/1440), debe ganar siempre.
    tok = _token("activity", {"userid": 1, "courseid": 1440}, course=1440)
    client = _client()
    r = client.get(f"/asistente/tok?token={tok}&userid=999&courseid=999", follow_redirects=False)
    assert r.status_code == 303
    assert "token=" not in r.headers["location"]
    # Tras el redirect, la sesión propia (no la URL) decide la identidad.
    r2 = client.get("/asistente/tok?userid=999&courseid=999")
    assert r2.status_code == 200


# ── Cabeceras / cookie ───────────────────────────────────────────────────

def test_csp_frame_ancestors_presente():
    r = _client().get("/login")
    assert r.headers.get("content-security-policy") == f"frame-ancestors {settings.MOODLE_FRAME_ANCESTOR}"


def test_session_middleware_usa_settings_samesite_https_only():
    # SessionMiddleware se instancia una sola vez, al importar `app`, con
    # los valores de settings.SESSION_SAMESITE/SESSION_HTTPS_ONLY vigentes
    # en ese momento (por defecto lax/False, para que el desarrollo local
    # por http:// siga funcionando) — se verifica que esos parámetros
    # efectivamente llegaron al middleware real, no solo que la variable
    # de settings existe. El comportamiento con SameSite=None/Secure=True
    # se confirma manualmente una vez desplegado con esos valores en el
    # .env real (ver deploy/README.md).
    from starlette.middleware.sessions import SessionMiddleware

    entradas = [m for m in appmod.app.user_middleware if m.cls is SessionMiddleware]
    assert len(entradas) == 1
    kwargs = entradas[0].kwargs
    assert kwargs["same_site"] == settings.SESSION_SAMESITE
    assert kwargs["https_only"] == settings.SESSION_HTTPS_ONLY


# ── CSRF por Origin ──────────────────────────────────────────────────────

def test_post_origin_ajeno_403():
    r = _client().post("/login", json={"username": "x", "password": "y"}, headers={"Origin": "https://malicioso.com"})
    assert r.status_code == 403


def test_post_sin_origin_no_bloqueado_por_csrf():
    r = _client().post("/login", json={"username": "x", "password": "y"})
    # Pasa el CSRF (sin Origin) — falla más adelante por credenciales
    # inválidas (401), que es lo esperado, no un 403 de Origin.
    assert r.status_code == 401
