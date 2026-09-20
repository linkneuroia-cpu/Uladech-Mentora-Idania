"""
evaluate_vectorization_quality.py
==================================
Evaluación de CALIDAD DE VECTORIZACIÓN (extracción/chunking/indexación) de
Curso_1472 y Curso_1541 contra el mirror local dev (Qdrant localhost:6333 +
Postgres U_F1_Profundizacion_desarrollo, ENV=DESARROLLO en .env).

100% de solo lectura: solo hace scroll() sobre Qdrant y SELECT sobre
Postgres. No hace upsert, no hace set_payload, no toca curso_archivos.

Salida: evaluate_vectorization_results.json (en la raíz del proyecto),
consumido después por generate_vectorization_report.py para armar el
.docx final.
"""
import json
import re
import unicodedata
from collections import Counter, defaultdict
from pathlib import Path

from qdrant_client import QdrantClient

import db
from settings import settings

COLLECTIONS = ["Curso_1472", "Curso_1541"]
OUT_PATH = Path(__file__).parent / "evaluate_vectorization_results.json"

STRUCTURED_TYPES = ("pdf", "docx")  # ver definitions._STRUCTURED_BLOCK_EXTRACTORS

assert not settings.is_produccion, (
    "ABORTADO: este script es de solo lectura contra el mirror LOCAL DEV. "
    "settings.is_produccion=True — no se ejecuta contra producción."
)

client = QdrantClient(
    url=f"http://{settings.QDRANT_HOST}:{settings.QDRANT_PORT}",
    timeout=120,
    check_compatibility=False,
)


# ═══════════════════════════════════════════════════════════════════
# Heurísticas de "garbled" / mala extracción
# ═══════════════════════════════════════════════════════════════════

_REPLACEMENT_CHAR = "�"


def _alpha_ratio(text: str) -> float:
    letters = sum(1 for c in text if c.isalpha())
    return letters / len(text) if text else 0.0


def _max_char_run(text: str) -> int:
    runs = re.findall(r"(.)\1{4,}", text)  # 5+ repeticiones del mismo char
    if not runs:
        return 0
    # recomputar longitud real de cada run encontrado
    best = 0
    for m in re.finditer(r"(.)\1{4,}", text):
        best = max(best, len(m.group(0)))
    return best


def classify_chunk_quality(text: str, chunk_type: str) -> list:
    """Devuelve lista de flags de calidad para un chunk. chunk_type
    'table'/'table_row' se excluye del chequeo de alpha_ratio (una tabla
    Markdown tiene pipes/números legítimamente, no es señal de garbled)."""
    flags = []
    length = len(text)
    if length < 20:
        flags.append("too_short")
    if length > 3000:
        flags.append("too_long")
    if _REPLACEMENT_CHAR in text:
        flags.append("replacement_char")
    if _max_char_run(text) >= 8:
        flags.append("repeated_char_run")
    is_prose = chunk_type not in ("table", "table_row")
    if is_prose and length >= 30 and _alpha_ratio(text) < 0.35:
        flags.append("low_alpha_ratio")
    return flags


# ═══════════════════════════════════════════════════════════════════
# Semanas — parseo de section_name
# ═══════════════════════════════════════════════════════════════════

_WEEK_RE = re.compile(r"semana\s+0?(\d{1,2})", re.IGNORECASE)

# ═══════════════════════════════════════════════════════════════════
# Patrones de contenido "pseudo-LaTeX" garbled — señal de que el fallback
# de OCR de fórmulas (_find_formula_regions/_ocr_page_region/pix2tex en
# definitions.py) disparó sobre una región que NO era una fórmula real
# (tabla sin líneas completas, diagrama, foto) y el modelo alucinó texto
# sin sentido en vez de fallar limpio.
# ═══════════════════════════════════════════════════════════════════
_GARBLED_PATTERNS = {
    "object_object": re.compile(r"\[object Object\]"),
    "mathrm_gibberish": re.compile(r"mathrm\{[a-z]{6,}\}"),
    "scriptstyle": re.compile(r"scriptstyle"),
    "begin_array": re.compile(r"begin\{array\}"),
}


def parse_week(section_name: str):
    if not section_name:
        return None
    m = _WEEK_RE.search(section_name)
    if not m:
        return None
    n = int(m.group(1))
    return n if 1 <= n <= 20 else None


# ═══════════════════════════════════════════════════════════════════
# 1) Scroll completo de Qdrant por colección
# ═══════════════════════════════════════════════════════════════════

def scroll_collection(collection_name: str) -> list:
    all_points = []
    offset = None
    while True:
        points, offset = client.scroll(
            collection_name=collection_name,
            limit=500,
            offset=offset,
            with_payload=True,
            with_vectors=False,
        )
        all_points.extend(points)
        if offset is None:
            break
    return all_points


def analyze_collection(collection_name: str) -> dict:
    print(f"\n=== {collection_name} ===")
    info = client.get_collection(collection_name)
    points_count_reported = info.points_count or 0

    points = scroll_collection(collection_name)
    print(f"  scroll() devolvió {len(points)} puntos (reportado por Qdrant: {points_count_reported})")

    docs = defaultdict(lambda: {
        "chunks": 0,
        "chunk_type_counts": Counter(),
        "format": None,
        "file_type": None,
        "section_names": set(),
        "weeks": set(),
        "lengths": [],
        "quality_flags": Counter(),
        "flagged_examples": [],
        "source_type": Counter(),
        "missing_chunk_type_field": 0,
    })

    global_chunk_type_counts = Counter()
    global_quality_flags = Counter()
    global_flag_chunktype_crosstab = Counter()
    global_flagged_examples = []
    global_flagged_examples_by_flag = defaultdict(list)
    missing_chunk_type_field_total = 0
    garbled_pattern_counts = Counter()
    garbled_pattern_docs = defaultdict(Counter)
    garbled_examples = defaultdict(list)
    week_to_files = defaultdict(set)
    section_names_all = set()
    total_chunks = 0

    for p in points:
        payload = p.payload or {}
        filename = payload.get("filename") or "(sin filename)"
        text = payload.get("text") or ""
        chunk_type_raw = payload.get("chunk_type")
        has_chunk_type_field = "chunk_type" in payload
        chunk_type = chunk_type_raw or "prose"
        section_name = payload.get("section_name")
        file_type = payload.get("file_type")
        fmt = payload.get("format")
        source_type = payload.get("source_type")

        d = docs[filename]
        d["chunks"] += 1
        d["chunk_type_counts"][chunk_type] += 1
        d["format"] = fmt or d["format"]
        d["file_type"] = file_type or d["file_type"]
        d["lengths"].append(len(text))
        if section_name:
            d["section_names"].add(section_name)
            section_names_all.add(section_name)
        week = parse_week(section_name) if section_name else None
        if week:
            d["weeks"].add(week)
            week_to_files[week].add(filename)
        if source_type:
            d["source_type"][source_type] += 1
        if not has_chunk_type_field:
            d["missing_chunk_type_field"] += 1
            missing_chunk_type_field_total += 1

        flags = classify_chunk_quality(text, chunk_type)
        for f in flags:
            d["quality_flags"][f] += 1
            global_quality_flags[f] += 1
            global_flag_chunktype_crosstab[(f, chunk_type)] += 1
        if flags and len(d["flagged_examples"]) < 3:
            d["flagged_examples"].append({
                "chunk": payload.get("chunk"),
                "chunk_type": chunk_type,
                "flags": flags,
                "text_excerpt": text[:300],
                "length": len(text),
            })
        if flags and len(global_flagged_examples) < 40:
            global_flagged_examples.append({
                "filename": filename,
                "chunk": payload.get("chunk"),
                "chunk_type": chunk_type,
                "flags": flags,
                "text_excerpt": text[:300],
                "length": len(text),
            })
        for f in flags:
            key = f"{f}|{chunk_type}"
            if len(global_flagged_examples_by_flag[key]) < 8:
                global_flagged_examples_by_flag[key].append({
                    "filename": filename,
                    "chunk": payload.get("chunk"),
                    "page": payload.get("page"),
                    "text_excerpt": text[:300],
                    "length": len(text),
                })

        for pname, rx in _GARBLED_PATTERNS.items():
            if rx.search(text):
                garbled_pattern_counts[pname] += 1
                garbled_pattern_docs[pname][filename] += 1
                if len(garbled_examples[pname]) < 6:
                    garbled_examples[pname].append({
                        "filename": filename,
                        "chunk": payload.get("chunk"),
                        "page": payload.get("page"),
                        "text_excerpt": text[:300],
                    })

        global_chunk_type_counts[chunk_type] += 1
        total_chunks += 1

    # Separación tabla/fila/prosa por documento (solo pdf/docx)
    structured_ok = []       # tiene table o table_row Y prose
    structured_prose_only = []  # es pdf/docx pero solo prose (posible falla)
    structured_table_only = []  # solo tablas, sin prose (raro, informativo)

    for filename, d in docs.items():
        ft = d["file_type"] or d["format"]
        if ft not in STRUCTURED_TYPES:
            continue
        has_table = d["chunk_type_counts"]["table"] > 0 or d["chunk_type_counts"]["table_row"] > 0
        has_prose = d["chunk_type_counts"]["prose"] > 0
        if has_table and has_prose:
            structured_ok.append(filename)
        elif has_prose and not has_table:
            structured_prose_only.append(filename)
        elif has_table and not has_prose:
            structured_table_only.append(filename)

    documents_summary = []
    for filename, d in sorted(docs.items(), key=lambda kv: -kv[1]["chunks"]):
        lengths = d["lengths"]
        documents_summary.append({
            "filename": filename,
            "chunks": d["chunks"],
            "chunk_type_counts": dict(d["chunk_type_counts"]),
            "format": d["format"],
            "file_type": d["file_type"],
            "avg_len": round(sum(lengths) / len(lengths), 1) if lengths else 0,
            "min_len": min(lengths) if lengths else 0,
            "max_len": max(lengths) if lengths else 0,
            "weeks": sorted(d["weeks"]),
            "section_names": sorted(d["section_names"]),
            "quality_flags": dict(d["quality_flags"]),
            "flagged_examples": d["flagged_examples"],
            "source_type": dict(d["source_type"]),
            "missing_chunk_type_field": d["missing_chunk_type_field"],
        })

    weeks_present = sorted(week_to_files.keys())
    weeks_missing = sorted(set(range(1, 17)) - set(weeks_present))

    return {
        "collection_name": collection_name,
        "points_count_reported": points_count_reported,
        "points_scrolled": len(points),
        "total_documents": len(docs),
        "total_chunks": total_chunks,
        "global_chunk_type_counts": dict(global_chunk_type_counts),
        "global_quality_flags": dict(global_quality_flags),
        "global_flag_chunktype_crosstab": {f"{k[0]}|{k[1]}": v for k, v in global_flag_chunktype_crosstab.items()},
        "global_flagged_examples": global_flagged_examples,
        "global_flagged_examples_by_flag": dict(global_flagged_examples_by_flag),
        "garbled_pattern_counts": dict(garbled_pattern_counts),
        "garbled_pattern_docs": {k: dict(v) for k, v in garbled_pattern_docs.items()},
        "garbled_examples": dict(garbled_examples),
        "missing_chunk_type_field_total": missing_chunk_type_field_total,
        "structured_docs_ok": structured_ok,
        "structured_docs_prose_only": structured_prose_only,
        "structured_docs_table_only": structured_table_only,
        "documents": documents_summary,
        "weeks_present": weeks_present,
        "weeks_missing": weeks_missing,
        "week_to_files": {k: sorted(v) for k, v in week_to_files.items()},
        "section_names_all": sorted(section_names_all),
    }


# ═══════════════════════════════════════════════════════════════════
# 2) Ledger (curso_archivos) vs Qdrant real
# ═══════════════════════════════════════════════════════════════════

def compare_ledger(collection_name: str, qdrant_filenames: set) -> dict:
    rows = db.fetch_all(
        "SELECT filename, estado, document_hash, last_indexed_at "
        "FROM curso_archivos WHERE qdrant_collection_name=%s ORDER BY filename",
        (collection_name,),
    )
    ledger_filenames = {r["filename"] for r in rows}
    ledger_by_name = {r["filename"]: r for r in rows}

    vectorizado_estados = {"vectorizado"}
    vectorizado_sin_chunks = []
    for r in rows:
        if r["estado"] in vectorizado_estados and r["filename"] not in qdrant_filenames:
            vectorizado_sin_chunks.append({
                "filename": r["filename"],
                "estado": r["estado"],
                "last_indexed_at": str(r["last_indexed_at"]) if r["last_indexed_at"] else None,
            })

    en_qdrant_no_ledger = sorted(qdrant_filenames - ledger_filenames)

    estado_counts = Counter(r["estado"] for r in rows)

    # archivos en ledger con estado activo (nuevo/posible_actualizacion) que
    # todavía no tienen chunks -- esperado (cola de trabajo pendiente), se
    # reporta aparte de "vectorizado_sin_chunks" (que sí sería una falla real).
    pendientes_sin_chunks = [
        r["filename"] for r in rows
        if r["estado"] in ("nuevo", "posible_actualizacion") and r["filename"] not in qdrant_filenames
    ]

    return {
        "ledger_total_filas": len(rows),
        "ledger_estado_counts": dict(estado_counts),
        "qdrant_distinct_filenames": len(qdrant_filenames),
        "vectorizado_pero_sin_chunks_en_qdrant": vectorizado_sin_chunks,
        "en_qdrant_pero_no_en_ledger": en_qdrant_no_ledger,
        "pendientes_en_cola_sin_chunks": pendientes_sin_chunks,
    }


# ═══════════════════════════════════════════════════════════════════
# 3) Índice de Campos Estructurados
# ═══════════════════════════════════════════════════════════════════

def analyze_campos_estructurados(collection_name: str) -> dict:
    rows = db.fetch_all(
        "SELECT field_normalized, document_filename, is_repeated_template, chunk_type "
        "FROM coleccion_campos_estructurados WHERE qdrant_collection_name=%s",
        (collection_name,),
    )
    if not rows:
        return {
            "total_field_records": 0,
            "distinct_fields": 0,
            "repeated_template_true": 0,
            "repeated_template_false": 0,
            "silabo_docs": [],
        }

    distinct_fields = len({r["field_normalized"] for r in rows})
    true_count = sum(1 for r in rows if r["is_repeated_template"])
    false_count = len(rows) - true_count

    silabo_docs = {}
    for r in rows:
        fn = (r["document_filename"] or "").lower()
        if "silabo" in fn or "sílabo" in fn or "silabus" in fn:
            key = r["document_filename"]
            entry = silabo_docs.setdefault(key, {"true": 0, "false": 0})
            entry["true" if r["is_repeated_template"] else "false"] += 1

    return {
        "total_field_records": len(rows),
        "distinct_fields": distinct_fields,
        "repeated_template_true": true_count,
        "repeated_template_false": false_count,
        "silabo_docs": [
            {"filename": k, "rows_unique": v["false"], "rows_repeated_template": v["true"]}
            for k, v in silabo_docs.items()
        ],
    }


# ═══════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════

def main():
    results = {}
    for collection_name in COLLECTIONS:
        analysis = analyze_collection(collection_name)
        qdrant_filenames = {d["filename"] for d in analysis["documents"]}
        analysis["ledger_comparison"] = compare_ledger(collection_name, qdrant_filenames)
        analysis["campos_estructurados"] = analyze_campos_estructurados(collection_name)
        results[collection_name] = analysis

        print(f"  documentos: {analysis['total_documents']}, chunks: {analysis['total_chunks']}")
        print(f"  chunk_type: {analysis['global_chunk_type_counts']}")
        print(f"  quality_flags: {analysis['global_quality_flags']}")
        print(f"  ledger: {analysis['ledger_comparison']['ledger_estado_counts']}")
        print(f"  vectorizado_sin_chunks: {len(analysis['ledger_comparison']['vectorizado_pero_sin_chunks_en_qdrant'])}")
        print(f"  campos_estructurados: {analysis['campos_estructurados']['distinct_fields']} campos distintos, "
              f"{analysis['campos_estructurados']['total_field_records']} filas")
        print(f"  semanas presentes: {analysis['weeks_present']}")
        print(f"  semanas faltantes: {analysis['weeks_missing']}")

    OUT_PATH.write_text(json.dumps(results, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(f"\nResultados guardados en {OUT_PATH}")


if __name__ == "__main__":
    main()
