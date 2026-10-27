"""
tests/test_funcionalidades.py
=============================
Las funcionalidades (Notas / Documentos / Navegabilidad) y las exclusiones de
documentos se aplican en el router y en el pipeline RAG. Estas pruebas no
tocan Moodle ni Qdrant: mockean las fuentes externas.
"""
import asyncio

import rag_pipeline
import query_router


def test_notas_deshabilitadas_devuelve_mensaje_fijo():
    r = query_router.route(
        "Curso_1472", 1472, "¿cuál es mi nota?",
        user_id=1, funcionalidades={"notas": False, "navegacion": True},
    )
    assert r is not None
    texto = r["payload"]["text"].lower()
    assert "no está habilitada" in texto and "notas" in texto


def test_navegacion_deshabilitada_devuelve_mensaje_fijo():
    r = query_router.route(
        "Curso_1472", 1472, "¿en qué semana estamos?",
        user_id=1, funcionalidades={"notas": True, "navegacion": False},
    )
    assert r is not None
    texto = r["payload"]["text"].lower()
    assert "no está habilitada" in texto and "navegación" in texto


def test_sin_funcionalidades_no_cambia_el_comportamiento():
    # Query que no matchea ningún intent determinista: sigue siendo None
    # con y sin funcionalidades (el caller decide).
    assert query_router.route("Curso_x", None, "hola, ¿cómo estás?") is None
    assert query_router.route(
        "Curso_x", None, "hola, ¿cómo estás?", funcionalidades=None
    ) is None


def test_documentos_deshabilitados_no_llama_al_retrieval(monkeypatch):
    async def prohibido(**kwargs):
        raise AssertionError("retrieve_rerank_boost no debe llamarse con documentos=False")

    monkeypatch.setattr(rag_pipeline, "retrieve_rerank_boost", prohibido)

    res = asyncio.run(
        rag_pipeline.answer_query(
            collection_name="Curso_x",
            query="¿de qué trata el curso?",
            funcionalidades={"notas": True, "documentos": False, "navegacion": True},
        )
    )
    assert "no está configurado" in res["answer"].lower()
    assert res["sources"] == []


def test_excluded_filenames_llega_como_query_filter(monkeypatch):
    class Dense:
        async def embed(self, texts, is_query=False):
            return [[0.1, 0.2]]

    class Sparse:
        async def embed(self, texts):
            return [{"indices": [1], "values": [1.0]}]

    monkeypatch.setattr(rag_pipeline.strategy_registry, "get_dense_strategy", lambda name: Dense())
    monkeypatch.setattr(rag_pipeline.strategy_registry, "get_sparse_strategy", lambda name: Sparse())

    capturado = {}

    class Admin:
        def hybrid_search(self, **kwargs):
            capturado.update(kwargs)
            return []

    monkeypatch.setattr(rag_pipeline, "get_qdrant_admin", lambda: Admin())

    asyncio.run(
        rag_pipeline.hybrid_retrieve(
            collection_name="Curso_x", query="q", excluded_filenames=["x.pdf"]
        )
    )

    qf = capturado.get("query_filter")
    assert qf is not None
    assert "x.pdf" in str(qf)


def test_sin_exclusiones_no_se_manda_filtro(monkeypatch):
    class Dense:
        async def embed(self, texts, is_query=False):
            return [[0.1]]

    class Sparse:
        async def embed(self, texts):
            return [{"indices": [1], "values": [1.0]}]

    monkeypatch.setattr(rag_pipeline.strategy_registry, "get_dense_strategy", lambda name: Dense())
    monkeypatch.setattr(rag_pipeline.strategy_registry, "get_sparse_strategy", lambda name: Sparse())

    capturado = {}

    class Admin:
        def hybrid_search(self, **kwargs):
            capturado.update(kwargs)
            return []

    monkeypatch.setattr(rag_pipeline, "get_qdrant_admin", lambda: Admin())

    asyncio.run(rag_pipeline.hybrid_retrieve(collection_name="Curso_x", query="q"))
    assert "query_filter" not in capturado
