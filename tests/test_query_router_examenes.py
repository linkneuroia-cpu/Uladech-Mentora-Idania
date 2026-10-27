"""
tests/test_query_router_examenes.py
===================================
Curso 1440: "semana 8 + archivos", exámenes de la unidad, fechas desde el
Course Map, actividades ocultas/etiquetas, historial acotado, alias y
guardia de enlaces. No toca Moodle ni la base.
"""
from datetime import datetime, timezone

import pytest

import assistants
import course_map as cm
import moodle_tools as mt
import query_router as qr
import rag_pipeline as rp
from course_map import Activity, CourseMap, Section

START = int(datetime(2026, 9, 1, tzinfo=timezone.utc).timestamp())
APERTURA = START + 49 * 86400  # 20-oct (semana 8)
CIERRE = START + 55 * 86400    # 26-oct (semana 8)


def _course():
    return CourseMap(course_id=1440, fetched_at="2026-10-21T00:00:00+00:00", sections=[
        Section(section_number=7, name="Semana 07. Criterios de evaluación", visible=True, summary="",
                activities=[
                    Activity(cmid=60298, name="SESIÓN DE APRENDIZAJE N.° 7", modname="folder",
                             url="https://campus.uladech.edu.pe/mod/folder/view.php?id=60298",
                             visible=True, uservisible=True, completion_tracking=0, section_number=7),
                    Activity(cmid=78837, name="Guía de práctica N.° 7", modname="resource",
                             url="https://campus.uladech.edu.pe/mod/resource/view.php?id=78837",
                             visible=True, uservisible=True, completion_tracking=0, section_number=7),
                ]),
        Section(section_number=8, name="Semana 08. Evaluación de la Unidad I.", visible=True, summary="",
                activities=[
                    Activity(cmid=60300, name="SESIÓN DE APRENDIZAJE N.° 8", modname="folder",
                             url="https://campus.uladech.edu.pe/mod/folder/view.php?id=60300",
                             visible=True, uservisible=True, completion_tracking=0, section_number=8),
                    Activity(cmid=80849, name="Examen de unidad", modname="resource",
                             url="https://campus.uladech.edu.pe/mod/resource/view.php?id=80849",
                             visible=False, uservisible=False, completion_tracking=0, section_number=8),
                    Activity(cmid=60301, name="U1-Ex-Examen sumativo", modname="assign",
                             url="https://campus.uladech.edu.pe/mod/assign/view.php?id=60301",
                             visible=True, uservisible=True, completion_tracking=0, section_number=8,
                             dates=[{"label": "Apertura", "timestamp": APERTURA},
                                    {"label": "Cierre", "timestamp": CIERRE}]),
                    Activity(cmid=60302, name="Unidad II: Diseña un recurso digital", modname="label",
                             url=None, visible=True, uservisible=True, completion_tracking=0, section_number=8),
                ]),
    ])


@pytest.fixture
def mock_moodle(monkeypatch):
    course = _course()
    monkeypatch.setattr(cm, "get_course_map", lambda course_id: course)
    monkeypatch.setattr(cm, "get_current_week_number", lambda course_id: {
        "status": "confident", "week_number": 8, "elapsed_days": 50,
    })
    monkeypatch.setattr(cm, "get_course_dates", lambda course_id: {
        "status": "confident", "startdate_epoch": START,
        "startdate": "01 de septiembre de 2026", "enddate_epoch": 0, "enddate": None,
    })
    monkeypatch.setattr(qr.mc, "get_due_dates", lambda course_id: {"status": "unavailable"})
    monkeypatch.setattr(qr, "get_current_week_summary", lambda collection, course_id: None)
    return course


def test_semana_8_archivos_usa_course_map_sin_ocultas(mock_moodle):
    q = "Que archivos puedo revisar para le semana 8?"
    assert qr._classify(q, 1440) == {"intent": "SECTION_CONTENTS", "section_number": 8}
    r = qr.route("Curso_1440", 1440, q)
    text = r["payload"]["text"]
    assert "id=60300" in text
    assert "id=60301" in text
    assert "Abre 20/10" in text and "Cierra 26/10" in text
    assert "80849" not in text           # recurso oculto
    assert "Unidad II: Diseña" not in text  # etiqueta
    assert "id=60298" not in text and "id=78837" not in text  # semana 7


def test_ayudame_con_el_caso_semana_8(mock_moodle):
    q = "Ayudame con el caso para la semana 8"
    parsed = qr._classify(q, 1440)
    # Se documenta la decisión: "caso" se agregó a las palabras de contenido,
    # por eso responde con el contenido real de la semana 8 (no RAG).
    assert parsed == {"intent": "SECTION_CONTENTS", "section_number": 8}
    text = qr.route("Curso_1440", 1440, q)["payload"]["text"]
    assert "id=60300" in text
    assert "id=60298" not in text


def test_cuando_es_el_examen_usa_fechas_del_course_map(mock_moodle):
    q = "¿cuándo es el examen de la unidad?"
    parsed = qr._classify(q, 1440)
    assert parsed["intent"] in ("DUE_DATES", "ASSESSMENTS")
    text = qr.route("Curso_1440", 1440, q)["payload"]["text"]
    assert "U1-Ex-Examen sumativo" in text
    assert "26 de octubre de 2026" in text  # cierre real del 60301


def test_dame_el_examen_de_la_unidad_i(mock_moodle):
    q = "dame el examen de la unidad I"
    assert qr._classify(q, 1440) == {"intent": "ASSESSMENTS", "unit": "I"}
    text = qr.route("Curso_1440", 1440, q)["payload"]["text"]
    assert "U1-Ex-Examen sumativo" in text
    assert "id=60301" in text


def test_que_examen_hay_esta_semana_sigue_siendo_current_week(mock_moodle):
    assert qr._classify("¿qué examen hay esta semana?", 1440)["intent"] == "CURRENT_WEEK"


def test_navegacion_deshabilitada_responde_mensaje_fijo(mock_moodle):
    q = "Que archivos puedo revisar para le semana 8?"
    r = qr.route("Curso_1440", 1440, q, funcionalidades={"navegacion": False, "notas": True})
    assert "no está habilitada" in r["payload"]["text"].lower()


def test_alias_examen_unidad():
    a = mt._content_words("U1-Ex-Examen sumativo")
    b = mt._content_words("examen de la unidad I")
    assert {"examen", "unidad", "1"} <= (a & b)


def test_guardia_de_enlaces_quita_url_sin_respaldo():
    cands = [{"payload": {"filename": "x.pdf", "text": "sin enlaces"}}]
    ans, removed = rp._remove_unbacked_urls(
        "[Guía](https://campus.uladech.edu.pe/mod/resource/view.php?id=78837)", cands
    )
    assert ans.strip() == "Guía"
    assert removed


def test_guardia_de_enlaces_conserva_url_con_respaldo():
    cands = [{"payload": {"filename": "x.pdf", "text": "ver https://campus.uladech.edu.pe/mod/assign/view.php?id=60301"}}]
    ans, removed = rp._remove_unbacked_urls(
        "[Examen](https://campus.uladech.edu.pe/mod/assign/view.php?id=60301)", cands
    )
    assert "https://campus.uladech.edu.pe/mod/assign/view.php?id=60301" in ans
    assert not removed


def test_prompt_rotula_seccion_y_no_semana_seccion():
    prompt = rp.build_generation_prompt(
        "q",
        [{"payload": {"filename": "x.pdf", "section_name": "Semana 08. Evaluación de la Unidad I.", "text": "t"},
          "text": "t"}],
    )
    assert "sección de Moodle (no es un documento)" in prompt
    assert "semana/sección:" not in prompt


def test_historial_acotado_por_tiempo_y_pares(monkeypatch):
    captured = {}

    def fake_fetch_all(sql, params=()):
        captured["sql"] = sql
        captured["params"] = params
        return []

    monkeypatch.setattr(assistants.db, "fetch_all", fake_fetch_all)
    assistants.get_recent_history(5)
    assert "interval '1 hour'" in captured["sql"]
    assert captured["params"] == (5, 6, 12)


def test_course_map_tolera_cache_viejo_sin_dates():
    # JSON de una caché anterior a `dates`: debe seguir siendo legible.
    raw = '{"course_id":1,"fetched_at":"x","sections":[{"section_number":1,"name":"S",' \
          '"visible":true,"summary":"","activities":[{"cmid":1,"name":"A","modname":"assign",' \
          '"url":null,"visible":true,"uservisible":true,"completion_tracking":0,"section_number":1}]}]}'
    course = CourseMap.from_json(raw)
    assert course.sections[0].activities[0].dates == []
    assert cm.activity_due(course.sections[0].activities[0]) is None
