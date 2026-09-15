"""Estrategias de reranking: reordenan el Top-K recuperado por Qdrant a un
Top-N hiperrelevante. `apply_source_boost` aplica el multiplicador de
prioridad a `source_type == "curso_propio"` después del reranking, para
que el material propio del curso siempre gane frente a bibliografía
igualmente relevante según el cross-encoder/API.
"""
import re
from typing import Any, Dict, List, Optional

from strategies.base import RerankStrategy

_cross_encoder_cache: Dict[str, Any] = {}


def _get_cross_encoder(model_name: str):
    if model_name not in _cross_encoder_cache:
        import torch
        from sentence_transformers import CrossEncoder

        model = CrossEncoder(model_name)
        # El parámetro device=... del constructor NO mueve el modelo en esta
        # combinación de versiones (sentence-transformers 3.1.1 +
        # transformers 4.57.6): queda en CPU aunque se pida "cuda" y haya
        # GPU disponible. Se fuerza moviendo el modelo interno con .to() —
        # medido en vivo: 27.8s -> 0.7s para 30 candidatos (~40x). Este es
        # el modelo más pesado del pipeline (568M params, hasta 50
        # candidatos por consulta) — el que más se beneficia de GPU.
        if torch.cuda.is_available():
            model.model.to("cuda")
        _cross_encoder_cache[model_name] = model
    return _cross_encoder_cache[model_name]


class BGERerankStrategy(RerankStrategy):
    """BGE-Reranker (cross-encoder local, BAAI/bge-reranker-v2-m3). Gratis,
    sin API, pero requiere descargar el modelo (~1-2 GB) la primera vez."""

    _model_name = "BAAI/bge-reranker-v2-m3"

    async def rerank(
        self, query: str, candidates: List[Dict[str, Any]], top_n: int
    ) -> List[Dict[str, Any]]:
        import asyncio

        if not candidates:
            return []

        loop = asyncio.get_event_loop()

        def _call():
            model = _get_cross_encoder(self._model_name)
            pairs = [(query, c["text"]) for c in candidates]
            scores = model.predict(pairs)
            for c, score in zip(candidates, scores):
                c["rerank_score"] = float(score)
            ranked = sorted(candidates, key=lambda c: c["rerank_score"], reverse=True)
            return ranked[:top_n]

        return await loop.run_in_executor(None, _call)


class NoOpRerankStrategy(RerankStrategy):
    """Sin reranking real: conserva el orden que ya trajo Qdrant (fusión
    RRF de dense+sparse) y solo recorta a `top_n`. Gratis e instantáneo —
    pensado para producción sin GPU, donde el cross-encoder local
    (BGERerankStrategy) cuesta ~28s por consulta en CPU (ver el comentario
    de _get_cross_encoder más abajo) y era, medido en vivo, la causa
    dominante del tiempo de respuesta. `rerank_score` se llena con el
    score de Qdrant tal cual, para que el resto del pipeline (filtro
    MIN_SOURCE_RELEVANCE, boost de fuente, score mostrado al usuario) siga
    funcionando sin distinguir si hubo reranking real o no."""

    async def rerank(
        self, query: str, candidates: List[Dict[str, Any]], top_n: int
    ) -> List[Dict[str, Any]]:
        for c in candidates:
            c["rerank_score"] = c.get("score")
        return candidates[:top_n]


class CohereRerankStrategy(RerankStrategy):
    """Cohere Rerank v3 — reranking en la nube de alta calidad."""

    _model_name = "rerank-v3.5"

    def __init__(self):
        from settings import settings

        if not settings.COHERE_API_KEY:
            raise ValueError(
                "COHERE_API_KEY no está configurado. Requerido para la "
                "estrategia de reranking 'cohere'."
            )
        import cohere

        self._client = cohere.ClientV2(api_key=settings.COHERE_API_KEY)

    async def rerank(
        self, query: str, candidates: List[Dict[str, Any]], top_n: int
    ) -> List[Dict[str, Any]]:
        import asyncio

        if not candidates:
            return []

        loop = asyncio.get_event_loop()

        def _call():
            documents = [c["text"] for c in candidates]
            response = self._client.rerank(
                model=self._model_name,
                query=query,
                documents=documents,
                top_n=min(top_n, len(documents)),
            )
            ranked = []
            for result in response.results:
                candidate = candidates[result.index]
                candidate["rerank_score"] = float(result.relevance_score)
                ranked.append(candidate)
            return ranked

        return await loop.run_in_executor(None, _call)


def apply_source_boost(
    candidates: List[Dict[str, Any]],
    boost_multiplier: float,
    top_n: int,
) -> List[Dict[str, Any]]:
    """Multiplica `rerank_score` por `boost_multiplier` para los candidatos
    con `payload.source_type == "curso_propio"`, reordena de mayor a menor,
    y trunca a `top_n`."""
    for c in candidates:
        payload = c.get("payload", {})
        base_score = c.get("rerank_score", 0.0)
        if payload.get("source_type") == "curso_propio":
            c["boosted_score"] = base_score * boost_multiplier
        else:
            c["boosted_score"] = base_score

    ranked = sorted(candidates, key=lambda c: c["boosted_score"], reverse=True)
    return ranked[:top_n]


# Semana 02 : ... / Semana  03 : ... (doble espacio, visto en datos reales) /
# Senana 04 : ... (typo real visto en un curso de Moodle) / SESION
# APRENDIZAJE 2.docx — se busca el NÚMERO cerca de "semana"/"sesión"/
# "aprendizaje", tolerando el typo "Senana" (no se ancla a la ortografía
# de la palabra, solo a que haya un número de 1-2 dígitos ahí cerca).
_WEEK_NUM_RE = re.compile(
    r"(?:sem[aá]na|sen[aá]na|sesi[oó]n|aprendizaje|week)\s*[°ºn]?\.?\s*0*(\d{1,2})\b",
    re.IGNORECASE,
)


def _extract_week_number(text: str) -> Optional[int]:
    m = _WEEK_NUM_RE.search(text or "")
    return int(m.group(1)) if m else None


def _extract_all_week_numbers(text: str) -> List[int]:
    """Igual que _extract_week_number pero devuelve TODOS los números de
    semana/sesión distintos mencionados (no solo el primero) — necesario
    para preguntas que comparan/relacionan 2+ semanas explícitas (ver
    apply_week_boost)."""
    seen: List[int] = []
    for m in _WEEK_NUM_RE.finditer(text or ""):
        n = int(m.group(1))
        if n not in seen:
            seen.append(n)
    return seen


def apply_week_boost(candidates: List[Dict[str, Any]], query: str) -> List[Dict[str, Any]]:
    """Si la pregunta menciona uno o más números de semana/sesión puntuales
    ("¿de qué trata la tarea de la semana 2?", "compara la semana 11 con
    la semana 12"), sube al frente los candidatos cuyo section_name o
    filename mencionen CUALQUIERA de esos números.

    Por qué hace falta: sin reranker real (ver NoOpRerankStrategy — se
    desactivó por el costo en CPU sin GPU, ~28s/consulta), la única señal
    de orden es el score crudo de Qdrant (fusión RRF dense+sparse). Para
    preguntas "navegacionales" tipo "¿de qué trata la semana N?" eso no
    alcanza: las sesiones de un curso comparten casi el mismo boilerplate
    estructural (DATOS GENERALES, DESARROLLO APLICACIÓN, Indicaciones para
    el desarrollo, Criterios de evaluación...) semana a semana, así que el
    embedding de la pregunta puntúa parecido a CUALQUIER semana — se vio
    en vivo un caso real donde la semana correcta quedaba en el puesto
    #11, fuera del top_n que llega al LLM, y la respuesta terminaba
    describiendo el tema de otra semana. El número de semana/sesión, en
    cambio, es una señal exacta que el texto plano ya trae — no hace
    falta ningún modelo para aprovecharla.

    No afecta ninguna pregunta que no mencione una semana/sesión (retorna
    la lista igual, sin tocar el orden). Preguntas de comparación/relación
    entre 2+ semanas ("¿cómo se relaciona la semana 9 con la semana 10?")
    boostean candidatos de AMBAS, no solo la primera mencionada — antes
    solo se tomaba _extract_week_number (la primera), y la segunda semana
    quedaba sin ayuda ninguna, lo que producía respuestas incompletas
    ("no tengo información sobre la semana 12") en preguntas de
    comparación aunque la evidencia de esa semana sí existiera en la
    colección — encontrado con datos reales en la evaluación de 218
    preguntas (categoría F, comparación/integración)."""
    query_weeks = _extract_all_week_numbers(query)
    if not query_weeks:
        return candidates

    max_score = max((c.get("boosted_score", 0.0) for c in candidates), default=0.0)
    for c in candidates:
        payload = c.get("payload", {})
        candidate_weeks = set(_extract_all_week_numbers(payload.get("section_name") or "")) | set(
            _extract_all_week_numbers(payload.get("filename") or "")
        )
        if candidate_weeks & set(query_weeks):
            # Por encima del máximo existente — garantiza que quede
            # primero sin importar cuán bajo haya puntuado el fusion
            # RRF/dense+sparse original.
            c["boosted_score"] = max_score + 1.0 + c.get("boosted_score", 0.0)

    return sorted(candidates, key=lambda c: c["boosted_score"], reverse=True)


# El router de metadata basado en patrones regex curados a mano (Fase 3.7)
# fue retirado tras comprobar con evaluación real que el Índice de Campos
# Estructurados (campos_estructurados.py) iguala sus métricas sin ninguna
# regla específica de campo/curso/documento — ver informe
# "Indice de Campos Estructurados - Informe Final.docx" en Data prueba/.
