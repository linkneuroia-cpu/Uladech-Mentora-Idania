"""
migracion.py
=============
Job de re-sincronización de una colección contra su curso de Moodle:
detecta archivos nuevos/modificados/eliminados (ver ledger.py, que deja la
cola de trabajo lista pero — a propósito — no se auto-ejecuta) y vectoriza
lo pendiente reutilizando AutoCore (core.py), sin duplicar lógica de
descarga/vectorización.

Dos formas de disparar esto:
  · Manual: POST /api/collections/{name}/migracion/ejecutar (app.py).
  · Automática: migracion_scheduler_loop(), una tarea de fondo arrancada
    en el lifespan de la app (app.py) que revisa cada ~60s qué colecciones
    en modo "periodica" ya vencieron su intervalo (rds.list_colecciones_periodicas).
"""
import asyncio
import json
from typing import Any, Dict

import db
import ledger
import rds
from settings import settings


async def ejecutar_migracion(coleccion: Dict[str, Any]) -> Dict[str, Any]:
    """Corre la migración de UNA colección. Nunca lanza — atrapa cualquier
    excepción y la deja registrada en `migracion_estado`/`migracion_ultimo_resultado`,
    para que el scheduler pueda seguir con la siguiente colección aunque
    esta falle."""
    from core import AutoCore  # import diferido: evita import circular (core importa ledger)

    name = coleccion["qdrant_collection_name"]
    curid = coleccion["moodle_courseid"]
    await db.run(rds.set_migracion_status, name, "corriendo", None)
    try:
        sync = await db.run(ledger.sync_curso_archivos, name, curid)
        cola = sync["cola"]
        if not cola:
            resultado = f"Sin cambios ({json.dumps(sync['resumen'], ensure_ascii=False)})"
            await db.run(rds.set_migracion_status, name, "ok", resultado)
            return {"collection": name, "status": "ok", "detail": resultado}

        model_name = await _resolve_model_name(name)
        filenames = [row["filename"] for row in cola]
        job_info = await AutoCore.download_and_validate(curid, name, filenames, model_name)
        await AutoCore.confirm_and_vectorize(job_info["job_id"])

        resultado = f"{len(filenames)} archivo(s) procesados ({json.dumps(sync['resumen'], ensure_ascii=False)})"
        await db.run(rds.set_migracion_status, name, "ok", resultado)
        return {"collection": name, "status": "ok", "detail": resultado}
    except Exception as e:
        await db.run(rds.set_migracion_status, name, "error", str(e))
        print(f"❌ Migración de '{name}' falló: {e}")
        return {"collection": name, "status": "error", "detail": str(e)}


def ejecutar_migracion_en_hilo(coleccion: Dict[str, Any]) -> None:
    """Wrapper SÍNCRONO de ejecutar_migracion(), pensado para correr en un
    hilo aparte (loop.run_in_executor, o Starlette BackgroundTasks — que ya
    ejecuta las tareas sync que recibe en un threadpool automáticamente).

    Por qué hace falta: AutoCore.download_and_validate/confirm_and_vectorize
    hacen llamadas de red y de Qdrant directas (no wrapeadas en executor) —
    si se las llama tal cual desde una coroutine en el loop principal (como
    hacía antes esta migración, y como sigue haciendo hoy el Automático
    manual vía la UI), bloquean el event loop entero mientras corren: todo
    el resto de la app (login, chat de los asistentes, cualquier otra
    request) se congela hasta que termina. `asyncio.run()` acá adentro le
    da a ejecutar_migracion() un event loop propio, en un hilo del sistema
    operativo separado del principal — así una migración pesada (descargas,
    transcripción, Qdrant) corre en verdadero segundo plano, sin afectar a
    los usuarios reales del asistente mientras tanto."""
    asyncio.run(ejecutar_migracion(coleccion))


async def _resolve_model_name(collection_name: str) -> str:
    """El embedding real de la colección: el "maestro" de su RD si tiene
    una (todas las colecciones de una RD lo comparten), o el default del
    sistema si no — mismo criterio que usa el resto del pipeline
    (_build_pipeline_config en core.py) para no producir vectores de la
    dimensión equivocada en una colección legacy."""
    rd = await db.run(rds.get_rd_for_collection, collection_name)
    return (rd or {}).get("dense_strategy") or settings.EMBEDDING_MODEL


async def migracion_scheduler_loop() -> None:
    """Tarea de fondo (arrancada en el lifespan de app.py): cada ~60s
    revisa qué colecciones en modo "periodica" ya vencieron su intervalo y
    las migra, SECUENCIALMENTE — no concurrente, Qdrant se ha visto
    inestable bajo carga simultánea (ver trabajo previo de esta sesión).
    Cualquier excepción de una colección individual se atrapa dentro de
    ejecutar_migracion(); esta función solo protege el ciclo en sí (p.ej.
    un error consultando la lista de pendientes) para que un problema
    puntual no mate la tarea de fondo para siempre."""
    loop = asyncio.get_event_loop()
    while True:
        try:
            pendientes = await db.run(rds.list_colecciones_periodicas)
            for coleccion in pendientes:
                # run_in_executor (no `await ejecutar_migracion` directo):
                # deja el trabajo pesado en un hilo aparte, ver el docstring
                # de ejecutar_migracion_en_hilo — sin esto, el tick del
                # scheduler bloquearía el event loop principal y con él a
                # toda la app mientras dura la migración.
                await loop.run_in_executor(None, ejecutar_migracion_en_hilo, coleccion)
        except Exception as e:
            print(f"⚠️ Error en el ciclo del scheduler de migración: {e}")
        await asyncio.sleep(60)
