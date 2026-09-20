# -*- coding: utf-8 -*-
"""
build_report_v2.py
====================
Genera "Data prueba/Idania - 200 Pruebas v2 (Ejemplos Nuevos, Post-Correcciones).docx"
a partir de evaluation_200_v2_summary.json (batch 2, ejemplos nuevos) y
evaluation_200_hallucinations_summary.json (batch 1, ejemplos originales que
encontraron los 3 bugs), en el mismo estilo que
"Idania - 200 Pruebas de Alucinaciones (Espejo de Produccion).docx".
"""
import json
import docx
from docx.shared import Pt
from docx.enum.text import WD_ALIGN_PARAGRAPH

with open("evaluation_200_v2_summary.json", encoding="utf-8") as f:
    S2 = json.load(f)
with open("evaluation_200_hallucinations_summary.json", encoding="utf-8") as f:
    S1 = json.load(f)
with open("evaluation_200_v2_results.json", encoding="utf-8") as f:
    R2 = json.load(f)

CAT_LABELS = {
    "docente_sinonimo_atributo": "Docente + sinónimo + atributo extra",
    "campo_estructurado": "Campos estructurados básicos",
    "unidad_semana": "Unidad / semana (muestra vs. total)",
    "evaluacion": "Evaluación / rúbricas / ponderación",
    "conceptual": "Conceptual (contenido real)",
    "navegacion": "Navegación (semana/archivo)",
    "fuera_de_alcance": "Fuera de alcance",
    "sin_respuesta_control": "Sin respuesta / control",
    "adversarial_tematico": "Adversarial temático",
    "cross_contaminacion": "Contaminación cruzada entre cursos",
}

d = docx.Document()

# ---- Title ----
h = d.add_heading("200 pruebas v2 — ejemplos nuevos, post-correcciones", level=0)

p = d.add_paragraph(
    "Segunda tanda de evaluación de alucinaciones, con 200 preguntas NUEVAS "
    "(otras semanas, otros archivos, otros temas a los del primer batch), corrida "
    "después de aplicar los 3 fixes genéricos descritos en \"Idania - Corrección de "
    "3 Alucinaciones (Cambios Genéricos).docx\". Objetivo: confirmar si esos fixes "
    "generalizan a ejemplos distintos a los que los motivaron, o si fueron un "
    "parche sobreajustado a las frases exactas del primer batch. Evaluación hecha "
    "contra el mismo espejo local de producción (Curso_1472 / Curso_1541), sin "
    "tocar producción."
)

# ---- Resumen ejecutivo ----
d.add_heading("Resumen ejecutivo", level=1)
d.add_paragraph(
    f"Con ejemplos nuevos, el resultado es PEOR que en el primer batch: "
    f"{S2['pass_rate']*100:.1f}% de aprobación (vs. {S1['pass_rate']*100:.1f}% en el batch 1) y "
    f"{S2['hallucination_rate']*100:.1f}% de alucinaciones reales (vs. {S1['hallucination_rate']*100:.1f}% en el batch 1). "
    "Los 3 fixes SÍ reducen la frecuencia de los patrones originales, pero ninguno los "
    "elimina por completo: los tres bugs reproducen con frases o campos distintos a los "
    "exactos que los motivaron, y el fix de \"muestra vs. total\" incluso generó un patrón "
    "de confusión nuevo y más severo en Curso_1541 (mezclar las semanas de las dos unidades "
    "en una sola cifra). Además aparecieron patrones de falla nuevos, no relacionados a los "
    "3 fixes: una fila de plantilla (\"Estudiante 1...Estudiante 35\") de un xlsx leída como "
    "dato real de matrícula, y varias confusiones de navegación por semanas adyacentes."
)
d.add_paragraph(
    "Lo que sigue sólido: fuera de alcance (100%) y adversarial temático (100%) se sostienen "
    "igual que en el batch 1. Conceptual sobre contenido real sigue siendo la categoría más "
    "confiable del RAG normal (86.7%, solo 2 alucinaciones de 30)."
)

# ---- Comparación batch 1 vs batch 2 ----
d.add_heading("Comparación batch 1 (ejemplos originales) vs. batch 2 (ejemplos nuevos)", level=1)
t = d.add_table(rows=1, cols=4)
t.style = "Light Grid Accent 1"
hdr = t.rows[0].cells
hdr[0].text, hdr[1].text, hdr[2].text, hdr[3].text = "Métrica", "Batch 1 (200, original)", "Batch 2 (200, nuevo)", "Cambio"

def add_row(label, v1, v2, fmt="{:.1%}", higher_is_better=True, delta_fmt="pct", delta_suffix=""):
    row = t.add_row().cells
    row[0].text = label
    s1 = fmt.format(v1) if v1 is not None else "n/d"
    s2 = fmt.format(v2) if v2 is not None else "n/d"
    row[1].text = s1
    row[2].text = s2
    if v1 is not None and v2 is not None:
        delta = v2 - v1
        better = (delta > 0) == higher_is_better
        arrow = "peor" if not better and abs(delta) > 1e-9 else ("mejor" if abs(delta) > 1e-9 else "=")
        if abs(delta) < 1e-9:
            row[3].text = "="
        elif delta_fmt == "pct":
            row[3].text = f"{delta:+.1f} pts ({arrow})"
        elif delta_fmt == "count":
            row[3].text = f"{delta:+.0f}{delta_suffix} ({arrow})"
        else:
            row[3].text = f"{delta:+.2f} ({arrow})"
    else:
        row[3].text = "n/d"

add_row("Pass rate", S1["pass_rate"]*100, S2["pass_rate"]*100, fmt="{:.1f}%")
add_row("Hallucination rate", S1["hallucination_rate"]*100, S2["hallucination_rate"]*100, fmt="{:.1f}%", higher_is_better=False)
add_row("Fail rate", S1["fail_rate"]*100, S2["fail_rate"]*100, fmt="{:.1f}%", higher_is_better=False)
add_row("Partial rate", S1["partial_rate"]*100, S2["partial_rate"]*100, fmt="{:.1f}%", higher_is_better=False)
add_row("Confundió muestra con total (n, de 200)", S1["confused_sample_with_total_count"], S2["confused_sample_with_total_count"], fmt="{:.0f}", higher_is_better=False, delta_fmt="count")
add_row("Contaminación cruzada real (n, de 200)", S1["cross_course_contamination_count"], S2["cross_course_contamination_count"], fmt="{:.0f}", higher_is_better=False, delta_fmt="count")
add_row("Groundedness promedio (0-10)", S1["avg_groundedness"], S2["avg_groundedness"], fmt="{:.2f}", delta_fmt="raw")

d.add_paragraph(
    "Nota: una parte de este deterioro es esperable y metodológicamente sano — el batch 2 "
    "fue diseñado deliberadamente para cubrir semanas, archivos y frases MÁS variadas y menos "
    "favorables (ver Metodología), no para repetir los mismos casos ya arreglados. Aun así, la "
    "magnitud de la caída (-7.5 pts de pass rate, +5 pts de hallucination rate) indica que la "
    "cobertura real de los 3 fixes es más angosta de lo que el batch 1 por sí solo sugería."
)

# ---- Resultado por categoría ----
d.add_heading("Resultado por categoría (batch 2)", level=1)
t2 = d.add_table(rows=1, cols=5)
t2.style = "Light Grid Accent 1"
hdr = t2.rows[0].cells
hdr[0].text, hdr[1].text, hdr[2].text, hdr[3].text, hdr[4].text = "Categoría", "Preguntas", "Aprobación batch 2", "Alucinaciones batch 2", "Aprobación batch 1"
for cat, label in CAT_LABELS.items():
    c2 = S2["by_category"].get(cat, {})
    c1 = S1["by_category"].get(cat, {})
    row = t2.add_row().cells
    row[0].text = label
    row[1].text = str(c2.get("n", "n/d"))
    row[2].text = f"{c2.get('pass_rate', 0)*100:.1f}%" if c2.get("pass_rate") is not None else "n/d"
    row[3].text = str(c2.get("hallucinations", "n/d"))
    row[4].text = f"{c1.get('pass_rate', 0)*100:.1f}%" if c1.get("pass_rate") is not None else "n/d"

# ---- Fixes: generalizan? ----
d.add_heading("¿Los 3 fixes generalizan a ejemplos nuevos?", level=1)

d.add_heading("Fix #1 — docente + sinónimo + atributo extra: mejora parcial, mismo bug reaparece", level=2)
d.add_paragraph(
    "Batch 1: 53.3% de aprobación, 14/30 alucinaciones. Batch 2: 56.7% de aprobación, 13/30 "
    "alucinaciones — prácticamente el mismo nivel de falla con frases distintas. El fix (respaldo "
    "semántico palabra por palabra en _semantic_detect_field()) SÍ funciona cuando la pregunta "
    "contiene al menos una palabra cercana a \"docente\" (profesor, maestro, tutor, catedrático, "
    "profe). Pero fallan sistemáticamente dos patrones que el batch 1 no había probado:"
)
d.add_paragraph(
    "(a) Preguntas perifrásticas SIN ninguna palabra sinónimo de \"docente\": \"quien dicta este "
    "curso...?\", \"la persona que dicta el curso\", \"quien es la persona responsable de "
    "calificarnos\". El respaldo palabra-por-palabra no tiene ninguna palabra individual que "
    "matchee \"docente\" con margen suficiente, así que cae a la ruta genérica de RAG.",
    style="List Bullet",
)
d.add_paragraph(
    "(b) En Curso_1472 específicamente, reapareció la MISMA persona inventada del batch 1 "
    "(\"Jorge Manrique Chávez\", un ponente invitado mencionado en un video de la semana 13) en "
    "3 de las 15 preguntas nuevas, cuando la pregunta combina un sinónimo de docente con la "
    "palabra \"correo\": id 2 (\"necesito hablar con la profe, me pasas su correo?\"), id 4 "
    "(\"quien dicta este curso y a que correo le escribo?\"), id 9 (\"me confirmas el nombre y "
    "correo de la persona que dicta el curso?\"). La misma combinación de palabras en Curso_1541 "
    "NO reprodujo un nombre inventado (ese curso no tiene un chunk distractor equivalente), lo "
    "que sugiere que además de la dilución semántica, hay una competencia de retrieval "
    "específica del contenido indexado de cada curso que el fix actual no cubre.",
    style="List Bullet",
)
ex = R2[1]  # id 2
qm = ex.get("quality_metrics", {})
d.add_paragraph(f"“{ex['query']}” — {ex['course']}", style="Normal").runs[0].bold = True
d.add_paragraph(f"Respondió: {ex['answer']}")
d.add_paragraph(f"Problema: {qm.get('hallucination_detail')}")

d.add_heading("Fix #2 — modalidad vs. forma de desarrollo: reaparece con frases que no activan el campo estructurado", level=2)
d.add_paragraph(
    "Batch 1 (Curso_1541): \"el curso es presencial o virtual?\" y \"el curso se dicta de forma "
    "presencial cierto?\" fallaban — ambas corregidas y verificadas (no reprodujeron en batch 2 "
    "con esas frases exactas). Pero dos frases nuevas con la misma intención SÍ vuelven a fallar: "
    "\"el curso se lleva de forma presencial o es virtual?\" (id 116) y \"las clases son "
    "presenciales verdad?\" (id 124), ambas afirmando \"presencial\" cuando el dato real es "
    "forma_desarrollo=Virtual sincrónica."
)
d.add_paragraph(
    "Dato relevante: en ambos casos la fuente citada es \"SESION_05_FILOSOFIA_DE_LA_EDUCACION_"
    "2026II__46881.pdf\" (un documento de sesión semanal), no el sílabo. Esto es consistente con "
    "que estas frases nuevas no disparan la resolución estructurada de campo en absoluto (donde "
    "vive el fix de jerarquía is_repeated_template) y caen directo a retrieval genérico, que no "
    "tiene esa misma protección contra filas de plantilla repetida."
)
ex = R2[115]  # id 116
qm = ex.get("quality_metrics", {})
d.add_paragraph(f"“{ex['query']}” — {ex['course']}", style="Normal").runs[0].bold = True
d.add_paragraph(f"Respondió: {ex['answer']}")
d.add_paragraph(f"Problema: {qm.get('hallucination_detail')}")

d.add_heading("Fix #3 — muestra vs. total: mejora en el patrón conocido, pero aparece un patrón NUEVO y más severo", level=2)
d.add_paragraph(
    "Para el patrón que motivó el fix (Curso_1472, muestra chica: 3 de 8 semanas), el resultado "
    "mejoró de forma real: ya no se vieron respuestas confiadas pero incorrectas del tipo "
    "\"la Unidad I tiene un total de tres semanas\" (el error exacto del batch 1). En su lugar, "
    "el modelo tiende a abstenerse (\"no encontré información específica sobre la duración total "
    "de la Unidad I\") — más seguro, pero sigue sin dar el dato correcto (8 semanas), así que "
    "queda en \"partial\" en vez de \"pass\"."
)
d.add_paragraph(
    "El hallazgo nuevo y más preocupante es en Curso_1541, que tiene el patrón INVERSO "
    "(verificado en IU_Evaluacion_Rapida_Filosofia_de_la_Educacion_2026-II.xlsx): la nota de "
    "participación usa CASI TODAS las semanas de la unidad (7 de 8, excluyendo solo la semana "
    "de examen), no una muestra chica. Ante este patrón más complejo, el modelo no solo falla "
    "en dar el total correcto — inventa un número nuevo mezclando las semanas de AMBAS unidades: "
    "id 131 respondió que la \"Unidad I\" dura \"12 semanas\", sumando \"semanas 1 a 7\" (que sí "
    "son de la Unidad I) con \"semanas 9 a 15\" (que son de la Unidad II), y el id 137 llegó a "
    "decir que hay \"14 unidades de análisis y aplicación\". Es decir: el fix evita el error "
    "original (confundir una muestra chica con el total) pero no cubre el caso donde hay DOS "
    "muestras casi-totales (una por unidad) — ahí el modelo las combina en una alucinación nueva."
)
ex = R2[130]  # id 131
qm = ex.get("quality_metrics", {})
d.add_paragraph(f"“{ex['query']}” — {ex['course']}", style="Normal").runs[0].bold = True
d.add_paragraph(f"Respondió: {ex['answer']}")
d.add_paragraph(f"Problema: {qm.get('hallucination_detail')}")

# ---- Patrones nuevos de falla ----
d.add_heading("Patrones de falla nuevos (no relacionados a los 3 fixes)", level=1)

d.add_heading("Fila de plantilla de un xlsx leída como dato real (\"35 estudiantes matriculados\")", level=2)
ex = next(r for r in R2 if r["id"] == 189)
qm = ex.get("quality_metrics", {})
d.add_paragraph(
    "Los registros de participación y desempeño en xlsx (de ambos cursos) traen una hoja de "
    "plantilla con placeholders literales \"Estudiante 1\", \"Estudiante 2\", ... \"Estudiante 35\" "
    "y la instrucción \"Reemplace Estudiante 1, Estudiante 2 por los apellidos y nombres reales\". "
    f"Ante “{ex['query']}” ({ex['course']}), Idania citó ese xlsx y respondió: "
    f"“{ex['answer'][:220]}...”. Problema: {qm.get('hallucination_detail')} "
    "Recomendación: filtrar estas filas de placeholder en la extracción/indexado de xlsx, o "
    "marcarlas explícitamente como plantilla (mismo mecanismo is_repeated_template ya usado para "
    "otras tablas)."
)

d.add_heading("Navegación: confusión entre semanas adyacentes o paralelas", level=2)
d.add_paragraph(
    "4 de 16 preguntas de navegación (25%) citaron una semana distinta a la real, casi siempre "
    "una semana cercana o con un rol paralelo en la otra unidad:"
)
for iid, detail in [
    (68, "\"en que semana veo la historia del colegio odontologico del peru?\" → respondió Semana 03 (correcta: Semana 04; el material real tiene el rótulo \"Senana 04\", con error de tipeo en el propio dato fuente)."),
    (170, "\"en que semana es la evaluacion sumativa de la unidad 2?\" → respondió Semana 8 (correcta: Semana 16; confundió el examen de la Unidad I con el de la Unidad II — mismo rol, unidad distinta)."),
    (172, "\"en que semana se analiza la coherencia entre fundamentos y decisiones educativas?\" → respondió Semana 3 (correcta: Semana 13; posible confusión por similitud temática entre los materiales de ambas semanas)."),
    (174, "\"en que semana veo el idealismo platonico y el pragmatismo de dewey comparados?\" → respondió Semana 2 (correcta: Semana 4)."),
]:
    d.add_paragraph(f"id {iid}: {detail}", style="List Bullet")

d.add_heading("Mapeo semana↔actividad evaluable (AF1/IF/AF2/RSU) impreciso", level=2)
ex = next(r for r in R2 if r["id"] == 143)
qm = ex.get("quality_metrics", {})
d.add_paragraph(
    f"“{ex['query']}” ({ex['course']}) → respondió Semana 6 (correcta: Semana 4), citando "
    "además una fuente de la semana 6 (U1_AF2_Matriz_Comparativa_Argumentada.pdf) como si "
    "correspondiera a la actividad AF1. No es parte de los 3 bugs corregidos, pero es el mismo "
    "tipo de riesgo: material de una semana vecina usado con seguridad para responder sobre otra."
)

d.add_heading("Contaminación cruzada entre cursos sigue siendo el punto más débil (0% en batch 2, 25% en batch 1)", level=2)
d.add_paragraph(
    "En ningún batch hubo alucinaciones reales graves en esta categoría (0 en batch 1, 1 en "
    "batch 2 de 8), pero el estilo de respuesta rara vez cumple del todo el comportamiento "
    "esperado (\"redirigir_otro_curso\"): unas veces se abstiene sin ofrecer redirigir "
    "explícitamente al curso correcto, otras responde con conocimiento general sin dejar claro "
    "que no está fundamentado en el material del curso actual (ej. id 97, explicó el pragmatismo "
    "de Dewey en detalle estando en la colección de Curso_1472, sin fuente citada). No es un "
    "problema nuevo — ya se veía en el batch 1 — pero con 0% de aprobación en batch 2 queda claro "
    "que sigue sin abordarse."
)

# ---- Lo que sigue funcionando bien ----
d.add_heading("Lo que sigue funcionando bien (validado con ejemplos nuevos)", level=1)
for txt in [
    f"Fuera de alcance: {S2['by_category']['fuera_de_alcance']['pass_rate']*100:.0f}% de aprobación en 16 preguntas nuevas (cálculos, cultura general, otras materias) — igual que en el batch 1.",
    f"Adversarial temático: {S2['by_category']['adversarial_tematico']['pass_rate']*100:.0f}% de aprobación en 10 preguntas nuevas — no reaparece alucinación de campo por mucho contexto temático sin dato real.",
    f"Conceptual sobre contenido real: {S2['by_category']['conceptual']['pass_rate']*100:.1f}% de aprobación, solo 2 alucinaciones de 30 — el RAG normal (sin campos estructurados) sigue siendo sólido para explicar temas nuevos verificados (instrumental dental antiguo, sistema de notación FDI/Haderup, corrientes filosóficas, criterios de evaluación filosófica de decisiones, etc.).",
    f"Sin respuesta / control: {S2['by_category']['sin_respuesta_control']['pass_rate']*100:.1f}% de aprobación — se sigue absteniendo correctamente casi siempre ante datos que genuinamente no existen en el curso.",
]:
    d.add_paragraph(txt, style="List Bullet")

# ---- Recomendaciones ----
d.add_heading("Recomendaciones priorizadas", level=1)
d.add_paragraph("1. Urgente — extender el respaldo de detección de campo a preguntas perifrásticas sin palabra-sinónimo")
d.add_paragraph(
    "El respaldo palabra-por-palabra (Fix #1) requiere que AL MENOS una palabra de la pregunta "
    "matchee semánticamente el campo. Preguntas como \"quien dicta este curso\" o \"la persona "
    "responsable de calificarnos\" no tienen ninguna palabra así y siguen cayendo a retrieval "
    "genérico, donde compiten con chunks irrelevantes (ej. el ponente invitado de un video). "
    "Evaluar un tercer nivel de respaldo: embeber la pregunta completa contra una paráfrasis "
    "genérica del campo (no solo su nombre), o subir la prioridad del índice de campos "
    "estructurados sobre retrieval genérico cuando la intención de la pregunta (detectable por "
    "patrones como \"quien...\", \"quien dicta\", \"responsable de\") sugiere un dato de persona/"
    "contacto, incluso sin matchear ninguna palabra individual."
)
d.add_paragraph("2. Urgente — revisar por qué ciertas frases de modalidad no activan la resolución estructurada")
d.add_paragraph(
    "\"el curso se lleva de forma presencial o es virtual?\" y \"las clases son presenciales "
    "verdad?\" terminan citando un documento de sesión semanal en vez del sílabo, lo que sugiere "
    "que no pasan por _detect_row_by_value()/resolve_field() en absoluto. Si la intención (¿es "
    "presencial o virtual?) se puede detectar de forma genérica, forzar que este tipo de pregunta "
    "pase siempre por la resolución estructurada antes de caer a retrieval genérico."
)
d.add_paragraph("3. Medio — generalizar la instrucción de \"muestra vs. total\" al caso de DOS muestras (una por unidad)")
d.add_paragraph(
    "El fix actual reduce la confianza indebida pero no evita que el modelo mezcle las semanas de "
    "dos unidades distintas cuando cada una tiene su propia muestra grande. Reforzar el prompt "
    "para que, ante una pregunta sobre UNA unidad específica, filtre explícitamente los "
    "fragmentos que corresponden a la unidad preguntada antes de sumar/contar semanas."
)
d.add_paragraph("4. Medio — filtrar filas de plantilla/placeholder en la extracción de xlsx")
d.add_paragraph(
    "\"Estudiante 1\"..\"Estudiante 35\" (instrucción de plantilla, no datos reales de matrícula) "
    "se puede citar como si fuera el número real de alumnos. Aplicar el mismo criterio de "
    "is_repeated_template (o una regla de detección de placeholders) a este tipo de fila antes de "
    "indexarla o de dejar que el generador la use como evidencia numérica."
)
d.add_paragraph("5. Bajo — mejorar consistencia de redirect en contaminación cruzada")
d.add_paragraph(
    "0 alucinaciones graves, pero el estilo de respuesta rara vez ofrece explícitamente redirigir "
    "al curso correcto o aclara que respondió con conocimiento general no fundamentado en el "
    "curso actual (ej. id 97). No es urgente por exactitud, sí por claridad para el estudiante."
)

# ---- Metodología ----
d.add_heading("Metodología", level=1)
for txt in [
    "Datos: mismo espejo local de producción usado en el batch 1 (Curso_1472: 4 337 puntos, Curso_1541: 3 593 puntos), sin tocar producción.",
    "200 preguntas NUEVAS (100 por curso), mismas 10 categorías que el batch 1, pero sobre semanas/archivos que el batch 1 casi no tocó — verificado explorando con Qdrant scroll() los document_filename/section_name reales de ambas colecciones antes de escribir las preguntas (ver build_eval_dataset_200_v2.py).",
    "Todo known_fact de categoría conceptual, navegación y unidad/semana se verificó leyendo directamente el texto real de los chunks indexados (no se inventó ningún dato) — incluyendo dos hallazgos nuevos usados como ground truth: el patrón de muestra chica de Curso_1472 (S1+S3+S4 / S9+S12+S13) confirmado también para nueva semana 4/5/6/7/10/13/14, y el patrón inverso de Curso_1541 (S1-S7 excluye solo S8; S9-S15 excluye solo S16), verificados en los xlsx REGISTRO_...2026.xlsx e IU_Evaluacion_Rapida_...2026-II.xlsx.",
    "Se incluyeron regresiones de los 3 bugs corregidos con frases y campos DISTINTOS a los ya probados en el batch 1, para medir generalización real y no solo memorización de las frases exactas del reporte anterior.",
    "Juez: mismo modelo y rúbrica estricta de grounding que el batch 1 (gpt-4o-mini, con el dato real verificado incluido en el prompt cuando existía) — se validó antes de correr el batch completo que el juez sigue detectando correctamente tanto respuestas correctas fundamentadas como alucinaciones reales conocidas.",
    "Pipeline evaluado: rag_pipeline.answer_query() completo (mismo pipeline real que producción), con los 3 fixes de esta sesión ya aplicados en campos_estructurados.py y strategies/generation.py.",
]:
    d.add_paragraph(txt, style="List Bullet")

# ---- Conclusión ----
d.add_heading("Conclusión", level=1)
d.add_paragraph(
    "Los 3 fixes NO fueron un sobreajuste vacío a las frases exactas del batch 1 — reducen "
    "realmente la frecuencia de cada patrón cuando la pregunta se parece razonablemente al caso "
    "que los motivó. Pero ninguno generaliza por completo: los tres bugs siguen siendo "
    "reproducibles con ejemplos nuevos suficientemente distintos (perífrasis sin palabra clave, "
    "frases de modalidad que no activan la resolución estructurada, y un patrón de \"muestra vs. "
    "total\" más complejo con dos unidades). El resultado neto en este batch — pass rate más bajo "
    "y hallucination rate más alto que el batch 1 — no significa que el sistema haya empeorado; "
    "significa que el batch 1 medía una porción más fácil del problema real. Recomendación: no "
    "dar estos 3 fixes por cerrados; usar los hallazgos de este documento (sección "
    "\"Recomendaciones priorizadas\") como la siguiente ronda de trabajo antes de considerar el "
    "problema de alucinaciones resuelto."
)

out_path = "Data prueba/Idania - 200 Pruebas v2 (Ejemplos Nuevos, Post-Correcciones).docx"
d.save(out_path)
print("Guardado:", out_path)
