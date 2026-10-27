"""
run_mentor_eval_200.py
========================
Evaluación integral de Idania como mentor académico (post-eliminación del
router 3.7, arquitectura Índice de Campos Estructurados + Qdrant Hybrid +
Reranking + Evidence Gate + generación grounded). Corre las ~218
interacciones de eval_dataset_200_mentor.json contra Curso_1472_experiment_v2
y Curso_1541_experiment_v2 (SOLO colecciones experimentales, nunca
producción), captura métricas de retrieval/latencia y usa un LLM-juez para
las dimensiones pedagógicas y de grounding pedidas por el usuario.

Salidas: evaluation_200_results.json, evaluation_200_results.csv,
evaluation_200_summary.json.
"""
import asyncio
import os
import csv
import json
import math
import time
import unicodedata
from collections import defaultdict
from typing import Any, Dict, List, Optional

from openai import OpenAI

import rag_pipeline
from settings import settings

EVAL_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tests", "eval")

DENSE = "openai/text-embedding-3-small"
SPARSE = "bm25"
COLLECTIONS = {"1472": "Curso_1472_experiment_v2", "1541": "Curso_1541_experiment_v2"}

JUDGE_MODEL = "gpt-4o-mini"
JUDGE_CONCURRENCY = 6
_judge_client = OpenAI(api_key=settings.OPENAI_API_KEY)
_judge_sem = asyncio.Semaphore(JUDGE_CONCURRENCY)
_gen_sem = asyncio.Semaphore(4)

with open(os.path.join(EVAL_DIR, "eval_dataset_200_mentor.json"), encoding="utf-8") as f:
    DATASET = json.load(f)


def _norm(s):
    n = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode()
    return n.strip().lower()


# ══════════════════════════════════════════════════════════════════════
# Retrieval metrics (solo cuando expected_source está definido)
# ══════════════════════════════════════════════════════════════════════

def _retrieval_metrics(candidates: List[Dict[str, Any]], expected_source: Optional[str]) -> Dict[str, Any]:
    if not expected_source:
        return {"applicable": False}
    expected_norm = _norm(expected_source)
    rank = None
    for i, c in enumerate(candidates):
        if _norm(c["payload"].get("filename")) == expected_norm:
            rank = i + 1
            break
    recall_5 = rank is not None and rank <= 5
    recall_10 = rank is not None and rank <= 10
    rr = (1.0 / rank) if rank else 0.0
    ndcg_10 = (1.0 / math.log2(rank + 1)) if rank and rank <= 10 else 0.0
    return {
        "applicable": True, "rank": rank, "recall_at_5": recall_5, "recall_at_10": recall_10,
        "reciprocal_rank": rr, "ndcg_at_10": ndcg_10, "hit": rank is not None,
    }


# ══════════════════════════════════════════════════════════════════════
# LLM-juez pedagógico/grounding
# ══════════════════════════════════════════════════════════════════════

JUDGE_SYSTEM = """Eres un evaluador experto de un tutor/mentor académico universitario (RAG) llamado Idania.
Evalúas UNA respuesta de Idania a un estudiante, con base en: la pregunta, el comportamiento esperado
(definido por el diseñador de la prueba), la respuesta real de Idania, y la evidencia real que Idania tuvo
disponible (fuentes recuperadas). Sé estricto y honesto — no le regales puntos a una respuesta fluida pero
no sustentada. Prioridad de evaluación: 1) grounding/ausencia de alucinaciones, 2) fidelidad al material,
3) calidad pedagógica, 4) capacidad de guía, 5) correcta abstención, 6) atribución de fuente.

Devuelve SOLO un JSON con esta forma exacta (usa null en un campo si la dimensión no aplica a esta pregunta):
{
  "correctness": 0-10 o null,
  "groundedness": 0-10 o null,
  "relevance": 0-10 o null,
  "pedagogical_value": 0-10 o null,
  "guidance_quality": 0-10 o null,
  "depth": 0-10 o null,
  "clarity": 0-10 o null,
  "source_attribution": 0-10 o null,
  "hallucination": true/false,
  "hallucination_detail": "string breve o null",
  "appropriate_abstention": true/false/null,
  "over_solving": true/false,
  "superficial_copy_paste": true/false,
  "unsupported_claims": ["lista breve de afirmaciones NO sustentadas por la evidencia, o []"],
  "verdict": "pass" | "partial" | "fail",
  "rationale": "1-3 frases explicando el veredicto"
}"""


def _build_judge_prompt(item: Dict[str, Any], question: str, answer: str,
                         sources: List[Dict[str, Any]], conversation_context: Optional[str] = None) -> str:
    # IMPORTANTE: 300 caracteres era insuficiente — en un caso de control
    # positivo verificado (Fernando Díaz, universidades) la evidencia real
    # que sustenta la respuesta aparecía después del corte, y el juez la
    # marcó como alucinación inexistente. Se sube a 1800 y se reduce a los
    # 5 candidatos más relevantes para no exceder el contexto del juez.
    src_text = "\n".join(
        f"- [{s.get('source_type')}] {s.get('filename')}: {(s.get('text') or '')[:1800]}"
        for s in sources[:5]
    ) or "(sin fuentes recuperadas)"
    ctx = f"\nContexto de la conversación previa:\n{conversation_context}\n" if conversation_context else ""
    return f"""Categoría de la prueba: {item['category']}
Comportamiento esperado (definido por el diseñador de la prueba, NO es la respuesta ideal literal, es el criterio de éxito): {item['expected_behavior']}
{ctx}
Pregunta del estudiante: {question}

Respuesta real de Idania: {answer}

Evidencia que Idania tuvo disponible (fuentes recuperadas, top 6):
{src_text}

Evalúa la respuesta de Idania según el JSON pedido."""


async def _judge(item: Dict[str, Any], question: str, answer: str, sources: List[Dict[str, Any]],
                  conversation_context: Optional[str] = None) -> Dict[str, Any]:
    prompt = _build_judge_prompt(item, question, answer, sources, conversation_context)
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


# ══════════════════════════════════════════════════════════════════════
# Ejecución de una interacción (single-turn o multi-turn)
# ══════════════════════════════════════════════════════════════════════

async def _run_single_turn(item: Dict[str, Any], question: str, history: Optional[List[Dict[str, str]]] = None,
                            conversation_context: Optional[str] = None) -> Dict[str, Any]:
    collection = COLLECTIONS[item["course_id"]]
    async with _gen_sem:
        t0 = time.perf_counter()
        candidates = await rag_pipeline.retrieve_rerank_boost(
            collection_name=collection, query=question,
            dense_strategy_name=DENSE, sparse_strategy_name=SPARSE, rerank_strategy_name="none",
        )
        retrieval_ms = (time.perf_counter() - t0) * 1000
        result = await rag_pipeline.answer_query(
            collection_name=collection, query=question,
            dense_strategy_name=DENSE, sparse_strategy_name=SPARSE, rerank_strategy_name="none",
            history=history,
        )
    retrieval_metrics = _retrieval_metrics(candidates, item.get("expected_source"))
    judge = await _judge(item, question, result["answer"], result["sources"], conversation_context)
    return {
        "question": question, "answer": result["answer"], "sources": result["sources"],
        "retrieval_metrics": retrieval_metrics, "quality_metrics": judge,
        "latency_metrics": result["config_used"]["timings_ms"],
        "tokens_consumidos": result.get("tokens_consumidos"),
    }


async def run_item(item: Dict[str, Any]) -> Dict[str, Any]:
    if item.get("multi_turn"):
        turns_out = []
        history: List[Dict[str, str]] = []
        convo_so_far = []
        for turn in item["multi_turn"]:
            q = turn["question"]
            ctx = "\n".join(convo_so_far) if convo_so_far else None
            turn_item = {**item, "expected_behavior": turn["expected_behavior"]}
            out = await _run_single_turn(turn_item, q, history=list(history), conversation_context=ctx)
            turns_out.append(out)
            history.append({"role": "user", "content": q})
            history.append({"role": "assistant", "content": out["answer"]})
            convo_so_far.append(f"Estudiante: {q}")
            convo_so_far.append(f"Idania: {out['answer'][:400]}")
        # continuidad evaluada sobre el ÚLTIMO turno con el historial completo como contexto
        last = turns_out[-1]
        continuity_prompt_ctx = "\n".join(convo_so_far[:-2])
        continuity_judge = await _judge(
            {**item, "category": "K_multiturno_continuidad",
             "expected_behavior": "mantiene memoria de contexto, consistencia y coherencia pedagógica a través de los 3 turnos, sin contradecirse ni perder el hilo"},
            item["multi_turn"][-1]["question"], last["answer"], last["sources"],
            conversation_context=continuity_prompt_ctx,
        )
        return {**item, "turns": turns_out, "continuity_judge": continuity_judge, "passed": None}
    else:
        out = await _run_single_turn(item, item["question"])
        judge = out["quality_metrics"]
        passed = judge.get("verdict") == "pass" if isinstance(judge, dict) and "verdict" in judge else None
        return {**item, **out, "passed": passed}


async def main():
    print(f"Cargadas {len(DATASET)} interacciones", flush=True)
    results = []
    t_start = time.time()
    sem_outer = asyncio.Semaphore(6)

    async def _guarded(item, idx):
        async with sem_outer:
            try:
                r = await run_item(item)
            except Exception as e:
                r = {**item, "error": str(e), "passed": False}
            if (idx + 1) % 10 == 0:
                print(f"[{idx+1}/{len(DATASET)}] completadas ({time.time()-t_start:.0f}s)", flush=True)
            return r

    results = await asyncio.gather(*[_guarded(item, i) for i, item in enumerate(DATASET)])

    with open(os.path.join(EVAL_DIR, "evaluation_200_results.json"), "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)

    # CSV plano (una fila por turno, incluidos multi-turno)
    rows = []
    for r in results:
        if r.get("turns"):
            for ti, t in enumerate(r["turns"]):
                rows.append({
                    "id": f"{r['id']}-T{ti+1}", "course_id": r["course_id"], "category": r["category"],
                    "week": r.get("week"), "question": t["question"], "answer": t["answer"][:500],
                    "verdict": (t["quality_metrics"] or {}).get("verdict"),
                    "hallucination": (t["quality_metrics"] or {}).get("hallucination"),
                    "groundedness": (t["quality_metrics"] or {}).get("groundedness"),
                    "pedagogical_value": (t["quality_metrics"] or {}).get("pedagogical_value"),
                    "total_ms": (t.get("latency_metrics") or {}).get("total_ms"),
                })
        else:
            rows.append({
                "id": r["id"], "course_id": r["course_id"], "category": r["category"], "week": r.get("week"),
                "question": r.get("question"), "answer": (r.get("answer") or "")[:500],
                "verdict": (r.get("quality_metrics") or {}).get("verdict"),
                "hallucination": (r.get("quality_metrics") or {}).get("hallucination"),
                "groundedness": (r.get("quality_metrics") or {}).get("groundedness"),
                "pedagogical_value": (r.get("quality_metrics") or {}).get("pedagogical_value"),
                "total_ms": (r.get("latency_metrics") or {}).get("total_ms"),
            })
    with open("evaluation_200_results.csv", "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)

    # ── Resumen agregado ──
    summary: Dict[str, Any] = {"total_interactions": len(results), "total_turns": len(rows)}

    all_latencies = [r["total_ms"] for r in rows if r.get("total_ms") is not None]
    def _pct(vals, p):
        if not vals:
            return None
        s = sorted(vals)
        k = (len(s) - 1) * p / 100
        f_, c_ = math.floor(k), math.ceil(k)
        if f_ == c_:
            return s[int(k)]
        return s[f_] + (s[c_] - s[f_]) * (k - f_)
    summary["latency_ms"] = {
        "mean": sum(all_latencies) / len(all_latencies) if all_latencies else None,
        "p50": _pct(all_latencies, 50), "p90": _pct(all_latencies, 90),
        "p95": _pct(all_latencies, 95), "p99": _pct(all_latencies, 99),
        "min": min(all_latencies) if all_latencies else None, "max": max(all_latencies) if all_latencies else None,
    }

    # retrieval agregado (solo single-turn con expected_source)
    retr = [r["retrieval_metrics"] for r in results if not r.get("turns") and r.get("retrieval_metrics", {}).get("applicable")]
    if retr:
        n = len(retr)
        summary["retrieval"] = {
            "n_applicable": n,
            "recall_at_5": sum(1 for x in retr if x["recall_at_5"]) / n,
            "recall_at_10": sum(1 for x in retr if x["recall_at_10"]) / n,
            "mrr": sum(x["reciprocal_rank"] for x in retr) / n,
            "ndcg_at_10": sum(x["ndcg_at_10"] for x in retr) / n,
            "hit_rate": sum(1 for x in retr if x["hit"]) / n,
        }

    # pedagógico/grounding agregado (todos los turnos con judge válido)
    judged = [row for row in rows if row.get("verdict") is not None]
    dims = ["groundedness", "pedagogical_value"]
    summary["quality"] = {
        "n_judged": len(judged), "n_total_turns": len(rows),
        "pass_rate": sum(1 for r in judged if r["verdict"] == "pass") / len(judged) if judged else None,
        "hallucination_rate": sum(1 for r in judged if r.get("hallucination") is True) / len(judged) if judged else None,
        "avg_groundedness": sum(r["groundedness"] for r in judged if r.get("groundedness") is not None) / max(1, sum(1 for r in judged if r.get("groundedness") is not None)),
        "avg_pedagogical_value": sum(r["pedagogical_value"] for r in judged if r.get("pedagogical_value") is not None) / max(1, sum(1 for r in judged if r.get("pedagogical_value") is not None)),
    }

    # matriz de cobertura: curso x semana x categoria
    coverage = defaultdict(lambda: {"preguntas": 0, "pass": 0, "fail": 0, "partial": 0, "sin_judge": 0})
    for r in results:
        key = (r["course_id"], r.get("week") or "General", r["category"])
        c = coverage[key]
        if r.get("turns"):
            for t in r["turns"]:
                c["preguntas"] += 1
                v = (t["quality_metrics"] or {}).get("verdict")
                if v == "pass": c["pass"] += 1
                elif v == "partial": c["partial"] += 1
                elif v == "fail": c["fail"] += 1
                else: c["sin_judge"] += 1
        else:
            c["preguntas"] += 1
            v = (r.get("quality_metrics") or {}).get("verdict")
            if v == "pass": c["pass"] += 1
            elif v == "partial": c["partial"] += 1
            elif v == "fail": c["fail"] += 1
            else: c["sin_judge"] += 1
    summary["coverage_matrix"] = [
        {"course_id": k[0], "week": k[1], "category": k[2], **v} for k, v in sorted(coverage.items())
    ]

    # por categoria
    by_cat = defaultdict(lambda: {"n": 0, "pass": 0})
    for row in rows:
        by_cat[row["category"]]["n"] += 1
        if row.get("verdict") == "pass":
            by_cat[row["category"]]["pass"] += 1
    summary["by_category"] = {k: {**v, "pass_rate": v["pass"] / v["n"] if v["n"] else None} for k, v in by_cat.items()}

    # por curso
    by_course = defaultdict(lambda: {"n": 0, "pass": 0})
    for row in rows:
        by_course[row["course_id"]]["n"] += 1
        if row.get("verdict") == "pass":
            by_course[row["course_id"]]["pass"] += 1
    summary["by_course"] = {k: {**v, "pass_rate": v["pass"] / v["n"] if v["n"] else None} for k, v in by_course.items()}

    with open(os.path.join(EVAL_DIR, "evaluation_200_summary.json"), "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)

    print(f"\n=== RESUMEN ===")
    print(json.dumps({k: v for k, v in summary.items() if k != "coverage_matrix"}, ensure_ascii=False, indent=2))
    print(f"\nTerminado en {time.time()-t_start:.0f}s")
    print("Guardado: evaluation_200_results.json / evaluation_200_results.csv / evaluation_200_summary.json")


if __name__ == "__main__":
    asyncio.run(main())
