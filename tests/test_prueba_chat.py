"""
tests/test_prueba_chat.py
=========================
Sesiones de prueba (botón "Probar chat"): se marcan con es_prueba, no se
mezclan con las reales del mismo userid y quedan fuera de las estadísticas.
No toca base ni Moodle.
"""
from fastapi.testclient import TestClient

import app as appmod
import assistants


def test_get_or_create_sesion_marca_prueba(monkeypatch):
    captured = {}

    def fake_fetch_one(sql, params=()):
        captured["select"] = params
        return None

    def fake_execute_returning(sql, params=()):
        captured["insert"] = params
        return {"id": 99}

    monkeypatch.setattr(assistants.db, "fetch_one", fake_fetch_one)
    monkeypatch.setattr(assistants.db, "execute", lambda *a, **k: None)
    monkeypatch.setattr(assistants.db, "execute_returning", fake_execute_returning)
    monkeypatch.setattr(assistants, "save_mensaje", lambda *a, **k: {})

    assistants.get_or_create_sesion(
        5, 1472, 1065, "u", "n", "Curso_1472", None, False, True, 7
    )
    # El SELECT filtra por es_prueba y el INSERT lo guarda junto a probado_por.
    assert captured["select"][-1] is True
    assert captured["insert"][-2] is True
    assert captured["insert"][-1] == 7


def test_sesion_prueba_y_real_no_se_mezclan(monkeypatch):
    vistos = []

    def fake_fetch_one(sql, params=()):
        vistos.append(params[-1])
        return None

    monkeypatch.setattr(assistants.db, "fetch_one", fake_fetch_one)
    monkeypatch.setattr(assistants.db, "execute", lambda *a, **k: None)
    monkeypatch.setattr(assistants.db, "execute_returning", lambda *a, **k: {"id": 1})
    monkeypatch.setattr(assistants, "save_mensaje", lambda *a, **k: {})

    assistants.get_or_create_sesion(1, 2, 3, "u", "n", "c", None, False, False, None)
    assistants.get_or_create_sesion(1, 2, 3, "u", "n", "c", None, False, True, 9)
    assert vistos == [False, True]


def test_estadisticas_excluyen_sesiones_de_prueba():
    assert "s.es_prueba = FALSE" in assistants._CALIF_SUBQUERY


def test_prueba_sin_sesion_del_gestor_se_ignora(monkeypatch):
    def fake_get(token):
        return {
            "id": 5, "nombre": "Idania", "rd_id": None,
            "moodle_courseid": 1472, "activo": True,
        }

    monkeypatch.setattr(assistants, "get_asistente_by_token", fake_get)
    client = TestClient(appmod.app)
    r = client.get("/asistente/tok?prueba=1&courseid=1472")
    assert r.status_code == 200
    # Sin cookie del gestor, `prueba` se ignora y se pide identificación normal.
    assert "Antes de empezar" in r.text
