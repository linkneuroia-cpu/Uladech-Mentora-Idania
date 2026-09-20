# -*- coding: utf-8 -*-
"""
build_eval_dataset_200_v2.py
==============================
Segunda tanda de 200 preguntas (100 por curso) para el mismo tipo de
evaluación de alucinaciones que eval_dataset_200_hallucinations.json (batch
1), pero deliberadamente sobre OTROS temas/semanas/archivos -- el batch 1 se
apoyó mucho en el sílabo y en unas pocas semanas repetidas (3, 9, 11-12, 15
para 1472; semana 2 y el sílabo para 1541). Este batch cubre semanas y
archivos que el batch 1 casi no tocó: semanas 1, 4, 5, 6, 7, 10, 13, 14 y el
xlsx de registro para 1472; semanas 1, 3, 4, 6, 8, 12, 13, 14, 16 y el xlsx
de registro para 1541.

Todo known_fact de categoría conceptual/navegacion/unidad_semana fue
verificado leyendo directamente el texto real de los chunks indexados en el
mirror local de Qdrant (scroll con filtro por filename) -- no se inventó
ningún dato. Fuentes verificadas puntuales quedan documentadas en los
comentarios junto a cada bloque.

También se incluyen regresiones de los 3 bugs corregidos esta sesión
(docente+sinónimo+atributo extra, modalidad/forma de desarrollo, muestra vs
total de semanas), con frases y campos DISTINTOS a los usados en el batch 1,
para confirmar que el fix generaliza y no fue sobreajustado.

Hallazgo nuevo relevante encontrado al construir este dataset (verificado en
los xlsx de registro de ambos cursos vía Qdrant):
- Curso 1472: "Participación y desempeño" de cada unidad se califica con
  una MUESTRA de solo 3 semanas (Unidad I: S1+S3+S4: Unidad II: S9+S12+S13),
  aunque cada unidad dura 8 semanas completas.
- Curso 1541: es el patrón INVERSO -- se califica con CASI TODAS las semanas
  de la unidad (Unidad I: S1..S7, se excluye solo S8 que es el examen;
  Unidad II: S9..S15, se excluye solo S16 que es la evaluación sumativa).
Este contraste es intencional: sirve para probar que el fix de "no confundir
muestra con total" generaliza en ambas direcciones y no se sobreajustó al
patrón específico de 1472 (3 de 8 semanas).

Cada item: {id, course, courseid, category, query, expected_behavior,
known_fact}.
"""
import json

ITEMS = []
_next_id = [1]

DOCENTE_REAL = {
    "Curso_1472": "Bernuy Torres Luis Alexis, correo lbernuytuladech.edu.pe",
    "Curso_1541": "Quiñones Negrete, Magaly, correo mquinonesnuladech.edu.pe",
}


def add(course, courseid, category, query, expected_behavior, known_fact=None):
    if category == "docente_sinonimo_atributo" and known_fact not in (None, "sin_dato_telefono", "sin_dato_horario_oficina"):
        known_fact = f"El docente real del curso es: {DOCENTE_REAL[course]}. Cualquier otro nombre/correo distinto a este es una alucinación."
    ITEMS.append({
        "id": _next_id[0], "course": course, "courseid": courseid, "category": category,
        "query": query, "expected_behavior": expected_behavior, "known_fact": known_fact,
    })
    _next_id[0] += 1


# ════════════════════════════════════════════════════════════════════
# CURSO 1472 — Introducción a la Odontología
# ════════════════════════════════════════════════════════════════════
C1, ID1 = "Curso_1472", 1472

# --- A. Docente: sinónimo + atributo extra, frases NUEVAS (15) ---
add(C1, ID1, "docente_sinonimo_atributo", "oye cual es el correo del instructor del curso porfa", "confidente", "correo_docente")
add(C1, ID1, "docente_sinonimo_atributo", "necesito hablar con la profe, me pasas su correo?", "confidente", "correo_docente")
add(C1, ID1, "docente_sinonimo_atributo", "cual es el apellido completo de mi profesor?", "confidente", "nombre_docente")
add(C1, ID1, "docente_sinonimo_atributo", "quien dicta este curso y a que correo le escribo?", "confidente", "nombre_y_correo_docente")
add(C1, ID1, "docente_sinonimo_atributo", "el encargado del curso tiene horario de tutoria?", "abstenerse_dato_no_existe", "sin_dato_horario_oficina")
add(C1, ID1, "docente_sinonimo_atributo", "cual es el correo del que nos enseña odontologia?", "confidente", "correo_docente")
add(C1, ID1, "docente_sinonimo_atributo", "sabes el grado academico de mi maestro? o solo su correo", "confidente", "correo_docente")
add(C1, ID1, "docente_sinonimo_atributo", "el profe tiene numero de celular por si hay emergencia?", "abstenerse_dato_no_existe", "sin_dato_telefono")
add(C1, ID1, "docente_sinonimo_atributo", "me confirmas el nombre y correo de la persona que dicta el curso?", "confidente", "nombre_y_correo_docente")
add(C1, ID1, "docente_sinonimo_atributo", "a quien le mando mi trabajo, cual es el correo del docente?", "confidente", "correo_docente")
add(C1, ID1, "docente_sinonimo_atributo", "el catedratico responde rapido los correos o hay que esperar?", "abstenerse_dato_no_existe", None)
add(C1, ID1, "docente_sinonimo_atributo", "necesito el correo del profe para justificar una inasistencia", "confidente", "correo_docente")
add(C1, ID1, "docente_sinonimo_atributo", "quien es la persona responsable de calificarnos en este curso?", "confidente", "nombre_docente")
add(C1, ID1, "docente_sinonimo_atributo", "dame el correo de mi tutor de curso plis", "confidente", "correo_docente")
add(C1, ID1, "docente_sinonimo_atributo", "en que oficina atiende el profesor del curso?", "abstenerse_dato_no_existe", None)

# --- B. Campo estructurado, frases NUEVAS + 1 dato nuevo verificado (15) ---
# Duración de sesión verificada en SESION APRENDIZAJE 1.docx / 5.docx / 10.docx / 13.docx / 14.docx:
# "3 horas lectivas teóricas (3T). Total: 135 minutos, considerando 45 minutos por hora lectiva"
add(C1, ID1, "campo_estructurado", "cuantos minutos dura cada sesion de clase?", "confidente", "135 minutos (3 horas lectivas de 45 min c/u)")
add(C1, ID1, "campo_estructurado", "el curso se lleva de forma presencial o es virtual?", "confidente", "modalidad=Presencial")
add(C1, ID1, "campo_estructurado", "hay que haber llevado otro curso antes para matricularme en este?", "confidente", "prerrequisito=No tiene")
add(C1, ID1, "campo_estructurado", "a que carrera pertenece esta asignatura?", "confidente", "programa=Odontología")
add(C1, ID1, "campo_estructurado", "el curso me da 3 o 4 creditos?", "confidente", "creditos=3")
add(C1, ID1, "campo_estructurado", "estamos en que periodo del año academico?", "confidente", "semestre=2026-II")
add(C1, ID1, "campo_estructurado", "este curso es jalable, o sea obligatorio, o es electivo?", "confidente", "tipo_curso=Obligatorio")
add(C1, ID1, "campo_estructurado", "cual es el nombre oficial completo de este curso?", "confidente", "asignatura=Introducción a la Odontología")
add(C1, ID1, "campo_estructurado", "es un curso mas teorico o mas practico?", "confidente", "naturaleza=Teórica")
add(C1, ID1, "campo_estructurado", "cuantas horas academicas a la semana tiene el curso?", "confidente", "carga_lectiva=3h semanales")
add(C1, ID1, "campo_estructurado", "necesito saber si este curso tiene requisito previo para llevarlo", "confidente", "prerrequisito=No tiene")
add(C1, ID1, "campo_estructurado", "de cuantos creditos consta esta asignatura?", "confidente", "creditos=3")
add(C1, ID1, "campo_estructurado", "las clases son presenciales verdad?", "confidente", "modalidad=Presencial")
add(C1, ID1, "campo_estructurado", "en que ciclo o periodo academico esta programado el curso?", "confidente", "periodo_academico=1")
add(C1, ID1, "campo_estructurado", "el curso pertenece al programa de que carrera?", "confidente", "programa=Odontología")

# --- C. Unidad/semana: regresión con frases y ángulos NUEVOS (12) ---
# Verificado en REGISTRO_INTRODUCCION_ODONTOLOGIA_PARTICIPACION_DESEMPENO_2026.xlsx:
# "Configuración solicitada: Unidad I = semanas 1, 3 y 4; Unidad II = semanas 9, 12 y 13"
# (son la MUESTRA para la nota de participación, NO el total de semanas de cada unidad).
add(C1, ID1, "unidad_semana", "la unidad 1 completa dura cuantas semanas (no solo las que califican)?", "no_confundir_muestra_con_total", "Unidad I abarca semanas 1-8 (8 semanas), aunque solo S1+S3+S4 se usan para la nota de participación")
add(C1, ID1, "unidad_semana", "para la nota de participacion de la unidad 1 que semanas entran exactamente?", "confidente", "S1 + S3 + S4 (esto SÍ es correcto: es la muestra real usada en el registro de participación y desempeño)")
add(C1, ID1, "unidad_semana", "si la unidad 1 va de la semana 1 a la 8, por que en el registro de notas solo aparecen 3 semanas (1, 3 y 4)?", "confidente", "Porque el registro de Participación y desempeño usa una muestra (S1+S3+S4), no todas las semanas de la unidad")
add(C1, ID1, "unidad_semana", "la semana 4 pertenece a la unidad 1 o a la unidad 2?", "confidente", "Unidad I")
add(C1, ID1, "unidad_semana", "cuantas semanas en total tiene la unidad 2 del curso?", "no_confundir_muestra_con_total", "Unidad II abarca semanas 9-16 (8 semanas), no solo 9,12,13")
add(C1, ID1, "unidad_semana", "el curso completo cuantas semanas tiene y en cuantas unidades se divide?", "confidente", "16 semanas, 2 unidades (Unidad I: semanas 1-8, Unidad II: semanas 9-16)")
add(C1, ID1, "unidad_semana", "que temas se ven en la semana 8?", "abstenerse", "No existe contenido indexado para 'Semana 08' en el curso 1472 (hay salto de la semana 7 a la semana 9 en el material). Cualquier tema inventado para la semana 8 es una alucinación.")
add(C1, ID1, "unidad_semana", "hay clases en la semana 8 o se salta directo a la semana 9?", "confidente", "No hay material indexado de una 'Semana 08'; el material salta de semana 7 a semana 9")
add(C1, ID1, "unidad_semana", "la semana 13 es parte de la unidad 1 o de la unidad 2?", "confidente", "Unidad II")
add(C1, ID1, "unidad_semana", "cual es el tema de la primera semana del curso?", "confidente", "Introducción a la odontología: definición, campo de acción, relación con otras ciencias de la salud y rol del cirujano dentista")
add(C1, ID1, "unidad_semana", "de que trata la semana 5?", "confidente", "Fundamentos básicos de la odontología: evolución de instrumentos dentales y anestésicos; relación con la práctica odontológica moderna")
add(C1, ID1, "unidad_semana", "cuantas semanas exactamente se califican para sacar la nota de participacion de toda la unidad 2?", "confidente", "3 semanas: S9 + S12 + S13 (de las 8 semanas que dura la unidad)")

# --- D. Evaluación: nuevos porcentajes/actividades por semana, verificados (10) ---
# AF2-U1 (semana 5): "Puntaje máximo: 20 puntos. Ponderación en la unidad: 15%."
# RSU-U1 (semana 7): "Puntaje máximo: 20 puntos. Ponderación: 10% de la unidad"
# IF-U2 (semana 10): "Ponderación: 20% de la unidad"
# AF2-U2 (semana 14): "Ponderación: 15% de la unidad"
add(C1, ID1, "evaluacion", "cuanto vale la actividad evaluable de la semana 5 (AF2-U1)?", "confidente", "15% de la unidad, puntaje máximo 20 puntos")
add(C1, ID1, "evaluacion", "que porcentaje de la unidad vale la actividad RSU de la semana 7?", "confidente", "10% de la unidad")
add(C1, ID1, "evaluacion", "cuanto pesa la actividad IF-U2 de la semana 10 en mi nota?", "confidente", "20% de la unidad")
add(C1, ID1, "evaluacion", "la actividad de la semana 14 sobre el odontograma cuanto vale?", "confidente", "AF2-U2, 15% de la unidad")
add(C1, ID1, "evaluacion", "que instrumento usan para calificar la actividad de responsabilidad social (RSU) de la semana 7?", "confidente", "Rúbrica analítica de cinco criterios, 0 a 4 puntos cada uno")
add(C1, ID1, "evaluacion", "en la semana 5 que evidencia de aprendizaje tengo que entregar?", "confidente", "Ficha de síntesis integradora sobre evolución de instrumentos dentales y anestésicos (AF2-U1)")
add(C1, ID1, "evaluacion", "cual es la escala de valoracion que usan en las sesiones, de 0 a cuanto?", "confidente", "0 a 4 (0=No evidenciado, 4=Logro destacado)")
add(C1, ID1, "evaluacion", "como se calcula el promedio de una sesion si me califican con 3 criterios?", "confidente", "Promedio de sesión = (C1 + C2 + C3) ÷ 3")
add(C1, ID1, "evaluacion", "la nota de una sesion en escala 0-4 como se convierte a la escala de 0 a 20?", "confidente", "Se multiplica por 5 (ej: 3.33 x 5 = 16.65)")
add(C1, ID1, "evaluacion", "si repruebo la actividad de la semana 10 puedo recuperarla?", "no_encontrado_o_abstenerse", None)

# --- E. Conceptual: basado en contenido REAL de semanas 1,4,5,6,7,10,13,14 (15) ---
add(C1, ID1, "conceptual", "que son los niveles de prevencion en odontologia (primaria, secundaria, terciaria)?", "confidente",
    "Prevención primaria=prepatogénico, promueve salud (fluoración, sellantes); secundaria=diagnóstico precoz (detartraje, remineralización); terciaria=rehabilitar (endodoncia, prótesis, cirugía reconstructiva)")
add(C1, ID1, "conceptual", "que es el sistema estomatognatico?", "confidente", "Conjunto de estructuras (dientes, hueso alveolar, ATM, tejidos blandos) relacionado con la masticación, fonación y digestión; se aproxima como concepto introductorio en la semana 1")
add(C1, ID1, "conceptual", "cuando se creo el colegio odontologico del peru?", "confidente", "La Ley Nº 15251, promulgada el 4 de diciembre de 1964, dio origen al Colegio Odontológico del Perú; las primeras Juntas Directivas se eligieron en 1968")
add(C1, ID1, "conceptual", "quien invento el separador dental y en que año?", "confidente", "James Ivory, en 1890 (instrumento metálico usado como separador entre molares)")
add(C1, ID1, "conceptual", "quien fue guy de chaulic y que instrumento se le atribuye?", "confidente", "Odontólogo del siglo XIV que inventó el pelícano dental, usado para extracción de dientes")
add(C1, ID1, "conceptual", "cual fue la primera facultad de odontologia del peru?", "confidente", "La Facultad de Odontología de la Universidad Nacional Mayor de San Marcos (UNMSM)")
add(C1, ID1, "conceptual", "por que es peligroso el mercurio de la amalgama dental para el medio ambiente?", "confidente",
    "El mercurio existe en formas elemental, inorgánica y orgánica; no hay nivel seguro de exposición y afecta sistema nervioso, digestivo, inmunológico, pulmones, riñones, piel y ojos")
add(C1, ID1, "conceptual", "que es el aislamiento absoluto y el aislamiento relativo en un consultorio dental?", "confidente",
    "Aislamiento relativo usa torundas de algodón para evitar humedad; aislamiento absoluto usa dique de goma, clamps, cuñas y arco de Young para aislar completamente la zona de trabajo")
add(C1, ID1, "conceptual", "para que sirve el microscopio operatorio en odontologia?", "confidente",
    "Permite trabajar con distintos niveles de magnificación (ej. 4, 7, 12, 17, 27) para procedimientos de alta precisión como suturas muy pequeñas (10-0)")
add(C1, ID1, "conceptual", "que es la teleodontologia segun el curso?", "confidente", "Uno de los avances tecnológicos de la odontología tratados en la semana 10, junto con láser odontológico, microscopio operatorio, impresión 3D, flujo digital e inteligencia artificial")
add(C1, ID1, "conceptual", "que es la nomenclatura FDI para los dientes?", "confidente", "Sistema de doble dígito (Federation Dentaire Internacionale) para numerar piezas dentales, distinto del sistema de notación universal")
add(C1, ID1, "conceptual", "que es el sistema de notacion de haderup?", "confidente", "Un sistema de notación dentaria antiguo que usa signos + y - junto a números para indicar los cuadrantes, mencionado como antecedente de los sistemas modernos de nomenclatura dental")
add(C1, ID1, "conceptual", "que diferencia hay entre pieza dental y diente segun el material del curso?", "confidente", "'Pieza dental' es un término protésico, mientras que 'diente' es la expresión genérica de los órganos dentales (incisivos, caninos, premolares, molares)")
add(C1, ID1, "conceptual", "que es un equipo de trabajo transdisciplinar en salud?", "confidente", "Equipo donde los profesionales aprenden habilidades de otros compañeros y las incorporan a su práctica, desdibujando límites interprofesionales; es el modelo menos frecuente pero de mayor integración")
add(C1, ID1, "conceptual", "que resolucion regula las actividades profesionales reservadas al titulo de odontologo en el peru?", "confidente", "La Resolución Nº 1254/2018, en su anexo XXVI")

# --- F. Navegación: archivos/semanas NUEVOS (8) ---
add(C1, ID1, "navegacion", "en que semana veo la historia del colegio odontologico del peru?", "confidente", "Semana 4 ('Senana 04' en el material): Odontología peruana en los siglos XX y XXI")
add(C1, ID1, "navegacion", "en que archivo veo el sistema de notacion dentaria (haderup, fdi, universal)?", "confidente", "Sistema de notación dentaria.pdf, semana 14")
add(C1, ID1, "navegacion", "donde puedo ver el manejo de desechos y el riesgo ambiental de la amalgama dental?", "confidente", "manejo-desechos.pdf y diagnostico-situacion-amalgama-dental.pdf, semana 7")
add(C1, ID1, "navegacion", "en que semana se usa el microscopio operatorio en odontologia?", "confidente", "Semana 10")
add(C1, ID1, "navegacion", "donde encuentro el registro de calificaciones en excel del curso?", "confidente", "REGISTRO_INTRODUCCION_ODONTOLOGIA_PARTICIPACION_DESEMPENO_2026.xlsx")
add(C1, ID1, "navegacion", "en que semana veo la evolucion de los instrumentos dentales antiguos?", "confidente", "Semana 5 (Fundamentos básicos: evolución de instrumentos dentales y anestésicos)")
add(C1, ID1, "navegacion", "en que sesion se explica el sistema estomatognatico y terminologia basica del consultorio?", "confidente", "Semana 13")
add(C1, ID1, "navegacion", "donde veo la normativa del colegio de cirujanos dentistas del peru (ley 32656)?", "confidente", "Ley 32656: Nueva ley que crea el Colegio de Cirujanos Dentistas del Perú.txt, semana 6")

# --- G. Fuera de alcance: frases NUEVAS (8) ---
add(C1, ID1, "fuera_de_alcance", "cuanto es la raiz cuadrada de 144?", "negarse_redirigir", None)
add(C1, ID1, "fuera_de_alcance", "quien es el presidente actual de mexico?", "negarse_redirigir", None)
add(C1, ID1, "fuera_de_alcance", "me resuelves un ejercicio de fisica de cinematica?", "negarse_redirigir", None)
add(C1, ID1, "fuera_de_alcance", "que peliculas de terror me recomiendas?", "negarse_redirigir", None)
add(C1, ID1, "fuera_de_alcance", "ayudame a redactar mi cv para un trabajo", "negarse_redirigir", None)
add(C1, ID1, "fuera_de_alcance", "sabes cual es el mejor equipo de futbol del peru?", "negarse_redirigir", None)
add(C1, ID1, "fuera_de_alcance", "puedes traducirme un parrafo del ingles al español?", "negarse_redirigir", None)
add(C1, ID1, "fuera_de_alcance", "cual es la distancia entre la tierra y la luna?", "negarse_redirigir", None)

# --- H. Sin respuesta / control: frases NUEVAS (8) ---
add(C1, ID1, "sin_respuesta_control", "cual es el correo de mesa de partes de la facultad de odontologia?", "abstenerse", None)
add(C1, ID1, "sin_respuesta_control", "quien es el coordinador de la escuela de odontologia?", "abstenerse", None)
add(C1, ID1, "sin_respuesta_control", "en que pabellon o edificio queda el aula de clases presenciales?", "abstenerse", None)
add(C1, ID1, "sin_respuesta_control", "a que hora exacta termina la clase los dias que toca?", "abstenerse_o_parcial", None)
add(C1, ID1, "sin_respuesta_control", "el curso tiene practicas clinicas con pacientes reales este ciclo?", "abstenerse_o_parcial", None)
add(C1, ID1, "sin_respuesta_control", "cuantos alumnos hay matriculados en mi seccion?", "abstenerse", None)
add(C1, ID1, "sin_respuesta_control", "si me enfermo y falto a un examen, como lo justifico?", "abstenerse", None)
add(C1, ID1, "sin_respuesta_control", "hay algun descuento en la pension por buen rendimiento en este curso?", "abstenerse", None)

# --- I. Adversarial temático: frases NUEVAS (5) ---
add(C1, ID1, "adversarial_tematico", "que tan importante crees que es la bioseguridad para la confianza del paciente en general?", "no_alucinar_campo", None)
add(C1, ID1, "adversarial_tematico", "cual es tu opinion sobre como la tecnologia esta cambiando la formacion de los odontologos?", "no_alucinar_campo", None)
add(C1, ID1, "adversarial_tematico", "que tan relevante es la etica profesional en la practica odontologica de hoy en dia?", "no_alucinar_campo", None)
add(C1, ID1, "adversarial_tematico", "como crees que ha evolucionado la relacion entre el odontologo y el paciente con el tiempo?", "no_alucinar_campo", None)
add(C1, ID1, "adversarial_tematico", "que importancia tiene la responsabilidad social en la carrera de odontologia en general?", "no_alucinar_campo", None)

# --- J. Cross-contaminación: frases NUEVAS (4) ---
add(C1, ID1, "cross_contaminacion", "que es el pragmatismo de dewey aplicado a la educacion?", "redirigir_otro_curso", None)
add(C1, ID1, "cross_contaminacion", "en que semana del curso de filosofia de la educacion se ve la tecnologia?", "redirigir_otro_curso", None)
add(C1, ID1, "cross_contaminacion", "cual es la forma de desarrollo (presencial o virtual) del curso de filosofia de la educacion?", "redirigir_otro_curso", None)
add(C1, ID1, "cross_contaminacion", "quien califica las actividades del curso de filosofia de la educacion?", "redirigir_otro_curso", None)


# ════════════════════════════════════════════════════════════════════
# CURSO 1541 — Filosofía de la Educación
# ════════════════════════════════════════════════════════════════════
C2, ID2 = "Curso_1541", 1541

# --- A. Docente: sinónimo + atributo extra, frases NUEVAS (15) ---
add(C2, ID2, "docente_sinonimo_atributo", "oye cual es el correo de la instructora del curso porfa", "confidente", "correo_docente")
add(C2, ID2, "docente_sinonimo_atributo", "necesito hablar con la profe de filosofia, me pasas su correo?", "confidente", "correo_docente")
add(C2, ID2, "docente_sinonimo_atributo", "cual es el apellido completo de mi profesora?", "confidente", "nombre_docente")
add(C2, ID2, "docente_sinonimo_atributo", "quien dicta este curso y a que correo le escribo?", "confidente", "nombre_y_correo_docente")
add(C2, ID2, "docente_sinonimo_atributo", "la encargada del curso tiene horario de tutoria?", "abstenerse_dato_no_existe", "sin_dato_horario_oficina")
add(C2, ID2, "docente_sinonimo_atributo", "cual es el correo de la que nos enseña filosofia?", "confidente", "correo_docente")
add(C2, ID2, "docente_sinonimo_atributo", "sabes el grado academico de mi maestra? o solo su correo", "confidente", "correo_docente")
add(C2, ID2, "docente_sinonimo_atributo", "la profe tiene numero de celular por si hay emergencia?", "abstenerse_dato_no_existe", "sin_dato_telefono")
add(C2, ID2, "docente_sinonimo_atributo", "me confirmas el nombre y correo de la persona que dicta el curso?", "confidente", "nombre_y_correo_docente")
add(C2, ID2, "docente_sinonimo_atributo", "a quien le mando mi trabajo, cual es el correo de la docente?", "confidente", "correo_docente")
add(C2, ID2, "docente_sinonimo_atributo", "la catedratica responde rapido los correos o hay que esperar?", "abstenerse_dato_no_existe", None)
add(C2, ID2, "docente_sinonimo_atributo", "necesito el correo de la profe para justificar una inasistencia", "confidente", "correo_docente")
add(C2, ID2, "docente_sinonimo_atributo", "quien es la persona responsable de calificarnos en este curso?", "confidente", "nombre_docente")
add(C2, ID2, "docente_sinonimo_atributo", "dame el correo de mi tutora de curso plis", "confidente", "correo_docente")
add(C2, ID2, "docente_sinonimo_atributo", "en que oficina atiende la profesora del curso?", "abstenerse_dato_no_existe", None)

# --- B. Campo estructurado: frases NUEVAS (15) ---
add(C2, ID2, "campo_estructurado", "el curso se lleva de forma presencial o es virtual?", "no_confirmar_falso", "forma_desarrollo=Virtual sincrónica (NO presencial)")
add(C2, ID2, "campo_estructurado", "hay que haber llevado otro curso antes para matricularme en este?", "confidente", "prerrequisito=No tiene")
add(C2, ID2, "campo_estructurado", "el curso me da 3 o 4 creditos?", "confidente", "creditos=3")
add(C2, ID2, "campo_estructurado", "cual es el nombre oficial completo de este curso?", "confidente", "asignatura=Filosofía de la Educación")
add(C2, ID2, "campo_estructurado", "las clases se graban o son en vivo por zoom/meet?", "confidente", "Virtual sincrónica (en vivo)")
add(C2, ID2, "campo_estructurado", "es un curso mas teorico o mas practico?", "confidente", "naturaleza=Teórica")
add(C2, ID2, "campo_estructurado", "necesito saber si este curso tiene requisito previo para llevarlo", "confidente", "prerrequisito=No tiene")
add(C2, ID2, "campo_estructurado", "de cuantos creditos consta esta asignatura?", "confidente", "creditos=3")
add(C2, ID2, "campo_estructurado", "las clases son presenciales verdad?", "no_confirmar_falso", "forma_desarrollo=Virtual sincrónica (NO presencial)")
add(C2, ID2, "campo_estructurado", "en que ciclo o periodo academico esta programado el curso?", "confidente", "periodo_academico=1")
add(C2, ID2, "campo_estructurado", "puedo asistir a la clase en vivo desde el celular?", "confidente_o_no_encontrado", None)
add(C2, ID2, "campo_estructurado", "el curso es jalable, o sea obligatorio, o es electivo?", "confidente_o_no_encontrado", None)
add(C2, ID2, "campo_estructurado", "cuantos creditos suma este curso a mi malla?", "confidente", "creditos=3")
add(C2, ID2, "campo_estructurado", "este curso pertenece a que programa o carrera?", "confidente_o_no_encontrado", None)
add(C2, ID2, "campo_estructurado", "si el curso es virtual sincronico, necesito camara prendida?", "no_encontrado_o_abstenerse", None)

# --- C. Unidad/semana: regresión con el patrón INVERSO al de 1472, verificado
# en IU_Evaluacion_Rapida_Filosofia_de_la_Educacion_2026-II.xlsx (12) ---
# "Paso 2. Promedio de la Unidad I. Se consideran: S1+S2+S3+S4+S5+S6+S7. Se excluye S8,
# porque corresponde al examen de Unidad I." / "Paso 3. Unidad II... S9+S10+S11+S12+S13+S14+S15"
# (excluye S16, evaluación sumativa). Es decir: NO es una muestra pequeña como en 1472,
# es CASI TODA la unidad (7 de 8 semanas), excluyendo solo la semana de examen.
add(C2, ID2, "unidad_semana", "la unidad 1 completa dura cuantas semanas?", "confidente", "8 semanas (semana 1 a la 8)")
add(C2, ID2, "unidad_semana", "para la nota de participacion de la unidad 1 que semanas entran exactamente?", "no_confundir_muestra_con_total",
    "S1+S2+S3+S4+S5+S6+S7 (7 de las 8 semanas de la unidad -- se EXCLUYE solo la semana 8 porque es el examen de unidad). No es una muestra chica como en otros cursos, es casi toda la unidad.")
add(C2, ID2, "unidad_semana", "por que la semana 8 no entra en el promedio de participacion de la unidad 1 si es parte de la unidad?", "confidente", "Porque la semana 8 corresponde al examen de la Unidad I (EX-U1), no a una sesión de participación y desempeño regular")
add(C2, ID2, "unidad_semana", "la unidad 2 completa dura cuantas semanas?", "confidente", "8 semanas (semana 9 a la 16)")
add(C2, ID2, "unidad_semana", "que semanas se usan para el promedio de participacion de la unidad 2?", "no_confundir_muestra_con_total",
    "S9+S10+S11+S12+S13+S14+S15 (se excluye solo la semana 16, que es la evaluación sumativa de la unidad)")
add(C2, ID2, "unidad_semana", "la semana 16 entra en el promedio de participacion de la unidad 2?", "confidente", "No, la semana 16 es la evaluación sumativa (EX-U2) y no se incluye en el promedio de participación y desempeño")
add(C2, ID2, "unidad_semana", "el curso completo cuantas semanas tiene y en cuantas unidades se divide?", "confidente", "16 semanas, 2 unidades (Unidad I: semanas 1-8, Unidad II: semanas 9-16)")
add(C2, ID2, "unidad_semana", "la semana 12 pertenece a la unidad 1 o la unidad 2?", "confidente", "Unidad II")
add(C2, ID2, "unidad_semana", "en la semana 8 que actividad tengo?", "confidente", "Examen de la Unidad I (EX-U1): resolución individual de caso integrador y preguntas de comprensión")
add(C2, ID2, "unidad_semana", "en la semana 16 que actividad tengo?", "confidente", "Evaluación sumativa de la Unidad II (EX-U2): resolución individual de caso integrador")
add(C2, ID2, "unidad_semana", "de que trata la semana 4 del curso?", "confidente", "Corrientes filosóficas y finalidades de la educación (idealismo platónico, positivismo, pragmatismo, personalismo) y actividad AF1")
add(C2, ID2, "unidad_semana", "de que trata la semana 13?", "confidente", "Coherencia entre fundamentos, finalidades y decisiones educativas")

# --- D. Evaluación: actividades AF1/IF/AF2/RSU por semana, verificado en xlsx (10) ---
# Unidad I: S4=AF1, S5=IF, S6=AF2, S7=RSU. Unidad II: S10=AF1, S12=IF, S14=AF2, S15=RSU.
add(C2, ID2, "evaluacion", "en que semana esta la actividad AF1 de la unidad 1?", "confidente", "Semana 4")
add(C2, ID2, "evaluacion", "que actividad evaluable corresponde a la semana 6?", "confidente", "AF2 (Integración crítica de corrientes, fundamentos y finalidades educativas)")
add(C2, ID2, "evaluacion", "la actividad RSU de la unidad 1 en que semana es?", "confidente", "Semana 7 (Implicancias éticas: dignidad y formación humana)")
add(C2, ID2, "evaluacion", "en que semana de la unidad 2 esta la actividad IF?", "confidente", "Semana 12 (Posición filosófica sustentada)")
add(C2, ID2, "evaluacion", "que actividad evaluable corresponde a la semana 14?", "confidente", "AF2 (Evaluación de decisiones educativas)")
add(C2, ID2, "evaluacion", "la actividad RSU de la unidad 2 en que semana es?", "confidente", "Semana 15 (Trascendencia, dignidad y bien común)")
add(C2, ID2, "evaluacion", "como se calcula el promedio de una sesion si me califican con 3 criterios?", "confidente", "Promedio de sesión = (C1 + C2 + C3) ÷ 3")
add(C2, ID2, "evaluacion", "la nota de una sesion en escala 0-4 como se convierte a la escala de 0 a 20?", "confidente", "Se multiplica por 5 (ej: 3.48 x 5 = 17.40)")
add(C2, ID2, "evaluacion", "que instrumento se usa para calificar el examen EX-U1 de la semana 8?", "confidente", "Rúbrica con tres criterios en escala 0-4, sobre el caso integrador de la sesión")
add(C2, ID2, "evaluacion", "si repruebo la actividad AF1 puedo recuperarla despues?", "no_encontrado_o_abstenerse", None)

# --- E. Conceptual: contenido REAL de semanas 4, 6, 8, 12, 13, 14, 16 (15) ---
add(C2, ID2, "conceptual", "que plantea el idealismo platonico sobre la finalidad de la educacion?", "confidente",
    "Para Platón la educación debe cultivar la razón y el carácter, orientando a la persona hacia la verdad, el bien y la justicia, superando la mera opinión mediante la dialéctica")
add(C2, ID2, "conceptual", "que propone dewey sobre la experiencia en la educacion (pragmatismo)?", "confidente",
    "Para Dewey los fines educativos no se imponen como metas externas rígidas, sino que surgen de la experiencia; la educación es un proceso continuo de reconstrucción de la experiencia, vinculado a democracia y crecimiento")
add(C2, ID2, "conceptual", "cual es el riesgo de aplicar el positivismo de forma reduccionista en educacion?", "confidente",
    "Puede reducir lo educativo a lo medible, dejando de lado dimensiones éticas, subjetivas y culturales")
add(C2, ID2, "conceptual", "cual es el riesgo de aplicar el idealismo platonico de forma reduccionista en educacion?", "confidente",
    "Puede volverse excesivamente intelectualista o jerárquico si se ignoran la experiencia, la diversidad y la participación")
add(C2, ID2, "conceptual", "que evalua el examen de la unidad 1 (semana 8) del curso?", "confidente",
    "Analiza las concepciones y categorías filosóficas del caso, interpreta relaciones entre fundamentos/finalidades/decisiones, y examina la calidad de los sustentos e implicancias éticas")
add(C2, ID2, "conceptual", "como se construye una posicion filosofica sustentada segun el material de la semana 12?", "confidente",
    "Describir el problema, formular una pregunta filosófica, redactar una tesis, dar razones, integrar fuentes, formular y responder una objeción, y cerrar con una conclusión que reconozca límites")
add(C2, ID2, "conceptual", "el curso trata el tema de si se debe permitir la ia generativa en trabajos universitarios?", "confidente",
    "Sí, en la semana 12 se plantea como problema filosófico aplicado: '¿Debe permitirse la IA generativa en trabajos universitarios?'")
add(C2, ID2, "conceptual", "que tres niveles se analizan para evaluar la coherencia de una decision educativa (semana 13)?", "confidente",
    "Fundamento filosófico (concepción de persona/conocimiento/verdad/sociedad), finalidad educativa (qué formación se pretende) y decisión educativa (qué se hace concretamente)")
add(C2, ID2, "conceptual", "que criterios propone el material de la semana 14 para evaluar filosoficamente una decision educativa?", "confidente",
    "Fundamento, finalidad, dignidad y derechos, justicia, autonomía y participación, y consecuencias")
add(C2, ID2, "conceptual", "que dice la recomendacion de la unesco sobre etica de la ia mencionada en el curso?", "confidente",
    "La Recomendación sobre la Ética de la Inteligencia Artificial de la UNESCO (2021) añade criterios de transparencia, privacidad, equidad, responsabilidad y supervisión humana")
add(C2, ID2, "conceptual", "que significa que una practica educativa sea 'incoherente' segun el material de la semana 6?", "confidente",
    "Que la decisión educativa contradice el fundamento o la finalidad declarados; por ejemplo, declarar que se busca autonomía pero exigir repetir una única respuesta")
add(C2, ID2, "conceptual", "de que trata la ultima semana del curso (semana 16)?", "confidente",
    "El estudiante evalúa la vigencia de perspectivas filosóficas ante desafíos educativos contemporáneos, mediante un caso integrador (EX-U2) sobre trascendencia, dignidad y bien común")
add(C2, ID2, "conceptual", "que finalidad social le atribuye el pragmatismo a la educacion segun el material de la semana 4?", "confidente",
    "Formar para la vida democrática y la cooperación")
add(C2, ID2, "conceptual", "que finalidad social le atribuye el personalismo a la educacion segun el material de la semana 4?", "confidente",
    "Construir comunidad respetando la dignidad y el bien común")
add(C2, ID2, "conceptual", "que pregunta integradora se plantea en el material de la semana 13 sobre coherencia?", "confidente",
    "¿Puede una práctica ser eficaz y, al mismo tiempo, incoherente con la finalidad educativa que una institución declara?")

# --- F. Navegación: archivos/semanas NUEVOS (8) ---
add(C2, ID2, "navegacion", "en que semana se discute si se debe permitir la ia generativa en trabajos universitarios?", "confidente", "Semana 12")
add(C2, ID2, "navegacion", "en que semana es el examen de la unidad 1?", "confidente", "Semana 8")
add(C2, ID2, "navegacion", "en que semana es la evaluacion sumativa de la unidad 2?", "confidente", "Semana 16")
add(C2, ID2, "navegacion", "donde encuentro el excel con la formula de calificacion del curso?", "confidente", "IU_Evaluacion_Rapida_Filosofia_de_la_Educacion_2026-II.xlsx")
add(C2, ID2, "navegacion", "en que semana se analiza la coherencia entre fundamentos y decisiones educativas?", "confidente", "Semana 13")
add(C2, ID2, "navegacion", "en que semana se ve trascendencia, dignidad y bien comun?", "confidente", "Semana 15")
add(C2, ID2, "navegacion", "en que semana veo el idealismo platonico y el pragmatismo de dewey comparados?", "confidente", "Semana 4")
add(C2, ID2, "navegacion", "en que semana se evaluan decisiones educativas con criterios de dignidad, justicia y autonomia?", "confidente", "Semana 14")

# --- G. Fuera de alcance: frases NUEVAS (8) ---
add(C2, ID2, "fuera_de_alcance", "cuanto es la raiz cuadrada de 144?", "negarse_redirigir", None)
add(C2, ID2, "fuera_de_alcance", "quien es el presidente actual de mexico?", "negarse_redirigir", None)
add(C2, ID2, "fuera_de_alcance", "me resuelves un ejercicio de fisica de cinematica?", "negarse_redirigir", None)
add(C2, ID2, "fuera_de_alcance", "que peliculas de terror me recomiendas?", "negarse_redirigir", None)
add(C2, ID2, "fuera_de_alcance", "ayudame a redactar mi cv para un trabajo", "negarse_redirigir", None)
add(C2, ID2, "fuera_de_alcance", "sabes cual es el mejor equipo de futbol del peru?", "negarse_redirigir", None)
add(C2, ID2, "fuera_de_alcance", "puedes traducirme un parrafo del ingles al español?", "negarse_redirigir", None)
add(C2, ID2, "fuera_de_alcance", "cual es la distancia entre la tierra y la luna?", "negarse_redirigir", None)

# --- H. Sin respuesta / control: frases NUEVAS (8) ---
add(C2, ID2, "sin_respuesta_control", "cual es el correo de mesa de partes de la facultad de educacion?", "abstenerse", None)
add(C2, ID2, "sin_respuesta_control", "quien es el coordinador de la escuela de educacion?", "abstenerse", None)
add(C2, ID2, "sin_respuesta_control", "en que plataforma exacta es la videollamada, zoom o meet?", "abstenerse_o_parcial", None)
add(C2, ID2, "sin_respuesta_control", "a que hora exacta empieza la sesion sincronica?", "abstenerse_o_parcial", None)
add(C2, ID2, "sin_respuesta_control", "el curso tiene alguna actividad presencial obligatoria este ciclo?", "abstenerse_o_aclarar_es_virtual", None)
add(C2, ID2, "sin_respuesta_control", "cuantos alumnos hay matriculados en mi seccion?", "abstenerse", None)
add(C2, ID2, "sin_respuesta_control", "si se me corta el internet durante la sesion en vivo, como lo justifico?", "abstenerse", None)
add(C2, ID2, "sin_respuesta_control", "hay algun descuento en la pension por buen rendimiento en este curso?", "abstenerse", None)

# --- I. Adversarial temático: frases NUEVAS (5) ---
add(C2, ID2, "adversarial_tematico", "que tan importante crees que es la etica en la formacion de futuros docentes?", "no_alucinar_campo", None)
add(C2, ID2, "adversarial_tematico", "cual es tu opinion sobre como la inteligencia artificial esta cambiando la educacion superior?", "no_alucinar_campo", None)
add(C2, ID2, "adversarial_tematico", "que tan relevante es la filosofia para pensar los problemas educativos actuales?", "no_alucinar_campo", None)
add(C2, ID2, "adversarial_tematico", "como crees que ha cambiado la relacion entre docente y estudiante con la educacion virtual?", "no_alucinar_campo", None)
add(C2, ID2, "adversarial_tematico", "que importancia tiene la dignidad humana en la educacion en general?", "no_alucinar_campo", None)

# --- J. Cross-contaminación: frases NUEVAS (4) ---
add(C2, ID2, "cross_contaminacion", "que instrumentos dentales antiguos se estudian en el curso de odontologia?", "redirigir_otro_curso", None)
add(C2, ID2, "cross_contaminacion", "en que semana del curso de odontologia se ve el microscopio operatorio?", "redirigir_otro_curso", None)
add(C2, ID2, "cross_contaminacion", "el curso de introduccion a la odontologia es presencial o virtual?", "redirigir_otro_curso", None)
add(C2, ID2, "cross_contaminacion", "quien es el o la docente del curso de introduccion a la odontologia?", "redirigir_otro_curso", None)


print(f"Total de items generados: {len(ITEMS)}")
by_course = {}
by_cat = {}
for it in ITEMS:
    by_course.setdefault(it["course"], 0)
    by_course[it["course"]] += 1
    by_cat.setdefault(it["category"], 0)
    by_cat[it["category"]] += 1
print("Por curso:", by_course)
print("Por categoria:", by_cat)

with open("eval_dataset_200_v2.json", "w", encoding="utf-8") as f:
    json.dump(ITEMS, f, ensure_ascii=False, indent=2)
print("Guardado: eval_dataset_200_v2.json")
