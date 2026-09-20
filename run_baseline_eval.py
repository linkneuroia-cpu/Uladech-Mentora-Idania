"""
run_baseline_eval.py
=====================
Fase 4: mide Recall@5/Recall@10/MRR (retrieval) y groundedness/abstención
(generación) del pipeline ACTUAL (sin ningún cambio de Fase 3) contra
eval_dataset_curso_1472.json, corriendo contra la copia de desarrollo de
Curso_1472 recién sincronizada 1:1 desde producción (mismo document_hash,
mismo contenido real). Sirve como línea base para comparar cualquier
cambio de Fase 3 (chunking estructural, contexto determinístico,
Contextual Retrieval selectivo, etc.) sobre la MISMA colección de origen.

Uso: venv/Scripts/python.exe run_baseline_eval.py
"""
import asyncio
import json

import rag_pipeline
from settings import settings

COLLECTION = "Curso_1472"
DENSE = "openai/text-embedding-3-small"
SPARSE = "bm25"

with open("eval_dataset_curso_1472.json", encoding="utf-8") as f:
    DATASET = json.load(f)


def _norm(s):
    return (s or "").strip().lower()


async def eval_retrieval(item):
    """Recall@5/@10 y rank del expected_source dentro del pool hybrid_retrieve
    (mismo top_k=RERANK_TOP_K=50 que usa producción hoy, mismo prefetch_limit=100
    fijo — se mide el comportamiento REAL actual, no uno idealizado)."""
    candidates = await rag_pipeline.hybrid_retrieve(
        collection_name=COLLECTION,
        query=item["question"],
        dense_strategy_name=DENSE,
        sparse_strategy_name=SPARSE,
        top_k=settings.RERANK_TOP_K,
    )
    expected = item.get("expected_source")
    if expected is None:
        return {"applicable": False}

    expected_norm = _norm(expected)
    alt_norm = [_norm(s) for s in item.get("acceptable_alt_sources", [])]
    valid = {expected_norm, *alt_norm}

    rank = None
    for i, c in enumerate(candidates):
        fname = _norm(c["payload"].get("filename"))
        if fname in valid:
            rank = i + 1
            break

    return {
        "applicable": True,
        "rank_in_pool": rank,
        "recall_at_5": rank is not None and rank <= 5,
        "recall_at_10": rank is not None and rank <= 10,
        "reciprocal_rank": (1.0 / rank) if rank else 0.0,
        "pool_size": len(candidates),
    }


async def eval_generation(item):
    """Corre el pipeline completo (retrieve+rerank(none)+boost+generación) tal
    cual lo usa producción, y revisa si la respuesta final contiene las
    palabras clave esperadas, o si se abstiene correctamente cuando
    corresponde (expected_source=None)."""
    result = await rag_pipeline.answer_query(
        collection_name=COLLECTION,
        query=item["question"],
        dense_strategy_name=DENSE,
        sparse_strategy_name=SPARSE,
        rerank_strategy_name="none",
    )
    answer_lower = _norm(result["answer"])
    sources = [_norm(s.get("filename")) for s in result["sources"]]

    if item.get("expected_source") is None:
        # SIN_RESPUESTA: éxito = el sistema NO afirma un dato con nombre/
        # cifra inventado. Heurística barata: si el bloque de fuentes quedó
        # vacío O la respuesta contiene una frase de abstención típica.
        abstained = (
            len(result["sources"]) == 0
            or any(
                phrase in answer_lower
                for phrase in ("no tengo información", "no encontré información",
                                "no cuento con", "no dispongo de información")
            )
        )
        return {
            "type": "abstention",
            "correct": abstained,
            "answer_preview": result["answer"][:200],
            "sources_returned": sources,
        }

    keywords = item.get("expected_answer_contains") or []
    hits = [kw for kw in keywords if _norm(kw) in answer_lower]
    expected_norm = _norm(item["expected_source"])
    alt_norm = [_norm(s) for s in item.get("acceptable_alt_sources", [])]
    cited_expected = any(s in {expected_norm, *alt_norm} for s in sources)

    return {
        "type": "factual",
        "keywords_expected": keywords,
        "keywords_found": hits,
        "keyword_coverage": len(hits) / len(keywords) if keywords else None,
        "cited_expected_source": cited_expected,
        "answer_preview": result["answer"][:250],
        "sources_returned": sources,
    }


async def main():
    print(f"Coleccion: {COLLECTION} | dense={DENSE} | sparse={SPARSE} | "
          f"RERANK_TOP_K={settings.RERANK_TOP_K} | RERANK_TOP_N={settings.RERANK_TOP_N}\n")

    retrieval_results = []
    generation_results = []

    for item in DATASET:
        r = await eval_retrieval(item)
        g = await eval_generation(item)
        retrieval_results.append({**item, "retrieval": r})
        generation_results.append({**item, "generation": g})

        tag = f"[{item['id']:2d}][{item['query_type']}]"
        if r["applicable"]:
            rank_str = r["rank_in_pool"] if r["rank_in_pool"] else "FUERA del pool"
            print(f"{tag} rank_pool={rank_str} R@5={r['recall_at_5']} R@10={r['recall_at_10']}  | {item['question']}")
        else:
            ok = g["correct"]
            print(f"{tag} abstención_correcta={ok}  | {item['question']}")

    applicable = [r["retrieval"] for r in retrieval_results if r["retrieval"]["applicable"]]
    n = len(applicable)
    recall_5 = sum(1 for x in applicable if x["recall_at_5"]) / n
    recall_10 = sum(1 for x in applicable if x["recall_at_10"]) / n
    mrr = sum(x["reciprocal_rank"] for x in applicable) / n

    abstentions = [g["generation"] for g in generation_results if g["generation"]["type"] == "abstention"]
    abstention_acc = (
        sum(1 for x in abstentions if x["correct"]) / len(abstentions) if abstentions else None
    )

    factuals = [g["generation"] for g in generation_results if g["generation"]["type"] == "factual"]
    avg_keyword_coverage = sum(x["keyword_coverage"] for x in factuals if x["keyword_coverage"] is not None) / len(factuals)
    cited_expected_rate = sum(1 for x in factuals if x["cited_expected_source"]) / len(factuals)

    print("\n" + "=" * 70)
    print("BASELINE — pipeline actual (sin cambios de Fase 3), Curso_1472")
    print("=" * 70)
    print(f"Retrieval  (n={n} preguntas con expected_source):")
    print(f"  Recall@5  = {recall_5:.2%}")
    print(f"  Recall@10 = {recall_10:.2%}")
    print(f"  MRR       = {mrr:.3f}")
    print(f"Generación (n={len(factuals)} preguntas factuales):")
    print(f"  Cobertura promedio de keywords esperadas = {avg_keyword_coverage:.2%}")
    print(f"  % que citó la fuente esperada como source  = {cited_expected_rate:.2%}")
    if abstention_acc is not None:
        print(f"Abstención (n={len(abstentions)} preguntas sin respuesta en el material):")
        print(f"  Abstención correcta = {abstention_acc:.2%}")

    with open("baseline_eval_results.json", "w", encoding="utf-8") as f:
        json.dump(
            {
                "collection": COLLECTION,
                "config": {"dense": DENSE, "sparse": SPARSE, "rerank_top_k": settings.RERANK_TOP_K,
                           "rerank_top_n": settings.RERANK_TOP_N, "prefetch_limit": 100},
                "summary": {
                    "recall_at_5": recall_5, "recall_at_10": recall_10, "mrr": mrr,
                    "avg_keyword_coverage": avg_keyword_coverage,
                    "cited_expected_rate": cited_expected_rate,
                    "abstention_accuracy": abstention_acc,
                },
                "per_question_retrieval": retrieval_results,
                "per_question_generation": generation_results,
            },
            f, ensure_ascii=False, indent=2,
        )
    print("\nGuardado: baseline_eval_results.json")


asyncio.run(main())
