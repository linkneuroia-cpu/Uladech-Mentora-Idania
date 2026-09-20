"""
run_full_eval_comparison.py
=============================
Evaluación amplia: corre el dataset de 15 preguntas
(eval_dataset_curso_1472.json) y el de 10 adversariales
(eval_dataset_curso_1472_adversarial.json) contra Curso_1472 (base, sin
tocar) y Curso_1472_experiment_v2 (chunking estructural + contexto
determinístico solo en tabla/fila + tablas-plantilla sin partir en filas
+ fix de source_type) — reporta métricas lado a lado para decidir con
datos, no por caso aislado.
"""
import asyncio
import json

import rag_pipeline
from settings import settings

DENSE = "openai/text-embedding-3-small"
SPARSE = "bm25"
COLLECTIONS = ["Curso_1472", "Curso_1472_experiment_v2"]

with open("eval_dataset_curso_1472.json", encoding="utf-8") as f:
    MAIN_DATASET = json.load(f)
with open("eval_dataset_curso_1472_adversarial.json", encoding="utf-8") as f:
    ADV_DATASET = json.load(f)


def _norm(s):
    return (s or "").strip().lower()


async def eval_retrieval(collection, item):
    candidates = await rag_pipeline.hybrid_retrieve(
        collection_name=collection, query=item["question"],
        dense_strategy_name=DENSE, sparse_strategy_name=SPARSE, top_k=settings.RERANK_TOP_K,
    )
    expected = item.get("expected_source")
    if expected is None:
        return {"applicable": False}
    expected_norm = _norm(expected)
    alt_norm = [_norm(s) for s in item.get("acceptable_alt_sources", [])]
    valid = {expected_norm, *alt_norm}
    rank = None
    for i, c in enumerate(candidates):
        if _norm(c["payload"].get("filename")) in valid:
            rank = i + 1
            break
    return {
        "applicable": True, "rank_in_pool": rank,
        "recall_at_5": rank is not None and rank <= 5,
        "recall_at_10": rank is not None and rank <= 10,
        "reciprocal_rank": (1.0 / rank) if rank else 0.0,
    }


async def eval_generation(collection, item):
    result = await rag_pipeline.answer_query(
        collection_name=collection, query=item["question"],
        dense_strategy_name=DENSE, sparse_strategy_name=SPARSE, rerank_strategy_name="none",
    )
    answer_lower = _norm(result["answer"])
    sources = [_norm(s.get("filename")) for s in result["sources"]]

    if item.get("expected_source") is None:
        abstained = (
            len(result["sources"]) == 0
            or any(p in answer_lower for p in (
                "no tengo información", "no encontré información", "no cuento con",
                "no dispongo de información", "no se encontró información",
                "no se menciona información", "no se encuentra información",
            ))
        )
        return {"type": "abstention", "correct": abstained, "answer": result["answer"], "sources": sources}

    keywords = item.get("expected_answer_contains") or []
    hits = [kw for kw in keywords if _norm(kw) in answer_lower]
    expected_norm = _norm(item["expected_source"])
    alt_norm = [_norm(s) for s in item.get("acceptable_alt_sources", [])]
    cited_expected = any(s in {expected_norm, *alt_norm} for s in sources)
    return {
        "type": "factual",
        "keyword_coverage": len(hits) / len(keywords) if keywords else None,
        "cited_expected_source": cited_expected,
        "answer": result["answer"], "sources": sources,
    }


async def eval_main_dataset(collection):
    retrieval_results, generation_results = [], []
    for item in MAIN_DATASET:
        r = await eval_retrieval(collection, item)
        g = await eval_generation(collection, item)
        retrieval_results.append(r)
        generation_results.append(g)

    applicable = [r for r in retrieval_results if r["applicable"]]
    n = len(applicable)
    recall_5 = sum(1 for x in applicable if x["recall_at_5"]) / n
    recall_10 = sum(1 for x in applicable if x["recall_at_10"]) / n
    mrr = sum(x["reciprocal_rank"] for x in applicable) / n

    factuals = [g for g in generation_results if g["type"] == "factual"]
    avg_kw = sum(x["keyword_coverage"] for x in factuals if x["keyword_coverage"] is not None) / len(factuals)
    cited_rate = sum(1 for x in factuals if x["cited_expected_source"]) / len(factuals)

    abstentions = [g for g in generation_results if g["type"] == "abstention"]
    abstain_acc = sum(1 for x in abstentions if x["correct"]) / len(abstentions) if abstentions else None

    return {
        "recall_at_5": recall_5, "recall_at_10": recall_10, "mrr": mrr,
        "avg_keyword_coverage": avg_kw, "cited_expected_rate": cited_rate,
        "abstention_accuracy": abstain_acc,
        "per_question": [{**item, "retrieval": r, "generation": g}
                          for item, r, g in zip(MAIN_DATASET, retrieval_results, generation_results)],
    }


async def eval_adversarial_dataset(collection):
    rows = []
    hallucination_flags = []
    for item in ADV_DATASET:
        answer, sources = None, None
        result = await rag_pipeline.answer_query(
            collection_name=collection, query=item["question"],
            dense_strategy_name=DENSE, sparse_strategy_name=SPARSE, rerank_strategy_name="none",
        )
        answer = result["answer"]
        sources = [s.get("filename") for s in result["sources"]]
        rows.append({**item, "answer": answer, "sources": sources})
    return rows


async def main():
    report = {}
    for collection in COLLECTIONS:
        print(f"\n{'#'*90}\n# {collection}\n{'#'*90}")
        main_eval = await eval_main_dataset(collection)
        adv_eval = await eval_adversarial_dataset(collection)
        report[collection] = {"main": main_eval, "adversarial": adv_eval}

        print(f"\n--- Dataset principal (15 preguntas) ---")
        print(f"  Recall@5  = {main_eval['recall_at_5']:.1%}")
        print(f"  Recall@10 = {main_eval['recall_at_10']:.1%}")
        print(f"  MRR       = {main_eval['mrr']:.3f}")
        print(f"  Cobertura de keywords  = {main_eval['avg_keyword_coverage']:.1%}")
        print(f"  Cita fuente esperada   = {main_eval['cited_expected_rate']:.1%}")
        if main_eval["abstention_accuracy"] is not None:
            print(f"  Abstención correcta    = {main_eval['abstention_accuracy']:.1%}")

        print(f"\n--- Dataset adversarial (10 preguntas) — respuestas completas ---")
        for row in adv_eval:
            print(f"\n[{row['id']}] {row['question']}")
            print(f"  -> {row['answer'][:200]}")

    with open("full_eval_comparison_results.json", "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)

    print(f"\n\n{'='*90}\nCOMPARACIÓN FINAL\n{'='*90}")
    print(f"{'Métrica':30} {'BASE':>12} {'EXPERIMENTAL':>15}")
    b, e = report["Curso_1472"]["main"], report["Curso_1472_experiment_v2"]["main"]
    for key, label in [("recall_at_5", "Recall@5"), ("recall_at_10", "Recall@10"), ("mrr", "MRR"),
                        ("avg_keyword_coverage", "Cobertura keywords"), ("cited_expected_rate", "Cita fuente esperada"),
                        ("abstention_accuracy", "Abstención correcta")]:
        bv, ev = b.get(key), e.get(key)
        bv_s = f"{bv:.1%}" if isinstance(bv, float) and key != "mrr" else (f"{bv:.3f}" if bv is not None else "—")
        ev_s = f"{ev:.1%}" if isinstance(ev, float) and key != "mrr" else (f"{ev:.3f}" if ev is not None else "—")
        print(f"{label:30} {bv_s:>12} {ev_s:>15}")

    print("\nGuardado: full_eval_comparison_results.json")


asyncio.run(main())
