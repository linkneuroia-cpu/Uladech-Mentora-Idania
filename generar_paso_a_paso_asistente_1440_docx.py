"""
generar_paso_a_paso_asistente_1440_docx.py
===========================================
Genera el Word con el paso a paso (capturas incluidas) de la CORRECCIÓN de
la vectorización del asistente "normal" del curso Moodle 1440 (recreación de
la colección con openai/text-embedding-3-small + re-vectorización + ajuste
del asistente), hecha con Playwright contra producción. Lee las capturas y
la metadata que deja el spec tests/e2e/corregir-vectorizacion-1440.spec.ts en
test-results/asistente_1440/.

Ejecutar:  python generar_paso_a_paso_asistente_1440_docx.py
Salida:    Paso_a_paso_Asistente_Curso_1440.docx
"""
import json
import os
from docx import Document
from docx.shared import Pt, RGBColor, Inches
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.enum.table import WD_TABLE_ALIGNMENT

AZUL = RGBColor(0x1F, 0x3B, 0x73)
AZUL_CLARO = RGBColor(0x2E, 0x5C, 0xA8)
GRIS = RGBColor(0x44, 0x44, 0x44)

OUT_DIR = os.path.join("test-results", "asistente_1440")
PASOS_JSON = os.path.join(OUT_DIR, "pasos.json")


def set_heading_style(doc):
    for i in range(1, 4):
        try:
            style = doc.styles[f"Heading {i}"]
            style.font.color.rgb = AZUL
            style.font.name = "Calibri"
        except KeyError:
            pass


def add_bullets(doc, items):
    for it in items:
        doc.add_paragraph(it, style="List Bullet")


def add_kv_table(doc, headers, rows):
    table = doc.add_table(rows=1, cols=len(headers))
    table.style = "Light Grid Accent 1"
    table.alignment = WD_TABLE_ALIGNMENT.CENTER
    hdr = table.rows[0].cells
    for i, h in enumerate(headers):
        hdr[i].text = ""
        run = hdr[i].paragraphs[0].add_run(h)
        run.bold = True
        run.font.size = Pt(10)
    for row in rows:
        cells = table.add_row().cells
        for i, val in enumerate(row):
            cells[i].text = ""
            run = cells[i].paragraphs[0].add_run(str(val))
            run.font.size = Pt(9.5)
    return table


def build():
    with open(PASOS_JSON, "r", encoding="utf-8") as f:
        data = json.load(f)

    doc = Document()
    set_heading_style(doc)
    normal = doc.styles["Normal"]
    normal.font.name = "Calibri"
    normal.font.size = Pt(11)

    # ── Portada ──────────────────────────────────────────────────
    title = doc.add_paragraph()
    title.alignment = WD_ALIGN_PARAGRAPH.CENTER
    run = title.add_run("Corrección de la vectorización del curso 1440")
    run.font.size = Pt(26)
    run.font.color.rgb = AZUL
    run.bold = True

    sub = doc.add_paragraph()
    sub.alignment = WD_ALIGN_PARAGRAPH.CENTER
    run2 = sub.add_run("Paso a paso automatizado con Playwright — Gestor F1 ULADECH (producción)")
    run2.font.size = Pt(14)
    run2.font.color.rgb = AZUL_CLARO
    doc.add_page_break()

    # ── 1. Resumen ───────────────────────────────────────────────
    doc.add_heading("1. Resumen", level=1)
    doc.add_paragraph(
        f"El asistente de IA \"Idania\" del curso Moodle {data['CURSO_ID']} había quedado "
        f"vectorizado con el embedding por defecto del sistema "
        f"(sentence-transformers/all-MiniLM-L6-v2, 384 dimensiones) por un bug real del "
        f"módulo \"Automático\" del gestor. En este documento se detalla la corrección: "
        f"se recreó la colección \"{data['COLECCION']}\" con el embedding correcto "
        f"(openai/text-embedding-3-small, 1536 dimensiones + bm25 + Cosine — el mismo que "
        f"usa la RD del curso 1541), se re-vectorizó el curso completo y se fijó el "
        f"embedding del asistente, dejando todo el pipeline consistente de punta a punta. "
        f"Todo el procedimiento se ejecutó, documentándolo paso a paso con capturas, "
        f"mediante automatización de navegador (Playwright) contra el gestor en producción "
        f"(https://educaia.uladech.edu.pe/f1/)."
    )
    add_kv_table(
        doc,
        ["Campo", "Valor"],
        [
            ["Curso Moodle (courseid)", data["CURSO_ID"]],
            ["Colección en Qdrant", data["COLECCION"]],
            ["Embedding denso", data["DENSE"]],
            ["Embedding disperso (sparse)", data["SPARSE"]],
            ["Dimensión de los vectores", "1536"],
            ["Nombre del asistente", "Idania"],
            ["Tipo", "Normal (sin RD)"],
            ["Token del asistente", data["token"]],
            ["URL pública", data["publicUrl"]],
        ],
    )
    doc.add_paragraph()

    # ── 2. Configuración replicada de 1541 ──────────────────────
    doc.add_heading("2. Configuración alineada con el asistente de referencia (curso 1541)", level=1)
    doc.add_paragraph(
        "El asistente de 1541 es de tipo RD (hereda su embedding de la RD "
        "\"RD-PRE-EDUCACION-VI-FILOSOFIA DE LA EDUCACION\": openai/text-embedding-3-small + "
        "bm25). El asistente de 1440, al no pertenecer a ninguna RD, es de tipo \"normal\" "
        "— pero tras la corrección quedó configurado con el MISMO embedding denso y sparse "
        "que la referencia (openai/text-embedding-3-small + bm25, distancia Cosine). El "
        "prompt maestro, el saludo y el mensaje de reencuentro que usan los demás "
        "asistentes \"Idania\" se mantienen idénticos."
    )
    add_kv_table(
        doc,
        ["Aspecto", "Curso 1541 (referencia)", "Curso 1440 (corregido)"],
        [
            ["Tipo de asistente", "RD (hereda embedding de la RD)", "Normal (embedding propio)"],
            ["Embedding denso", "openai/text-embedding-3-small (1536 dims)", "openai/text-embedding-3-small (1536 dims)"],
            ["Embedding sparse", "bm25", "bm25"],
            ["Métrica de distancia", "Cosine", "Cosine"],
            ["Reranking / Generación / ETL / Contextual", "Valor por defecto del sistema", "Valor por defecto del sistema"],
            ["Prompt maestro", "\"Te llamas Idania y eres la mentora...\"", "Idéntico"],
            ["Máximo de preguntas por conversación", "10", "10"],
        ],
    )
    doc.add_paragraph()

    # ── 3. Bug encontrado y corregido ───────────────────────────
    doc.add_heading("3. Bug encontrado y corregido en el código", level=1)
    doc.add_paragraph(
        "La causa de que \"Curso_1440\" hubiera quedado con el embedding equivocado era un "
        "bug real en el módulo \"Automático\" del gestor (frontend/interfaz.html, funciones "
        "aPreview()/aDownVal()): ese módulo nunca enviaba el parámetro model_name al "
        "backend al vectorizar, así que el backend usaba SIEMPRE el embedding por defecto "
        "de toda la plataforma (sentence-transformers/all-MiniLM-L6-v2, 384 dimensiones), "
        "sin importar con qué embedding se hubiera creado la colección destino. El módulo "
        "\"Crear colección\" sí permitía elegir el embedding, de ahí la inconsistencia."
    )
    doc.add_paragraph(
        "El bug se corrigió en el código: se agregó en el Paso 1 del módulo \"Automático\" "
        "un selector \"Modelo de embeddings\" (#auto-model, poblado desde "
        "GET /api/config/dense), y aPreview()/aDownVal() ahora envían model_name al "
        "backend cuando se elige un modelo (si se deja en blanco, el comportamiento es "
        "idéntico al anterior). El backend ya soportaba model_name como query param, así "
        "que no requirió cambios. Con esto, el módulo \"Automático\" queda utilizable para "
        "CUALQUIER colección no-default de ahora en adelante, no solo para 1440."
    )
    doc.add_paragraph(
        "Adicionalmente, durante la corrección se detectó y arregló un segundo bug real en "
        "el backend (rds.resolve_collection_normal): al borrar y recrear una colección "
        "\"normal\", quedaban filas viejas en Postgres con el nombre de colección en NULL "
        "que la consulta podía devolver por error, haciendo que el chat público mostrara "
        "\"no hay colección asignada\" aun con la colección existente. La consulta ahora "
        "ignora esas filas y elige la vigente."
    )
    doc.add_paragraph(
        "Nota (gap de producto aparte, no resuelto en la UI): la interfaz de edición de un "
        "asistente \"normal\" no expone ningún campo para fijar su propio "
        "dense_strategy/sparse_strategy, por lo que ese ajuste se aplicó una sola vez por "
        "API directa (PATCH /api/asistentes/22 → dense_strategy=openai/text-embedding-3-small, "
        "sparse_strategy=bm25). Sin ese ajuste, el asistente caería igual al embedding por "
        "defecto del sistema en tiempo de consulta y volvería a fallar por mismatch de "
        "dimensión contra la colección de 1536."
    )
    doc.add_paragraph()

    # ── 4. Paso a paso ───────────────────────────────────────────
    doc.add_heading("4. Paso a paso ejecutado", level=1)
    for i, p in enumerate(data["pasos"], start=1):
        doc.add_heading(f"Paso {i}. {p['titulo']}", level=2)
        doc.add_paragraph(p["descripcion"])
        img_path = os.path.join(OUT_DIR, p["file"])
        if os.path.exists(img_path):
            doc.add_picture(img_path, width=Inches(6.3))
        else:
            warn = doc.add_paragraph(f"[captura no encontrada: {p['file']}]")
            warn.runs[0].font.color.rgb = GRIS
        doc.add_paragraph()

    # ── 5. Verificación final ────────────────────────────────────
    doc.add_heading("5. Verificación", level=1)
    add_bullets(
        doc,
        [
            "La colección \"" + data["COLECCION"] + "\" existe en Qdrant con esquema híbrido, distancia Cosine y vector_size = 1536 (openai/text-embedding-3-small).",
            "El curso 1440 quedó re-vectorizado completo (documentos, carpetas y videos de Moodle).",
            "El asistente \"Idania\" quedó activo, con dense_strategy = openai/text-embedding-3-small y sparse_strategy = bm25.",
            "El chat público respondió correctamente a una primera pregunta usando contenido real del curso, sin error de dimensión en Qdrant.",
            "URL pública final: " + data["publicUrl"],
        ],
    )

    os.makedirs("doc", exist_ok=True)
    out = os.path.join("doc", "Paso_a_paso_Asistente_Curso_1440.docx")
    doc.save(out)
    print(f"Generado: {out}")


if __name__ == "__main__":
    build()
