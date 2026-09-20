# -*- coding: utf-8 -*-
"""
run_eval_200_hallucinations.py
================================
Corre las 200 preguntas de eval_dataset_200_hallucinations.json contra las
colecciones CANÓNICAS locales (Curso_1472 / Curso_1541 -- espejo exacto de
producción, restaurado desde snapshot real esta sesión) vía
rag_pipeline.answer_query(), y usa un LLM-juez estricto enfocado en
alucinaciones/grounding (mismo diseño de rúbrica que run_mentor_eval_200.py,
ya validado en esta sesión) para calificar cada respuesta.

Salidas: evaluation_200_hallucinations_results.json/.csv,
evaluation_200_hallucinations_summary.json.
"""
import asyncio
import csv
import json
import time
from collections import defaultdict
from typing import Any, Dict, List

from openai import OpenAI

import rag_pipeline
from settings import settings

DENSE = "openai/text-embedding-3-small"
SPARSE = "bm25"
COLLECTIONS = {"Curso_1472": "Curso_1472", "Curso_1541": "Curso_1541"}

JUDGE_MODEL = "gpt-4o-mini"
JUDGE_CONCURRENCY = 6
_judge_client = OpenAI(api_key=settings.OPENAI_API_KEY)
_judge_sem = asyncio.Semaphore(JUDGE_CONCURRENCY)
_gen_sem = asyncio.Semaphore(4)

with open("eval_dataset_200_hallucinations.json", encoding="utf-8") as f:
    DATASET = json.load(f)

JUDGE_SYSTEM = """Eres un evaluador experto de un tutor/mentor académico universitario (RAG) llamado Idania.
Evalúas UNA respuesta de Idania a un estudiante, con base en: la pregunta, el comportamiento esperado
(definido por el diseñador de la prueba), un dato real verificado (si existe, es la verdad objetiva contra
producción -- no algo opcional), la respuesta real de Idania, y la evidencia real que Idania tuvo
disponible. Sé estricto y honesto — no le regales puntos a una respuesta fluida pero no sustentada.
Presta ESPECIAL atención a: (a) si Idania inventa un dato (nombre, correo, teléfono, cifra) que NO
aparece literal en la evidencia mostrada, (b) si confunde una MUESTRA/subconjunto con el TOTAL real
(ej. tomar 3 semanas usadas en una fórmula de calificación como si fueran el total de semanas de una
unidad), (c) si mezcla contenido de OTRO curso/documento con el que se le pidió, (d) si responde una
pregunta fuera del alcance del curso (cultura general, cálculos, otra materia) en vez de negarse y
redirigir. Prioridad de evaluación: 1) ausencia de alucinaciones, 2) fidelidad al material real,
3) correcta abstención cuando corresponde, 4) calidad pedagógica.

Devuelve SOLO un JSON con esta forma exacta:
{
  "hallucination": true/false,
  "hallucination_detail": "string breve o null -- qué inventó exactamente, si aplica",
  "groundedness": 0-10,
  "correctness": 0-10,
  "appropriate_abstention": true/false/null,
  "confused_sample_with_total": true/false,
  "cross_course_contamination": true/false,
  "answered_out_of_scope": true/false,
  "pedagogical_value": 0-10,
  "verdict": "pass" | "partial" | "fail",
  "rationale": "1-3 frases explicando el veredicto"
}"""


def _build_judge_prompt(item: Dict[str, Any], answer: str, sources: List[Dict[str, Any]]) -> str:
    src_text = "\n".join(
        f"- [{s.get('source_type')}] {s.get('filename')}: {(s.get('text') or '')[:1800]}"
        for s in sources[:5]
    ) or "(sin fuentes recuperadas)"
    known_fact = f"\nDato real verificado (verdad objetiva): {item['known_fact']}" if item.get("known_fact") else ""
    return f"""Categoría de la prueba: {item['category']}
Comportamiento esperado: {item['expected_behavior']}{known_fact}

Pregunta del estudiante: {item['query']}

Respuesta real de Idania: {answer}

Evidencia que Idania tuvo disponible (top 5 fuentes recuperadas):
{src_text}

Evalúa la respuesta de Idania según el JSON pedido."""


async def _judge(item: Dict[str, Any], answer: str, sources: List[Dict[str, Any]]) -> Dict[str, Any]:
    prompt = _build_judge_prompt(item, answer, sources)
    async with _judge_sem:
        def _call():
            resp = _judge_client.chat.completions.create(
                model=JUDGE_MODEL,
                messages=[{"role": "system", "content": JUDGE_SYSTEM}, {"role": "user", "content": prompt}],
                temperature=0.0,
                response_format={"type": "json_object"},
            )
            return resp.choices[0].message.content
        try:
            raw = await asyncio.get_event_loop().run_in_executor(None, _call)
            return json.loads(raw)
        except Exception as e:
            return {"judge_error": str(e)}


async def run_item(item: Dict[str, Any]) -> Dict[str, Any]:
    collection = COLLECTIONS[item["course"]]
    async with _gen_sem:
        t0 = time.perf_counter()
        result = await rag_pipeline.answer_query(
            collection_name=collection, query=item["query"],
            dense_strategy_name=DENSE, sparse_strategy_name=SPARSE, rerank_strategy_name="none",
        )
        total_ms = (time.perf_counter() - t0) * 1000
    judge = await _judge(item, result["answer"], result["sources"])
    verdict = judge.get("verdict") if isinstance(judge, dict) else None
    return {
        **item, "answer": result["answer"], "sources": result["sources"],
        "quality_metrics": judge, "passed": verdict == "pass",
        "total_ms": total_ms, "tokens_consumidos": result.get("tokens_consumidos"),
    }


async def main():
    print(f"Cargadas {len(DATASET)} preguntas", flush=True)
    t_start = time.time()
    sem_outer = asyncio.Semaphore(6)

    async def _guarded(item, idx):
        async with sem_outer:
            try:
                r = await run_item(item)
            except Exception as e:
                r = {**item, "error": str(e), "passed": False, "answer": None, "quality_metrics": {}}
            if (idx + 1) % 10 == 0:
                print(f"[{idx+1}/{len(DATASET)}] completadas ({time.time()-t_start:.0f}s)", flush=True)
            return r

    results = await asyncio.gather(*[_guarded(item, i) for i, item in enumerate(DATASET)])

    with open("evaluation_200_hallucinations_results.json", "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)

    rows = []
    for r in results:
        qm = r.get("quality_metrics") or {}
        rows.append({
            "id": r["id"], "course": r["course"], "category": r["category"],
            "query": r["query"], "answer": (r.get("answer") or "")[:500],
            "verdict": qm.get("verdict"), "hallucination": qm.get("hallucination"),
            "hallucination_detail": qm.get("hallucination_detail"),
            "confused_sample_with_total": qm.get("confused_sample_with_total"),
            "cross_course_contamination": qm.get("cross_course_contamination"),
            "answered_out_of_scope": qm.get("answered_out_of_scope"),
            "groundedness": qm.get("groundedness"), "correctness": qm.get("correctness"),
            "total_ms": r.get("total_ms"),
        })
    with open("evaluation_200_hallucinations_results.csv", "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)

    judged = [row for row in rows if row.get("verdict") is not None]
    summary: Dict[str, Any] = {
        "total": len(results), "n_judged": len(judged),
        "pass_rate": sum(1 for r in judged if r["verdict"] == "pass") / len(judged) if judged else None,
        "fail_rate": sum(1 for r in judged if r["verdict"] == "fail") / len(judged) if judged else None,
        "partial_rate": sum(1 for r in judged if r["verdict"] == "partial") / len(judged) if judged else None,
        "hallucination_rate": sum(1 for r in judged if r.get("hallucination") is True) / len(judged) if judged else None,
        "confused_sample_with_total_count": sum(1 for r in judged if r.get("confused_sample_with_total") is True),
        "cross_course_contamination_count": sum(1 for r in judged if r.get("cross_course_contamination") is True),
        "answered_out_of_scope_count": sum(1 for r in judged if r.get("answered_out_of_scope") is True),
        "avg_groundedness": sum(r["groundedness"] for r in judged if r.get("groundedness") is not None) / max(1, sum(1 for r in judged if r.get("groundedness") is not None)),
    }

    by_cat = defaultdict(lambda: {"n": 0, "pass": 0, "hallucinations": 0})
    for row in rows:
        by_cat[row["category"]]["n"] += 1
        if row.get("verdict") == "pass":
            by_cat[row["category"]]["pass"] += 1
        if row.get("hallucination") is True:
            by_cat[row["category"]]["hallucinations"] += 1
    summary["by_category"] = {k: {**v, "pass_rate": v["pass"] / v["n"] if v["n"] else None} for k, v in by_cat.items()}

    by_course = defaultdict(lambda: {"n": 0, "pass": 0, "hallucinations": 0})
    for row in rows:
        by_course[row["course"]]["n"] += 1
        if row.get("verdict") == "pass":
            by_course[row["course"]]["pass"] += 1
        if row.get("hallucination") is True:
            by_course[row["course"]]["hallucinations"] += 1
    summary["by_course"] = {k: {**v, "pass_rate": v["pass"] / v["n"] if v["n"] else None} for k, v in by_course.items()}

    summary["failures"] = [
        {"id": row["id"], "course": row["course"], "category": row["category"], "query": row["query"],
         "verdict": row["verdict"], "hallucination": row["hallucination"],
         "hallucination_detail": row["hallucination_detail"], "answer": row["answer"]}
        for row in rows if row.get("verdict") in ("fail", "partial") or row.get("hallucination") is True
    ]

    with open("evaluation_200_hallucinations_summary.json", "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)

    print("\n=== RESUMEN ===")
    print(json.dumps({k: v for k, v in summary.items() if k != "failures"}, ensure_ascii=False, indent=2))
    print(f"\nTerminado en {time.time()-t_start:.0f}s — {len(summary['failures'])} casos con problema")


if __name__ == "__main__":
    asyncio.run(main())
