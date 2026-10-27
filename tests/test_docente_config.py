"""
tests/test_docente_config.py
============================
Resolución del asistente/colección que sirve a un curso (normal o de RD) y
de la configuración/exclusiones POR CURSO, sin tocar la base real: se
mockean las consultas.
"""
import assistants
import rds


def test_exclusiones_por_curso(monkeypatch):
    cap = {}

    def fake_fetch_all(sql, params=()):
        cap["sql"] = sql
        cap["params"] = params
        return []

    monkeypatch.setattr(assistants.db, "fetch_all", fake_fetch_all)
    assistants.get_excluded_filenames(5, 1472)
    assert "moodle_courseid" in cap["sql"]
    assert cap["params"] == (5, 1472)


def test_asistente_para_curso_cae_a_rd(monkeypatch):
    monkeypatch.setattr(assistants, "get_asistente_normal_por_courseid", lambda cid: None)
    cap = {}

    def fake_fetch_one(sql, params=()):
        cap["sql"] = sql
        cap["params"] = params
        return {"id": 19, "rd_id": 13}

    monkeypatch.setattr(assistants.db, "fetch_one", fake_fetch_one)
    a = assistants.get_asistente_para_curso(1472)
    assert a["id"] == 19
    assert "rd_id" in cap["sql"]
    assert cap["params"] == (1472,)


def test_coleccion_para_curso_cae_a_rd(monkeypatch):
    monkeypatch.setattr(rds, "resolve_collection_normal", lambda cid: None)
    cap = {}

    def fake_fetch_one(sql, params=()):
        cap["sql"] = sql
        cap["params"] = params
        return {"qdrant_collection_name": "Curso_1472"}

    monkeypatch.setattr(rds.db, "fetch_one", fake_fetch_one)
    assert rds.resolve_collection_para_curso(1472) == "Curso_1472"
    assert cap["params"] == (1472,)
    assert "rd_id IS NOT NULL" in cap["sql"]


def test_coleccion_normal_tiene_prioridad(monkeypatch):
    monkeypatch.setattr(rds, "resolve_collection_normal", lambda cid: "Curso_normal")

    def prohibido(*args, **kwargs):
        raise AssertionError("no debe consultar RD si hay colección normal")

    monkeypatch.setattr(rds.db, "fetch_one", prohibido)
    assert rds.resolve_collection_para_curso(1600) == "Curso_normal"
