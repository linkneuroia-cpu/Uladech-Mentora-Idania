"""
tests/test_docente_conocimiento.py
==================================
`modules` de "Conocimiento idanIA" conserva el orden de Moodle (igual que
/api/auto/preview), calcula estados y resumen, y el endpoint de vectorización
(409 / solo pendientes) sin Moodle ni base real.
"""
import base64
import json

import pytest
from itsdangerous import TimestampSigner
from fastapi.testclient import TestClient

import app as appmod
from settings import settings


def _admin_client():
    c = TestClient(appmod.app)
    signer = TimestampSigner(str(settings.SESSION_SECRET_KEY))
    user = {"id": 1, "username": "admin", "rol": "admin", "is_admin": True}
    raw = base64.b64encode(json.dumps({"user": user}).encode())
    c.cookies.set("session", signer.sign(raw).decode())
    return c


def test_modules_preservan_orden_moodle_y_estado():
    preview = {
        "modules": [
            {"module_name": "Semana 10", "resource_id": 1, "files": [
                {"filename": "b.pdf", "fileurl": "https://moodle/x?token=SECRETO&id=1"},
                {"filename": "A.pdf"},
            ]},
            {"module_name": "General", "resource_id": 2, "files": [{"filename": "z.pdf"}]},
            {"module_name": "Semana 2", "resource_id": 3, "files": [{"filename": "c.pdf"}]},
        ]
    }
    modules, resumen = appmod._conocimiento_modules(
        preview, {"c.pdf": {"estado": "no_soportado"}}, {"z.pdf"}
    )
    # Mismo orden que Moodle (no se reagrupa).
    assert [m["module_name"] for m in modules] == ["Semana 10", "General", "Semana 2"]
    assert modules[1]["files"][0]["estado"] == "excluido"
    assert modules[2]["files"][0]["estado"] == "no_soportado"
    assert modules[0]["files"][0]["estado"] == "pendiente"
    # fileurl expuesto SIN el token de Moodle.
    assert "token=" not in (modules[0]["files"][0]["fileurl"] or "")
    assert "id=1" in modules[0]["files"][0]["fileurl"]
    assert resumen == {
        "total": 4, "vectorizados": 0, "pendientes": 2, "no_soportados": 1, "excluidos": 1,
    }


def test_prioridad_de_estados():
    preview = {
        "modules": [{
            "module_name": "Semana 1",
            "files": [
                {"filename": "a.pdf", "is_duplicate": True},
                {"filename": "b.pdf", "is_duplicate": True},
                {"filename": "c.pdf", "is_duplicate": True},
                {"filename": "d.pdf"},
            ],
        }]
    }
    modules, _ = appmod._conocimiento_modules(
        preview, {"c.pdf": {"estado": "no_soportado"}}, {"b.pdf"}
    )
    est = {f["filename"]: f["estado"] for f in modules[0]["files"]}
    assert est["a.pdf"] == "vectorizado"
    assert est["b.pdf"] == "excluido"          # excluido gana sobre vectorizado
    assert est["c.pdf"] == "vectorizado"       # vectorizado gana sobre no_soportado
    assert est["d.pdf"] == "pendiente"


def test_vectorizar_409_si_hay_job_en_curso():
    client = _admin_client()
    appmod._docente_jobs[1472] = "job-en-curso"
    try:
        r = client.post("/api/docente/cursos/1472/vectorizar", json={"filenames": None})
        assert r.status_code == 409
    finally:
        appmod._docente_jobs.pop(1472, None)


def test_filenames_null_encola_solo_pendientes(monkeypatch):
    async def fake_conocimiento(courseid, coleccion, asistente_id):
        return {
            "modules": [
                {"files": [
                    {"filename": "a.pdf", "estado": "pendiente"},
                    {"filename": "b.pdf", "estado": "vectorizado"},
                    {"filename": "c.pdf", "estado": "excluido"},
                ]},
            ],
            "resumen": {},
        }

    def fake_resolve(cid):
        return "Curso_1"

    def fake_create_task(coro):
        coro.close()  # no ejecutar la vectorización real
        return None

    monkeypatch.setattr(appmod, "_conocimiento_payload", fake_conocimiento)
    monkeypatch.setattr(appmod.rds, "resolve_collection_para_curso", fake_resolve)
    monkeypatch.setattr(appmod.assistants, "get_asistente_para_curso", lambda cid: None)
    monkeypatch.setattr(appmod.asyncio, "create_task", fake_create_task)

    client = _admin_client()
    try:
        r = client.post("/api/docente/cursos/1/vectorizar", json={"filenames": None})
        assert r.status_code == 200
        assert r.json()["encolados"] == 1
    finally:
        appmod._docente_jobs.pop(1, None)
