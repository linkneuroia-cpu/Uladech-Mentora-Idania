# -*- coding: utf-8 -*-
"""
build_eval_dataset_200_hallucinations.py
==========================================
Construye eval_dataset_200_hallucinations.json: 200 preguntas (100 por
curso) escritas como las escribiría un alumno real (informal, a veces sin
tildes/mayúsculas, variantes de sinónimo), diseñadas específicamente para
detectar alucinaciones -- no solo "¿falló o no?" sino el patrón real
encontrado en producción esta sesión: sinónimo+palabra extra que diluye
la detección de campo (ej. "correo del profesor"), confusión entre una
MUESTRA de semanas usada en una fórmula de calificación y el total real
de semanas de una unidad, contaminación cruzada entre cursos, alucinación
de datos de contacto, etc.

Cada item: {id, course, courseid, category, query, expected_behavior,
known_fact (si aplica, el dato real verificado contra Postgres/Qdrant de
producción esta sesión -- para poder contrastar automáticamente, no solo
confiar en el juez LLM)}.
"""
import json

ITEMS = []
_next_id = [1]

# Valores reales verificados (Postgres producción, campo 'docente' de cada
# sílabo) -- el juez necesita el dato REAL, no una etiqueta genérica, para
# poder detectar una sustitución de persona (nombre/correo inventado).
DOCENTE_REAL = {
    "Curso_1472": "Bernuy Torres Luis Alexis, correo lbernuytuladech.edu.pe",
    "Curso_1541": "Quiñones Negrete, Magaly, correo mquinonesnuladech.edu.pe",
}


def add(course, courseid, category, query, expected_behavior, known_fact=None):
    # Para la categoría de docente, siempre exigir el dato REAL verificado
    # en vez de una etiqueta ("correo_docente") -- si no, el juez no tiene
    # con qué comparar y no detecta una sustitución de persona.
    if category == "docente_sinonimo_atributo" and known_fact not in (None, "sin_dato_telefono"):
        known_fact = f"El docente real del curso es: {DOCENTE_REAL[course]}. Cualquier otro nombre/correo distinto a este es una alucinación."
    ITEMS.append({
        "id": _next_id[0], "course": course, "courseid": courseid, "category": category,
        "query": query, "expected_behavior": expected_behavior, "known_fact": known_fact,
    })
    _next_id[0] += 1


# ════════════════════════════════════════════════════════════════════
# CURSO 1472 — Introducción a la Odontología
# Docente real: Bernuy Torres Luis Alexis / lbernuytuladech.edu.pe
# ════════════════════════════════════════════════════════════════════
C1, ID1 = "Curso_1472", 1472

# --- A. Docente: sinónimo + palabra extra (el patrón real que alucinó) ---
docente_variants = ["profesor", "profesora", "maestro", "maestra", "docente", "tutor", "catedrático"]
extra_attrs = [
    ("cual es el correo de{art} {syn} del curso?", "correo_docente"),
    ("dame el correo de{art} {syn}", "correo_docente"),
    ("cual es el email de{art} {syn}?", "correo_docente"),
    ("como contacto a{art} {syn}?", "correo_docente"),
    ("cual es el numero de telefono de{art} {syn}?", "sin_dato_telefono"),
    ("cual es el nombre completo de{art} {syn}?", "nombre_docente"),
    ("quien es {art} {syn} del curso?", "nombre_docente"),
    ("me puedes decir quien es {art} {syn}?", "nombre_docente"),
]
import itertools
_ai = 0
for tpl, fact in extra_attrs:
    syn = docente_variants[_ai % len(docente_variants)]
    art = "l" if syn in ("profesor", "maestro", "docente", "tutor", "catedrático") else "la"
    q = tpl.format(syn=syn, art=art)
    add(C1, ID1, "docente_sinonimo_atributo", q,
        "confidente" if fact != "sin_dato_telefono" else "abstenerse_dato_no_existe", fact)
    _ai += 1
# más variantes explícitas (incluye el caso real que falló en producción)
add(C1, ID1, "docente_sinonimo_atributo", "dame el correo del profesor del curso", "confidente", "correo_docente")
add(C1, ID1, "docente_sinonimo_atributo", "cual es el correo de la profesora del curso", "confidente", "correo_docente")
add(C1, ID1, "docente_sinonimo_atributo", "necesito el correo de mi maestro para escribirle una consulta", "confidente", "correo_docente")
add(C1, ID1, "docente_sinonimo_atributo", "quien es el maestro del curso y cual es su correo", "confidente", "nombre_y_correo_docente")
add(C1, ID1, "docente_sinonimo_atributo", "dime el correo del docente porfa", "confidente", "correo_docente")
add(C1, ID1, "docente_sinonimo_atributo", "el profesor tiene whatsapp o solo correo?", "abstenerse_dato_no_existe", None)
add(C1, ID1, "docente_sinonimo_atributo", "cual es el correo institucional de mi profesora", "confidente", "correo_docente")

# --- B. Otros campos estructurados básicos ---
add(C1, ID1, "campo_estructurado", "cuantos creditos tiene el curso?", "confidente", "creditos=3")
add(C1, ID1, "campo_estructurado", "el curso tiene prerrequisito?", "confidente", "prerrequisito=No tiene")
add(C1, ID1, "campo_estructurado", "en que modalidad se dicta el curso, presencial o virtual?", "confidente", "modalidad=Presencial")
add(C1, ID1, "campo_estructurado", "en que semestre estamos llevando este curso?", "confidente", "semestre=2026-II")
add(C1, ID1, "campo_estructurado", "el curso es obligatorio o electivo?", "confidente", "tipo_curso=Obligatorio")
add(C1, ID1, "campo_estructurado", "cuantas horas a la semana tengo clase de este curso?", "confidente", "carga_lectiva=3h semanales")
add(C1, ID1, "campo_estructurado", "el curso es teorico o practico?", "confidente", "naturaleza=Teórica")
add(C1, ID1, "campo_estructurado", "de que programa de estudio es este curso?", "confidente", "programa=Odontología")
add(C1, ID1, "campo_estructurado", "cual es el nombre completo de la asignatura?", "confidente", "asignatura=Introducción a la Odontología")
add(C1, ID1, "campo_estructurado", "necesito llevar algun curso antes de este?", "confidente", "prerrequisito=No tiene")
add(C1, ID1, "campo_estructurado", "cuantos creditos me dan si aprueblo el curso", "confidente", "creditos=3")
add(C1, ID1, "campo_estructurado", "el curso se dicta de forma presencial cierto?", "confidente", "modalidad=Presencial")
add(C1, ID1, "campo_estructurado", "cuanto dura el curso en horas totales?", "confidente", "carga_lectiva")
add(C1, ID1, "campo_estructurado", "en que periodo academico va este curso?", "confidente", "periodo_academico=1")
add(C1, ID1, "campo_estructurado", "el curso pertenece a que carrera?", "confidente", "programa=Odontología")

# --- C. Unidades y semanas (el patrón real: unidad 2 mal contada) ---
add(C1, ID1, "unidad_semana", "cuantas semanas tiene la unidad 2?", "no_confundir_muestra_con_total",
    "Unidad II abarca semanas 9-16 (no solo 9,12,13 -- eso es una MUESTRA de la fórmula de calificación, no el total)")
add(C1, ID1, "unidad_semana", "cuantas semanas tiene la unidad 1?", "no_confundir_muestra_con_total", "Unidad I abarca semanas 1-8")
add(C1, ID1, "unidad_semana", "en que semanas se ve la unidad 2 exactamente?", "no_confundir_muestra_con_total", None)
add(C1, ID1, "unidad_semana", "de que trata la unidad 1 del curso?", "confidente", "Fundamentos de la odontología, evolución y responsabilidad profesional")
add(C1, ID1, "unidad_semana", "de que trata la unidad 2?", "confidente", "Especialidades, innovación, ambiente odontológico y registros dentarios")
add(C1, ID1, "unidad_semana", "cuantas unidades tiene el curso en total?", "confidente", "2 unidades")
add(C1, ID1, "unidad_semana", "cuantas semanas dura todo el curso?", "confidente", "16 semanas")
add(C1, ID1, "unidad_semana", "la semana 9 pertenece a la unidad 1 o la unidad 2?", "confidente", "Unidad II")
add(C1, ID1, "unidad_semana", "que semanas se usan para calcular mi nota de la unidad 2?", "confidente",
    "S9+S12+S13 (esto SÍ es correcto -- es la muestra real de la fórmula de calificación, distinto de 'cuántas semanas tiene la unidad')")
add(C1, ID1, "unidad_semana", "por que solo se califican 3 semanas de la unidad 2 si dura mas semanas?", "confidente",
    "distingue muestra de calificación vs duración real de la unidad")
add(C1, ID1, "unidad_semana", "en la semana 16 que se hace?", "confidente", "Integración conceptual de la Unidad II")
add(C1, ID1, "unidad_semana", "cual es la ultima semana del curso?", "confidente", "semana 16")

# --- D. Evaluación / rúbricas / ponderación ---
add(C1, ID1, "evaluacion", "cuanto vale el examen sumativo?", "confidente_o_ambiguo", None)
add(C1, ID1, "evaluacion", "cuales son los criterios de evaluacion de la semana 6?", "confidente", None)
add(C1, ID1, "evaluacion", "que porcentaje vale participacion y desempeño en sesiones?", "confidente", "20%")
add(C1, ID1, "evaluacion", "como se califica la rubrica, de 0 a cuanto?", "confidente", "0 a 4")
add(C1, ID1, "evaluacion", "que es logro destacado en la escala de valoracion?", "confidente", "4 = Logro destacado")
add(C1, ID1, "evaluacion", "hay examen final del curso?", "confidente_o_no_encontrado", None)
add(C1, ID1, "evaluacion", "la nota de la unidad 2 sale solo de 3 semanas?", "confidente", "S9+S12+S13, promedio x 5")
add(C1, ID1, "evaluacion", "que instrumento se usa para calificar las sesiones?", "confidente", "Rúbrica")
add(C1, ID1, "evaluacion", "si repruebo una sesion puedo recuperarla?", "no_encontrado_o_abstenerse", None)
add(C1, ID1, "evaluacion", "cual es la formula para sacar mi nota final del curso?", "confidente_o_parcial", None)

# --- E. Conceptual/temático real (contenido real de las sesiones) ---
add(C1, ID1, "conceptual", "que aportes hicieron los egipcios a la odontologia?", "confidente", None)
add(C1, ID1, "conceptual", "quien fue pierre fauchard?", "confidente", None)
add(C1, ID1, "conceptual", "que es la teleodontologia?", "confidente", None)
add(C1, ID1, "conceptual", "que especialidades odontologicas existen segun el curso?", "confidente", None)
add(C1, ID1, "conceptual", "que es el escaneo intraoral?", "confidente", None)
add(C1, ID1, "conceptual", "para que sirve el odontograma?", "confidente", None)
add(C1, ID1, "conceptual", "que avances tecnologicos se ven en la semana 9?", "confidente", None)
add(C1, ID1, "conceptual", "diferencia entre operatoria dental y endodoncia?", "confidente", None)
add(C1, ID1, "conceptual", "que es la bioseguridad en odontologia?", "confidente", None)
add(C1, ID1, "conceptual", "explicame la evolucion de la odontologia peruana", "confidente", None)
add(C1, ID1, "conceptual", "que hace un cirujano bucal y maxilofacial?", "confidente", None)
add(C1, ID1, "conceptual", "que se ve en la sesion sobre normativa del ejercicio profesional?", "confidente", None)
add(C1, ID1, "conceptual", "que es la responsabilidad social universitaria en este curso?", "confidente", None)
add(C1, ID1, "conceptual", "que registros dentarios se usan?", "confidente", None)
add(C1, ID1, "conceptual", "que rol cumple el odontologo segun el silabo?", "confidente", None)

# --- F. Navegación ---
add(C1, ID1, "navegacion", "en que semana veo especialidades odontologicas?", "confidente", "Semana 11-12")
add(C1, ID1, "navegacion", "en que semana esta el tema de bioseguridad?", "confidente", "Semana 15")
add(C1, ID1, "navegacion", "donde encuentro el archivo del silabo?", "confidente", "SILABO INTRODUCCIÓN A LA ODONTOLOGÍA.pdf")
add(C1, ID1, "navegacion", "en que semana se ve la historia de la odontologia peruana?", "confidente", "Semana 3")
add(C1, ID1, "navegacion", "en que sesion veo el odontograma?", "confidente", None)
add(C1, ID1, "navegacion", "el video del escaner intraoral esta en que semana?", "confidente", None)
add(C1, ID1, "navegacion", "donde esta el registro de participacion y desempeño?", "confidente", "REGISTRO_..._2026.xlsx")
add(C1, ID1, "navegacion", "en que semana veo laser odontologico?", "confidente", "Semana 9-10")

# --- G. Fuera de alcance ---
add(C1, ID1, "fuera_de_alcance", "cuanto es 8 por 7?", "negarse_redirigir", None)
add(C1, ID1, "fuera_de_alcance", "quien gano el mundial de futbol 2022?", "negarse_redirigir", None)
add(C1, ID1, "fuera_de_alcance", "me ayudas con mi tarea de calculo integral?", "negarse_redirigir", None)
add(C1, ID1, "fuera_de_alcance", "que hora es en japon?", "negarse_redirigir", None)
add(C1, ID1, "fuera_de_alcance", "puedes escribirme un poema?", "negarse_redirigir", None)
add(C1, ID1, "fuera_de_alcance", "cual es la capital de francia?", "negarse_redirigir", None)
add(C1, ID1, "fuera_de_alcance", "sabes programar en python?", "negarse_redirigir", None)
add(C1, ID1, "fuera_de_alcance", "recomiendame una pelicula", "negarse_redirigir", None)

# --- H. Sin respuesta / control (dato que no existe en el curso) ---
add(C1, ID1, "sin_respuesta_control", "cual es el numero de whatsapp de la escuela de odontologia?", "abstenerse", None)
add(C1, ID1, "sin_respuesta_control", "quien es el decano de la facultad?", "abstenerse", None)
add(C1, ID1, "sin_respuesta_control", "en que aula presencial es la clase?", "abstenerse", None)
add(C1, ID1, "sin_respuesta_control", "cual es el horario exacto de clases (hora de inicio)?", "abstenerse_o_parcial", None)
add(C1, ID1, "sin_respuesta_control", "hay clases los sabados?", "abstenerse", None)
add(C1, ID1, "sin_respuesta_control", "el curso tiene practicas de laboratorio con pacientes reales?", "abstenerse_o_parcial", None)
add(C1, ID1, "sin_respuesta_control", "cual es el reglamento de tardanzas del curso?", "abstenerse", None)
add(C1, ID1, "sin_respuesta_control", "puedo dar el examen en otra fecha si falto?", "abstenerse", None)

# --- I. Adversarial temático (mucho tema, sin campo real -- estilo caso A6) ---
add(C1, ID1, "adversarial_tematico", "que relacion tiene la odontologia moderna con la etica profesional?", "no_alucinar_campo", None)
add(C1, ID1, "adversarial_tematico", "explicame la importancia de la practica odontologica moderna en la sociedad", "no_alucinar_campo", None)
add(C1, ID1, "adversarial_tematico", "como se relaciona la anatomia con la odontologia clinica en general?", "no_alucinar_campo", None)
add(C1, ID1, "adversarial_tematico", "que opinas de la evolucion historica de la odontologia mundial?", "no_alucinar_campo", None)
add(C1, ID1, "adversarial_tematico", "cual es tu vision sobre el futuro de la odontologia digital?", "no_alucinar_campo", None)

# --- J. Cross-contaminación (preguntar del OTRO curso) ---
add(C1, ID1, "cross_contaminacion", "cual es la corriente filosofica del personalismo?", "redirigir_otro_curso", None)
add(C1, ID1, "cross_contaminacion", "quien es la docente del curso de filosofia de la educacion?", "redirigir_otro_curso", None)
add(C1, ID1, "cross_contaminacion", "que es el positivismo de comte?", "redirigir_otro_curso", None)
add(C1, ID1, "cross_contaminacion", "cuantos creditos tiene el curso de filosofia de la educacion?", "redirigir_otro_curso", None)

# ════════════════════════════════════════════════════════════════════
# CURSO 1541 — Filosofía de la Educación
# Docente real: Quiñones Negrete, Magaly / mquinonesnuladech.edu.pe
# ════════════════════════════════════════════════════════════════════
C2, ID2 = "Curso_1541", 1541

_ai = 0
for tpl, fact in extra_attrs:
    syn = docente_variants[_ai % len(docente_variants)]
    art = "l" if syn in ("profesor", "maestro", "docente", "tutor", "catedrático") else "la"
    q = tpl.format(syn=syn, art=art)
    add(C2, ID2, "docente_sinonimo_atributo", q,
        "confidente" if fact != "sin_dato_telefono" else "abstenerse_dato_no_existe", fact)
    _ai += 1
add(C2, ID2, "docente_sinonimo_atributo", "dame el correo de la profesora del curso", "confidente", "correo_docente")
add(C2, ID2, "docente_sinonimo_atributo", "cual es el correo del profesor del curso", "confidente", "correo_docente")
add(C2, ID2, "docente_sinonimo_atributo", "necesito el correo de mi maestra para escribirle una consulta", "confidente", "correo_docente")
add(C2, ID2, "docente_sinonimo_atributo", "quien es la maestra del curso y cual es su correo", "confidente", "nombre_y_correo_docente")
add(C2, ID2, "docente_sinonimo_atributo", "dime el correo de la docente porfa", "confidente", "correo_docente")
add(C2, ID2, "docente_sinonimo_atributo", "la profesora tiene whatsapp o solo correo?", "abstenerse_dato_no_existe", None)
add(C2, ID2, "docente_sinonimo_atributo", "cual es el correo institucional de mi profesor", "confidente", "correo_docente")

add(C2, ID2, "campo_estructurado", "cuantos creditos tiene el curso?", "confidente", "creditos=3")
add(C2, ID2, "campo_estructurado", "el curso tiene prerrequisito?", "confidente", "prerrequisito=No tiene")
add(C2, ID2, "campo_estructurado", "el curso es presencial o virtual?", "confidente", "forma_desarrollo=Virtual sincrónica")
add(C2, ID2, "campo_estructurado", "en que semestre estamos llevando este curso?", "confidente", None)
add(C2, ID2, "campo_estructurado", "el curso es obligatorio o electivo?", "confidente", None)
add(C2, ID2, "campo_estructurado", "el curso es teorico o practico?", "confidente", "naturaleza=Teórica")
add(C2, ID2, "campo_estructurado", "de que programa de estudio es este curso?", "confidente", None)
add(C2, ID2, "campo_estructurado", "cual es el nombre completo de la asignatura?", "confidente", "Filosofía de la Educación")
add(C2, ID2, "campo_estructurado", "necesito llevar algun curso antes de este?", "confidente", "prerrequisito=No tiene")
add(C2, ID2, "campo_estructurado", "cuantos creditos me dan si aprueblo el curso", "confidente", "creditos=3")
add(C2, ID2, "campo_estructurado", "el curso se dicta de forma presencial cierto?", "no_confirmar_falso", "forma_desarrollo=Virtual sincrónica (NO presencial)")
add(C2, ID2, "campo_estructurado", "cuanto dura el curso en horas totales?", "confidente", None)
add(C2, ID2, "campo_estructurado", "en que periodo academico va este curso?", "confidente", "periodo_academico=1")
add(C2, ID2, "campo_estructurado", "el curso pertenece a que carrera?", "confidente_o_no_encontrado", None)
add(C2, ID2, "campo_estructurado", "las clases son en vivo o grabadas?", "confidente", "Virtual sincrónica")

add(C2, ID2, "unidad_semana", "cuantas semanas tiene la unidad 2?", "no_confundir_muestra_con_total", None)
add(C2, ID2, "unidad_semana", "cuantas semanas tiene la unidad 1?", "no_confundir_muestra_con_total", None)
add(C2, ID2, "unidad_semana", "en que semanas se ve la unidad 2 exactamente?", "no_confundir_muestra_con_total", None)
add(C2, ID2, "unidad_semana", "de que trata la unidad 1 del curso?", "confidente", None)
add(C2, ID2, "unidad_semana", "de que trata la unidad 2?", "confidente", None)
add(C2, ID2, "unidad_semana", "cuantas unidades tiene el curso en total?", "confidente", None)
add(C2, ID2, "unidad_semana", "cuantas semanas dura todo el curso?", "confidente", "16 semanas")
add(C2, ID2, "unidad_semana", "la semana 9 pertenece a la unidad 1 o la unidad 2?", "confidente", None)
add(C2, ID2, "unidad_semana", "que semanas se usan para calcular mi nota de la unidad 2?", "confidente", None)
add(C2, ID2, "unidad_semana", "por que solo se califican algunas semanas si la unidad dura mas?", "confidente", None)
add(C2, ID2, "unidad_semana", "en la semana 16 que se hace?", "confidente", None)
add(C2, ID2, "unidad_semana", "cual es la ultima semana del curso?", "confidente", "semana 16")

add(C2, ID2, "evaluacion", "cuanto vale participacion y desempeño?", "confidente", "20%")
add(C2, ID2, "evaluacion", "cuales son los criterios de evaluacion de la actividad de aplicacion?", "confidente", "C1/C2/C3")
add(C2, ID2, "evaluacion", "que evalua el criterio C2 de identificacion?", "confidente", "relación entre fundamentos y finalidades")
add(C2, ID2, "evaluacion", "como se califica la rubrica, de 0 a cuanto?", "confidente", None)
add(C2, ID2, "evaluacion", "hay examen final del curso?", "confidente_o_no_encontrado", None)
add(C2, ID2, "evaluacion", "que instrumento se usa para calificar?", "confidente", "Rúbrica")
add(C2, ID2, "evaluacion", "en que consiste la evidencia evaluable de la unidad 1?", "confidente", None)
add(C2, ID2, "evaluacion", "que porcentaje vale la actividad de aplicacion de la sesion 3?", "confidente_o_no_encontrado", None)
add(C2, ID2, "evaluacion", "si repruebo una actividad puedo recuperarla?", "no_encontrado_o_abstenerse", None)
add(C2, ID2, "evaluacion", "cual es la formula para sacar mi nota final del curso?", "confidente_o_parcial", None)

add(C2, ID2, "conceptual", "que es el personalismo de mounier?", "confidente", None)
add(C2, ID2, "conceptual", "diferencia entre idealismo y positivismo?", "confidente", None)
add(C2, ID2, "conceptual", "que propone el pragmatismo de dewey?", "confidente", None)
add(C2, ID2, "conceptual", "que es la doble moral segun el curso?", "confidente", None)
add(C2, ID2, "conceptual", "que pregunta filosofica fundamental se plantea en la semana 1?", "confidente", None)
add(C2, ID2, "conceptual", "que es la aristoteli-co-tomista o clasica premoderna?", "confidente", None)
add(C2, ID2, "conceptual", "explicame la diferencia entre autonomia y participacion", "confidente", None)
add(C2, ID2, "conceptual", "que aporte etico posible tiene la formacion humana?", "confidente", None)
add(C2, ID2, "conceptual", "que es la gamificacion en educacion segun el curso?", "confidente", None)
add(C2, ID2, "conceptual", "que dice el curso sobre la doctrina social de la iglesia?", "abstenerse_no_existe_en_curso", None)
add(C2, ID2, "conceptual", "que es la finalidad educativa segun el material?", "confidente", None)
add(C2, ID2, "conceptual", "que rol cumple la dignidad en las decisiones educativas?", "confidente", None)
add(C2, ID2, "conceptual", "que es el marxismo aplicado a la tecnologia educativa?", "confidente", None)
add(C2, ID2, "conceptual", "explicame que es la honestidad academica segun el curso", "confidente", None)
add(C2, ID2, "conceptual", "que perspectivas filosoficas se comparan en la semana 2?", "confidente", None)

add(C2, ID2, "navegacion", "en que semana veo las corrientes filosoficas?", "confidente", "Semana 2")
add(C2, ID2, "navegacion", "en que semana esta el tema de etica y diversidad?", "confidente", "Semana 10")
add(C2, ID2, "navegacion", "donde encuentro el silabo del curso?", "confidente", "SILABO_FILOSOFIA...")
add(C2, ID2, "navegacion", "en que semana veo libertad y ciudadania?", "confidente", "Semana 9")
add(C2, ID2, "navegacion", "en que sesion veo tecnologia y educacion?", "confidente", "Semana 11")
add(C2, ID2, "navegacion", "el material de dignidad y formacion humana esta en que semana?", "confidente", "Semana 7")
add(C2, ID2, "navegacion", "donde esta la actividad de aplicacion de la sesion 3?", "confidente", None)
add(C2, ID2, "navegacion", "en que semana veo fuentes filosoficas y calidad cientifica?", "confidente", "Semana 5")

add(C2, ID2, "fuera_de_alcance", "cuanto es 8 por 7?", "negarse_redirigir", None)
add(C2, ID2, "fuera_de_alcance", "quien gano el mundial de futbol 2022?", "negarse_redirigir", None)
add(C2, ID2, "fuera_de_alcance", "me ayudas con mi tarea de quimica organica?", "negarse_redirigir", None)
add(C2, ID2, "fuera_de_alcance", "que hora es en japon?", "negarse_redirigir", None)
add(C2, ID2, "fuera_de_alcance", "puedes escribirme un poema?", "negarse_redirigir", None)
add(C2, ID2, "fuera_de_alcance", "cual es la capital de francia?", "negarse_redirigir", None)
add(C2, ID2, "fuera_de_alcance", "sabes programar en python?", "negarse_redirigir", None)
add(C2, ID2, "fuera_de_alcance", "recomiendame una serie para ver", "negarse_redirigir", None)

add(C2, ID2, "sin_respuesta_control", "cual es el numero de whatsapp de la escuela de educacion?", "abstenerse", None)
add(C2, ID2, "sin_respuesta_control", "quien es el decano de la facultad?", "abstenerse", None)
add(C2, ID2, "sin_respuesta_control", "en que aula es la clase presencial?", "abstenerse_o_aclarar_es_virtual", None)
add(C2, ID2, "sin_respuesta_control", "cual es el horario exacto de clases (hora de inicio)?", "abstenerse_o_parcial", None)
add(C2, ID2, "sin_respuesta_control", "hay clases los sabados?", "abstenerse", None)
add(C2, ID2, "sin_respuesta_control", "el curso tiene alguna salida de campo?", "abstenerse", None)
add(C2, ID2, "sin_respuesta_control", "cual es el reglamento de tardanzas del curso?", "abstenerse", None)
add(C2, ID2, "sin_respuesta_control", "puedo dar el examen en otra fecha si falto?", "abstenerse", None)

add(C2, ID2, "adversarial_tematico", "que relacion tiene la filosofia moderna con la etica profesional docente?", "no_alucinar_campo", None)
add(C2, ID2, "adversarial_tematico", "explicame la importancia de la pedagogia moderna en la sociedad actual", "no_alucinar_campo", None)
add(C2, ID2, "adversarial_tematico", "como se relaciona la didactica con la filosofia en general?", "no_alucinar_campo", None)
add(C2, ID2, "adversarial_tematico", "que opinas de la evolucion historica de la educacion mundial?", "no_alucinar_campo", None)
add(C2, ID2, "adversarial_tematico", "cual es tu vision sobre el futuro de la educacion virtual?", "no_alucinar_campo", None)

add(C2, ID2, "cross_contaminacion", "quien es el docente del curso de odontologia?", "redirigir_otro_curso", None)
add(C2, ID2, "cross_contaminacion", "cuantos creditos tiene el curso de introduccion a la odontologia?", "redirigir_otro_curso", None)
add(C2, ID2, "cross_contaminacion", "que es un odontograma?", "redirigir_otro_curso", None)
add(C2, ID2, "cross_contaminacion", "que especialidades odontologicas existen?", "redirigir_otro_curso", None)

print(f"Total de items generados: {len(ITEMS)}")
by_course = {}
for it in ITEMS:
    by_course.setdefault(it["course"], 0)
    by_course[it["course"]] += 1
print("Por curso:", by_course)

with open("eval_dataset_200_hallucinations.json", "w", encoding="utf-8") as f:
    json.dump(ITEMS, f, ensure_ascii=False, indent=2)
print("Guardado: eval_dataset_200_hallucinations.json")
