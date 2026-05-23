"""Transcription worker — desacopla Whisper de los API workers.

ANTES (2026-05-23 y anterior): `/transcribe-uploaded` corría
`_run_transcription_for_job` inline dentro del request handler. Cada llamada
pinaba un worker de la API container por 15-20 s (Whisper + lrclib + forced
align). Con 3 usuarios actuales × 5 archivos eso ya quedaba apretado; el
roadmap (decenas de usuarios) lo rompería: los demás endpoints (status polls,
edits, save-segments) entrarían en cola detrás del Whisper bloqueando.

AHORA: el handler `/transcribe-uploaded` enqueue a la cola `transcription`,
devuelve `202 Accepted + job_id`, y este módulo es el entry point que el
worker container ejecuta. Frontend pollea `/transcription-status/{job_id}`
hasta que `status == "transcribed"`.

DISEÑO
------
- `run_transcription_job(job_id)` es la entry point pública (sync, importable
  por qualname para RQ). Abre su propia sesión de DB, descarga el audio de R2
  si todavía no está en disco, y llama a `_run_transcription_for_job` (que
  vive en main.py, async, ya battle-tested) vía `asyncio.run`.
- El código async existente NO se duplica — lo reusamos tal cual. La función
  no consulta `request` ni `current_user` (lo verifiqué: están en la firma
  pero nunca se referencian dentro del cuerpo).
- Errores se persisten en `job.error_message` + `status = "transcription_failed"`
  para que el frontend los muestre con un botón "Reintentar".
- El worker NO maneja per-tenant rate limit ni cancel todavía (v2). Si un
  job se cancela vía DB flag `job.cancelled=True`, este worker no lo respeta
  hoy — el peor caso es gastar ~$0.006 de cuota Whisper inútilmente.

Feature flag: `ASYNC_TRANSCRIBE_ENABLED=true` en el handler `/transcribe-uploaded`
elige entre este path async y el legacy sync. Default a env de staging primero.
"""
from __future__ import annotations

import asyncio
import logging
import os

logger = logging.getLogger("genly.transcription_worker")


def run_transcription_job(
    job_id: str,
    audio_path: str,
    *,
    language: str = "",
    artist: str = "",
    title: str = "",
    filename: str = "",
) -> dict:
    """RQ entry point — sync wrapper around `_run_transcription_for_job`.

    Persiste el resultado (segments, reference_lyrics, etc.) en el row del
    job vía `update_job`. Marca status `transcribed` en éxito, o
    `transcription_failed` con `error_message` en error.

    Devuelve el mismo dict que el handler legacy para que /transcription-status
    pueda servirlo idéntico al frontend.
    """
    # Lazy import — main.py es pesado y el worker no debería pagarlo si
    # corre otros queues. asyncio.run abre/cierra su propio event loop por job,
    # que es lo que queremos (jobs independientes, sin event-loop leak).
    from main import _run_transcription_for_job  # type: ignore
    from jobs import update_job, get_job
    import storage

    if not filename:
        filename = os.path.basename(audio_path)

    # 1. Status flip a "transcribing" para que el polling lo vea ya en marcha.
    try:
        update_job(job_id, status="transcribing", current_step="transcribing")
    except Exception as e:
        logger.warning("[TRANSCRIBE-WORKER] failed to flip status: %s", e)

    # 2. Si el archivo no está en disco (ej. worker en container distinto al
    #    handler que recibió el upload), descargarlo de R2.
    if not os.path.exists(audio_path):
        # input_r2_key necesario — leemos del row.
        try:
            job = get_job(job_id)
            input_r2_key = (job or {}).get("input_r2_key") if isinstance(job, dict) else None
        except Exception:
            input_r2_key = None
        if not input_r2_key:
            return _fail(job_id, "input_r2_key missing — no se puede materializar el audio para transcribir.")
        os.makedirs(os.path.dirname(audio_path), exist_ok=True)
        if not storage.download_object(input_r2_key, audio_path):
            return _fail(job_id, "No pudimos leer el archivo subido. Reintentá en unos segundos.")

    # 3. Llamar al pipeline async existente. `request` y `current_user` son
    #    ignorados dentro del cuerpo (verified) — passing None es seguro.
    try:
        result = asyncio.run(_run_transcription_for_job(
            None, None, job_id, audio_path,
            language=language, artist=artist, title=title, filename=filename,
        ))
    except Exception as e:
        import traceback
        tb = traceback.format_exc()
        logger.exception("[TRANSCRIBE-WORKER] job=%s failed", job_id)
        # Persistir un sumario corto + tipo de excepción para diagnóstico
        # vía /transcription-status sin tener que pegarse al log container.
        err_msg = f"{type(e).__name__}: {str(e)[:200]}"
        return _fail(job_id, err_msg, tb=tb)

    # 4. Persistir el resultado en el row para que /transcription-status lo
    #    devuelva al frontend. Los segments ya los persiste el handler async
    #    en su scoped_db() interno; acá solo flippeamos status final.
    try:
        update_job(job_id, status="transcribed", current_step="editing")
    except Exception as e:
        logger.warning("[TRANSCRIBE-WORKER] failed to flip status to transcribed: %s", e)
    return result


def _fail(job_id: str, error_msg: str, tb: str = "") -> dict:
    """Marca el job como transcription_failed con un mensaje para el frontend.

    El traceback se loguea (para grepear el log container) pero NO se persiste
    en la DB (puede ser >500 chars y conviene mantener `error` corto para el
    UI de error inline en el editor).
    """
    from jobs import update_job
    if tb:
        logger.error("[TRANSCRIBE-WORKER] job=%s traceback:\n%s", job_id, tb)
    try:
        update_job(
            job_id,
            status="transcription_failed",
            current_step="error",
            error=error_msg[:500],
        )
    except Exception as e:
        logger.warning("[TRANSCRIBE-WORKER] failed to persist error for %s: %s", job_id, e)
    return {"job_id": job_id, "status": "transcription_failed", "error": error_msg}
