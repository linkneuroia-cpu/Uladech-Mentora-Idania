"""
tests/test_navegacion.py
========================
Navegabilidad estructurada: resumen HTML a texto, cruce de vencimientos por
nombre normalizado, semana actual y calendario no disponible. Sin Moodle.
"""
import app as appmod


def _est():
    return {
        "semana_actual": 2,
        "sections": [
            {
                "section_number": 1, "name": "Semana 1", "visible": True,
                "summary": "<p>Hola&nbsp;<b>mundo</b></p>",
                "activities": [
                    {"modname": "assign", "name": "Tarea N° 1", "url": "u1", "visible": True},
                ],
            },
            {
                "section_number": 2, "name": "Semana 2", "visible": True,
                "summary": "",
                "activities": [
                    {"modname": "quiz", "name": "Cuestionario de práctica", "url": "u2", "visible": False},
                ],
            },
        ],
    }


def test_html_a_texto():
    assert appmod._html_a_texto("<p>Hola&nbsp;<b>mundo</b></p><br>x") == "Hola mundo\nx"
    assert appmod._html_a_texto("") == ""
    assert appmod._html_a_texto("<style>.x{}</style><p>Texto</p>") == "Texto"


def test_vence_por_nombre_sin_tildes_ni_mayusculas():
    fechas = {
        "status": "confident",
        "events": [
            {"name": "TAREA N 1", "due_date": "12/10/2026", "due_epoch": 2000000000},
            {"name": "cuestionario de practica", "due_date": "15/10/2026", "due_epoch": 2000000100},
        ],
    }
    d = appmod._navegacion_payload(_est(), fechas)
    assert d["secciones"][0]["actividades"][0]["vence"] == "12/10/2026"
    assert d["secciones"][1]["actividades"][0]["vence"] == "15/10/2026"
    assert d["secciones"][0]["es_actual"] is False
    assert d["secciones"][1]["es_actual"] is True
    assert d["secciones"][0]["resumen_texto"] == "Hola mundo"
    assert d["secciones"][1]["actividades"][0]["tipo_label"] == "Cuestionario"
    assert len(d["proximas_entregas"]) == 2


def test_calendario_unavailable_no_inventa_fechas():
    d = appmod._navegacion_payload(_est(), {"status": "unavailable", "message": "No disponible"})
    assert d["fechas_status"] == "unavailable"
    assert d["fechas_message"] == "No disponible"
    assert d["proximas_entregas"] == []
    assert all(a["vence"] is None for s in d["secciones"] for a in s["actividades"])


def test_actividad_oculta_se_marca():
    d = appmod._navegacion_payload(_est(), {"status": "unavailable"})
    assert d["secciones"][1]["actividades"][0]["visible"] is False
