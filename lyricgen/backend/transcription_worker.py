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
    live: bool = False,
    anchor_lyrics: str = "",
) -> dict:
    """RQ entry point — sync wrapper around `_run_transcription_for_job`.

    Persiste el resultado (segments, reference_lyrics, etc.) en el row del
    job vía `update_job`. Marca status `transcribed` en éxito, o
    `transcription_failed` con `error_message` en error.

    Devuelve el mismo dict que el handler legacy para que /transcription-status
    pueda servirlo idéntico al frontend.
    """
    # Observability 2026-06-10: toda línea de log de este job lleva job_id.
    from observability import set_job_log_context
    set_job_log_context(job_id)
    # Lazy import — main.py es pesado y el worker no debería pagarlo si
    # corre otros queues. asyncio.run abre/cierra su propio event loop por job,
    # que es lo que queremos (jobs independientes, sin event-loop leak).
    from main import (  # type: ignore
        _looks_live, _maybe_anchor_align, _maybe_ctc_retime,
        _maybe_adlib_filter, _run_transcription_for_job,
    )
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
    #    handler que recibió el upload), descargarlo de R2. Usamos
    #    get_job_model con una session propia — get_job() requería un db arg
    #    posicional y to_dict() no incluye input_r2_key (bug 2026-05-23).
    if not os.path.exists(audio_path):
        from database import SessionLocal
        from jobs import get_job_model
        input_r2_key = None
        _db = SessionLocal()
        try:
            row = get_job_model(_db, job_id)
            input_r2_key = (row.input_r2_key if row else None)
        except Exception as e:
            logger.warning("[TRANSCRIBE-WORKER] get_job_model failed for %s: %s", job_id, e)
        finally:
            _db.close()
        if not input_r2_key:
            return _fail(job_id, "input_r2_key missing [v2-fix] — no se puede materializar el audio para transcribir.")
        os.makedirs(os.path.dirname(audio_path), exist_ok=True)
        if not storage.download_object(input_r2_key, audio_path):
            return _fail(job_id, "No pudimos leer el archivo subido. Reintentá en unos segundos.")

    # 3. Llamar al pipeline async existente. `request` y `current_user` son
    #    ignorados dentro del cuerpo (verified) — passing None es seguro.
    try:
        async def _run_with_retime():
            r = await _run_transcription_for_job(
                None, None, job_id, audio_path,
                language=language, artist=artist, title=title, filename=filename,
            )
            # Post-pases gateados, en lockstep con los dos endpoints HTTP
            # (/transcribe y /transcribe-uploaded). ESTE es el camino que
            # usa el frontend real (enqueue → ShortWorker), así que si acá
            # falta un wrapper, el usuario NO lo recibe aunque los endpoints
            # sí lo tengan (bug 05/07: el filtro de ad-libs estaba en los
            # endpoints pero no acá → los 'uh' salían fragmentados en prod).
            #   1. CTC re-time (CTC_ALIGN_ENABLED, default off)
            #   2. filtro de fantasmas + colapso de ad-libs (ADLIB_CONSENSUS_
            #      ENABLED, default off) — corre aunque CTC declinó.
            # Versión B (ANCHOR_LYRICS_ENABLED, default off): si el operador
            # pegó la letra oficial, anclarla con CTC ANTES del retime normal.
            # Si ancló (timing_source == "anchor_ctc"), saltear el retime CTC
            # de la cascada — los segments YA salieron del motor CTC con la
            # letra oficial, un segundo retime sería doble trabajo sobre otro
            # texto. En decline/excepción el helper devuelve r intacto y este
            # if no matchea → cae a la Versión A tal cual hoy.
            if (anchor_lyrics or "").strip():
                r = await _maybe_anchor_align(r, audio_path, job_id,
                                              anchor_lyrics)
            if not (isinstance(r, dict)
                    and r.get("timing_source") == "anchor_ctc"):
                r = await _maybe_ctc_retime(r, audio_path, job_id, artist, title)
            r = await _maybe_adlib_filter(
                r, audio_path, job_id,
                live_hint=live or _looks_live(title, filename))
            from lyrics_format import format_lyrics_pass as _fmt
            return await _fmt(r, language=language or "es")

        result = asyncio.run(_run_with_retime())
    except Exception as e:
        import traceback
        tb = traceback.format_exc()
        logger.exception("[TRANSCRIBE-WORKER] job=%s failed", job_id)
        # OBSERVABILITY (audit 2026-05-24): the orchestrator catches +
        # re-raises, but worker process exceptions don't reach Sentry
        # via FastAPI's auto-capture. Capture explicitly here so prod
        # crashes are visible in the Sentry dashboard, not just stdout.
        try:
            import sentry_sdk
            with sentry_sdk.push_scope() as scope:
                scope.set_tag("job_id", job_id)
                scope.set_tag("layer", "transcription_worker")
                sentry_sdk.capture_exception(e)
        except Exception:
            pass  # never let Sentry failures break the error path
        # Persistir un sumario corto + tipo de excepción para diagnóstico
        # vía /transcription-status sin tener que pegarse al log container.
        err_msg = f"{type(e).__name__}: {str(e)[:200]}"
        return _fail(job_id, err_msg, tb=tb)

    # 4. Persistir el resultado en el row.
    #
    # BUG fix 2026-05-23: en el path sync legacy /transcribe-uploaded
    # devolvía los segments inline al frontend, que los guardaba después con
    # /save-segments. En el path async el frontend pollea /transcription-status
    # esperando los segments en el response. El worker DEBE persistirlos.
    # Sin esta línea los jobs terminaban como "transcribed" con segments=null
    # y el editor abría vacío.
    try:
        segments = result.get("segments") if isinstance(result, dict) else None
        reference_lyrics = result.get("reference_lyrics", "") if isinstance(result, dict) else ""
        # INCIDENT 2026-05-25: this used to be `status="transcribed"`
        # — that broke `/generate` which checks for `transcribed_pending`
        # before letting the user submit ("Job is in state 'transcribed',
        # cannot generate."). The sync path (main.py:2715) always set
        # `transcribed_pending`; the async worker drifted from that
        # convention. The response of `/transcription-status` already
        # normalises `transcribed_pending` → `transcribed` for the
        # frontend (main.py:2778), so the editor sees `transcribed` and
        # `/generate` sees `transcribed_pending`. Same observable
        # behaviour as the legacy path, no frontend change needed.
        update_kwargs = {"status": "transcribed_pending", "current_step": "editing"}
        if segments is not None:
            update_kwargs["segments_json"] = segments
        # reference_lyrics no tiene columna en el modelo Job (defer a otro PR
        # si el editor lo necesita post-transcribe). Lo dejo en el log para
        # diagnóstico mientras tanto.
        if reference_lyrics:
            logger.info("[TRANSCRIBE-WORKER] job=%s ref_lyrics=%d chars (no persistido aún)",
                        job_id, len(reference_lyrics))
        update_job(job_id, **update_kwargs)
    except Exception as e:
        logger.warning("[TRANSCRIBE-WORKER] failed to persist final state: %s", e)
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
