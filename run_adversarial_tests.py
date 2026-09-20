"""
run_adversarial_tests.py
=========================
Corre la batería de preguntas adversariales (eval_dataset_curso_1472_adversarial.json)
contra Curso_1472 (base) y Curso_1472_experiment_v2 (chunking estructural +
contexto determinístico) usando el pipeline real completo (answer_query),
para detectar riesgo de alucinación en ambas versiones y comparar.

Uso: venv/Scripts/python.exe run_adversarial_tests.py
"""
import asyncio
import json

import rag_pipeline

DENSE = "openai/text-embedding-3-small"
SPARSE = "bm25"
COLLECTIONS = ["Curso_1472", "Curso_1472_experiment_v2"]

with open("eval_dataset_curso_1472_adversarial.json", encoding="utf-8") as f:
    DATASET = json.load(f)


async def ask(collection, question):
    result = await rag_pipeline.answer_query(
        collection_name=collection, query=question,
        dense_strategy_name=DENSE, sparse_strategy_name=SPARSE,
        rerank_strategy_name="none",
    )
    return result["answer"], [s.get("filename") for s in result["sources"]]


async def main():
    all_results = []
    for item in DATASET:
        print(f"\n{'='*90}\n[{item['id']}] ({item['risk']}) {item['question']}\n{'='*90}")
        print(f"nota: {item['note']}\n")
        row = {**item, "answers": {}}
        for coll in COLLECTIONS:
            answer, sources = await ask(coll, item["question"])
            row["answers"][coll] = {"answer": answer, "sources": sources}
            print(f"--- {coll} ---")
            print(answer)
            print("fuentes:", sources)
            print()
        all_results.append(row)

    with open("adversarial_test_results.json", "w", encoding="utf-8") as f:
        json.dump(all_results, f, ensure_ascii=False, indent=2)
    print("\nGuardado: adversarial_test_results.json")


asyncio.run(main())
