"""
tests/test_roles.py
===================
Permisos por rol usando `docentes.cursos_de` en vivo (no la foto de la
sesión). Se mockea el ERP: no toca base ni Moodle.
"""
import pytest
from fastapi import HTTPException

import auth
import docentes


class FakeRequest:
    def __init__(self, user):
        self.session = {"user": user}


def _docente(documento="123", courseids=None):
    # Sin `courseids`: simula la sesión creada por POST /login (contraseña).
    user = {"id": 1, "username": documento, "rol": "docente", "documento": documento}
    if courseids is not None:
        user["courseids"] = courseids
    return FakeRequest(user)


def test_docente_no_puede_entrar_a_endpoints_admin():
    req = FakeRequest({"id": 1, "username": "doc", "rol": "docente", "documento": "123"})
    with pytest.raises(HTTPException) as exc:
        auth.require_admin(req)
    assert exc.value.status_code == 403


def test_admin_pasa_require_admin():
    req = FakeRequest({"id": 1, "username": "admin", "rol": "admin", "is_admin": True})
    assert auth.require_admin(req)["rol"] == "admin"


def test_docente_su_curso_se_resuelve_en_vivo(monkeypatch):
    monkeypatch.setattr(docentes, "cursos_de", lambda doc: [{"courseid": 100}, {"courseid": 101}])
    req = _docente()
    assert auth.require_docente_curso(req, 100)["id"] == 1
    with pytest.raises(HTTPException) as exc:
        auth.require_docente_curso(req, 999)
    assert exc.value.status_code == 403


def test_sesion_por_password_sin_courseids_igual_entra(monkeypatch):
    # Sin `courseids` en la sesión (login por contraseña): antes daba 403.
    monkeypatch.setattr(docentes, "cursos_de", lambda doc: [{"courseid": 1422}])
    req = _docente(documento="32915556")
    assert auth.require_docente_curso(req, 1422)["rol"] == "docente"


def test_erp_caido_devuelve_503(monkeypatch):
    def boom(doc):
        raise RuntimeError("ERP caído")

    monkeypatch.setattr(docentes, "cursos_de", boom)
    req = _docente()
    with pytest.raises(HTTPException) as exc:
        auth.require_docente_curso(req, 100)
    assert exc.value.status_code == 503


def test_admin_pasa_cualquier_curso():
    req = FakeRequest({"id": 1, "username": "admin", "rol": "admin", "is_admin": True})
    assert auth.require_docente_curso(req, 999)["rol"] == "admin"


def test_docente_sin_courseid_es_403():
    req = _docente()
    with pytest.raises(HTTPException) as exc:
        auth.require_docente_curso(req, None)
    assert exc.value.status_code == 403
