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


def _drop_final_credit_hallucinations(result: dict, job_id: str) -> dict:
    """Remove known subtitle/training credits created by late post-passes.

    The broad Whisper loop filter already runs inside the ASR cascade. At this
    late boundary we deliberately use only the narrow known-credit predicate:
    repeated musical ``no/oh/wow`` must survive, while an outro credit can
    never reach the editor merely because the formatter assembled it after
    the earlier filter ran. Never raises and preserves object identity when
    there is nothing to remove.
    """
    if not isinstance(result, dict):
        return result
    segments = result.get("segments") or []
    try:
        from pipeline import _is_whisper_hallucination
        kept = [
            segment for segment in segments
            if not (
                isinstance(segment, dict)
                and _is_whisper_hallucination(str(segment.get("text") or ""))
            )
        ]
    except Exception as exc:
        logger.warning(
            "[FINAL-HALLUCINATION] declined: %r job=%s", exc, job_id,
        )
        return result
    dropped = len(segments) - len(kept)
    if not dropped:
        return result
    cleaned = dict(result)
    cleaned["segments"] = kept
    cleaned.setdefault("postpass_stats", {})["final_credit_filter"] = {
        "dropped": dropped,
    }
    logger.warning(
        "[FINAL-HALLUCINATION] dropped %d known credit(s) job=%s",
        dropped, job_id,
    )
    return cleaned


# ── Cobertura contra el audio, por etapa ──────────────────────────────────
#
# La cascada deja en el result su propia cobertura (`audio_coverage`) y el
# stream de palabras del ASR (`_asr_words`, transporte interno). Acá volvemos
# a medir DESPUÉS de los post-pases, porque dos de ellos pueden sacar líneas
# (el filtro de ad-libs borra la cola; el formateador reescribe textos) y
# ninguno actualiza `timing_source`. Sin esta medición por etapa no se puede
# atribuir la pérdida — que es exactamente lo que bloqueó el diagnóstico del
# job b3a51559: llegó al editor con 2 carteles vacíos y una línea duplicada y
# no había forma de saber qué etapa los produjo.
#
# Sólo observabilidad: no altera el resultado.

def _coverage_de(r) -> float | None:
    """Cobertura del audio del result actual, o None si no se puede medir."""
    if not isinstance(r, dict):
        return None
    words = r.get("_asr_words")
    if not words:
        return None
    try:
        from audio_coverage import audio_coverage
        return audio_coverage(r.get("segments") or [], words)
    except Exception:
        return None


def _medir_cobertura_final(r, job_id: str, antes_fmt: float | None,
                           audio_path: str = "", *, strip_internal: bool = True,
                           live_hint: bool = False):
    """Loguea la cobertura final y la compara con la de la cascada y la
    previa al formateador. Saca `_asr_words` del result (transporte interno:
    no se persiste ni llega al cliente). Nunca levanta.

    Con `audio_path` y stem cacheado disponible, agrega el guardrail
    definitivo `voiced_gaps`: huecos entre carteles cruzados contra el VAD
    del stem. Es la métrica que la cobertura por palabras no puede dar — un
    punto sordo del ASR le es invisible (dcf773b5 reportó zonas_sin_letra=0
    con 30,7s de canto sin cartel en pantalla)."""
    if not isinstance(r, dict):
        return r
    _stem = None
    try:
        words = r.get("_asr_words")
        if words:
            from audio_coverage import summarize
            _dur = None
            if audio_path and os.path.exists(audio_path):
                try:
                    from pipeline import _audio_duration
                    _dur = _audio_duration(audio_path)
                    import vocal_sep as _vs
                    _stem = _vs.separate_vocals(audio_path, cache_only=True)
                except Exception as e:
                    logger.info("[COVERAGE] sin stem para voiced_gaps (%r)", e)
            # Veredictos del sondeo con ASR: gap_rescue mide PALABRAS, la
            # única evidencia real de letra faltante. El breaker no puede
            # acusar un hueco que aquél ya descartó (batch 30-07: 8 de 8
            # zonas acusadas por energía eran fuga de guitarra/vientos).
            _skip = ((r.get("postpass_stats") or {})
                     .get("gap_rescue", {}).get("skipped") or [])
            c = summarize(r.get("segments") or [], words,
                          stem_path=_stem, audio_duration=_dur,
                          rescue_skipped=_skip, live_hint=live_hint)
            if _dur is not None:
                c["audio_duration_s"] = round(float(_dur), 3)
            # Keep the exact unsafe windows as structured data.  Counts in a
            # log line are not enough for a bounded retry or for the editor to
            # take the operator to the problematic part of the song.
            from audio_coverage import voiced_gaps as _voiced_gaps
            from transcription_quality import build_unsafe_windows
            _vg = _voiced_gaps(
                r.get("segments") or [], _stem, audio_duration=_dur,
                rescue_skipped=_skip, include_leading=live_hint,
            )
            _independent = r.get("_independent_asr_words") or []
            _lexical_verification = {
                "total": 0, "verified": 0, "unverified": 0, "details": [],
            }
            if _independent:
                from audio_coverage import (
                    audio_coverage as _independent_coverage,
                    text_mismatches as _independent_mismatches,
                    uncovered_spans as _independent_uncovered,
                )
                c["independent_witness_words"] = len(_independent)
                c["independent_audio_coverage"] = _independent_coverage(
                    r.get("segments") or [], _independent,
                )
                c["independent_text_mismatches"] = len(
                    _independent_mismatches(
                        r.get("segments") or [], _independent,
                    )
                )
                _iw_gaps = _independent_uncovered(
                    r.get("segments") or [], _independent,
                )
                c["independent_uncovered_spans"] = len(_iw_gaps)
                c["independent_uncovered_seconds"] = round(sum(
                    max(0.0, float(end) - float(start))
                    for start, end, _count in _iw_gaps
                ), 3)
                from live_lexical_consensus import verify_corrections
                _lexical_verification = verify_corrections(
                    r.get("segments") or [], _independent,
                )
                c["live_lexical_corrections"] = _lexical_verification["total"]
                c["live_lexical_verified"] = _lexical_verification["verified"]
                c["live_lexical_unverified"] = _lexical_verification["unverified"]
            _structural_disagreements = [
                {
                    "index": index,
                    "start": segment.get("start"),
                    "end": segment.get("end"),
                    "suggestion": segment.get("live_structural_suggestion"),
                }
                for index, segment in enumerate(r.get("segments") or [])
                if isinstance(segment, dict)
                and segment.get("live_structural_suggestion")
            ]
            c["live_structural_disagreements"] = len(
                _structural_disagreements
            )
            _windows = build_unsafe_windows(
                r.get("segments") or [], words, voiced_gaps=_vg,
                independent_words=_independent,
                lexical_unverified=_lexical_verification["details"],
                structural_disagreements=_structural_disagreements,
            )
            cascada = r.get("audio_coverage")
            final = c["audio_coverage"]
            r["audio_coverage"] = final
            r.setdefault("postpass_stats", {})["coverage_final"] = c
            r["postpass_stats"]["quality_windows"] = _windows
            log = logger.warning if final < 0.8 else logger.info
            log("[COVERAGE] final=%.0f%% (cascada=%s, pre-formatter=%s) "
                "zonas_sin_letra=%d (%.1fs, peor %.1fs) "
                "carteles_texto_equivocado=%d huecos_con_voz=%d (%.1fs) job=%s",
                final * 100,
                f"{cascada * 100:.0f}%" if cascada is not None else "?",
                f"{antes_fmt * 100:.0f}%" if antes_fmt is not None else "?",
                c["uncovered_spans"], c["uncovered_seconds"],
                c["worst_span_s"], c.get("text_mismatches", 0),
                c.get("voiced_gaps", 0), c.get("voiced_gap_s", 0.0), job_id)
            # Circuit breaker: canto sin cartel según el VAD del stem → el
            # job sale con bandera, nunca en silencio. Peor caso acotado.
            _warn_s = float(os.environ.get("VOICED_GAP_WARN_S", "10"))
            if c.get("voiced_gap_s", 0.0) >= _warn_s:
                r["coverage_warning"] = True
                r["voiced_gap_s"] = c["voiced_gap_s"]
                logger.warning(
                    "[COVERAGE] CIRCUIT BREAKER: %.1fs de CANTO sin cartel "
                    "según el VAD del stem — el job sale marcado para "
                    "revisión, no en silencio job=%s",
                    c["voiced_gap_s"], job_id)
            # Carteles que no dicen lo que se canta: la dimensión que la
            # cobertura no ve (el usuario detectó a ojo 2 en un job con
            # 76 % de cobertura). Detalle por línea para diagnóstico.
            if c.get("text_mismatches"):
                from audio_coverage import text_mismatches as _tm
                for m in _tm(r.get("segments") or [], words)[:6]:
                    logger.warning(
                        "[COVERAGE] cartel #%d (%.1f-%.1fs) no suena a lo "
                        "cantado ahí (ratio=%.2f) job=%s",
                        m["index"], m["start"], m["end"], m["ratio"], job_id)
            # Atribución explícita: qué etapa se comió el canto.
            if cascada is not None and (cascada - final) > 0.02:
                logger.warning(
                    "[COVERAGE] los POST-PASES perdieron %.0f%% del canto "
                    "(cascada %.0f%% → final %.0f%%) job=%s",
                    (cascada - final) * 100, cascada * 100, final * 100, job_id)
            if antes_fmt is not None and (antes_fmt - final) > 0.02:
                logger.warning(
                    "[COVERAGE] el FORMATTER perdió %.0f%% del canto job=%s",
                    (antes_fmt - final) * 100, job_id)
            # Carteles vacíos: el defecto que no se pudo explicar en b3a51559.
            vacios = sum(1 for s in (r.get("segments") or [])
                         if isinstance(s, dict) and not (s.get("text") or "").strip())
            if vacios:
                logger.warning("[COVERAGE] %d segmento(s) con TEXTO VACÍO "
                               "en la salida final job=%s", vacios, job_id)
    except Exception as e:
        logger.warning("[COVERAGE] medición final falló: %r job=%s", e, job_id)
    finally:
        if strip_internal and isinstance(r, dict):
            r.pop("_asr_words", None)
            r.pop("_independent_asr_words", None)
        if _stem:
            try:
                os.unlink(_stem)
            except OSError:
                pass
    return r


async def _quality_gate_and_retry(r: dict, audio_path: str, job_id: str,
                                  language: str, antes_fmt: float | None,
                                  timing_consistency_fn, *, live_hint: bool = False):
    """Measure, retry only unsafe windows, and persist one final verdict."""
    from transcription_quality import calibration_identity, evaluate

    calibrated = calibration_identity()["calibrated"]
    require_independent = bool(
        calibrated or (
            live_hint and os.environ.get("LIVE_INDEPENDENT_VERIFY_ENABLED", "0")
            .strip().lower() in {"1", "true", "yes", "on"}
        )
    )

    r = _medir_cobertura_final(
        r, job_id, antes_fmt, audio_path, strip_internal=False,
        live_hint=live_hint,
    )
    post = r.get("postpass_stats") or {}
    windows = post.get("quality_windows") or []
    initial = evaluate(
        r.get("segments") or [], post.get("coverage_final"),
        unsafe_windows=windows, require_independent=require_independent,
    )
    retry_stats = {"attempted": False}
    try:
        from targeted_consensus import is_enabled, reprocess
        inline_retry_enabled = (
            os.environ.get("TRANSCRIPTION_QUALITY_INLINE_RETRY", "0")
            .strip().lower() in {"1", "true", "yes", "on"}
        )
        if (
            inline_retry_enabled
            and initial.get("decision") != "pass" and windows and is_enabled()
        ):
            r, retry_stats = await asyncio.to_thread(
                reprocess, r, audio_path, windows,
                language=language, job_id=job_id,
            )
            if retry_stats.get("lines_replaced") or retry_stats.get("lines_inserted"):
                r = timing_consistency_fn(r, job_id)
                r = _medir_cobertura_final(
                    r, job_id, antes_fmt, audio_path, strip_internal=False,
                    live_hint=live_hint,
                )
                post = r.get("postpass_stats") or {}
                windows = post.get("quality_windows") or []
    except Exception as exc:
        logger.warning("[QUALITY-GATE] targeted retry failed: %r job=%s", exc, job_id)
        retry_stats = {
            "attempted": True, "failed": True,
            "failure_reason": f"exception:{type(exc).__name__}",
            "declined": [f"exception:{type(exc).__name__}"],
        }

    diagnostics = retry_stats.get("structural_hybrid_diagnostics") or []
    acoustic_evidence = diagnostics[-1].get("evidence") if diagnostics else None

    final = evaluate(
        r.get("segments") or [], post.get("coverage_final"),
        unsafe_windows=windows, retry_stats=retry_stats,
        require_independent=require_independent,
        acoustic_evidence=acoustic_evidence,
    )
    final["initial_decision"] = initial.get("decision")
    final["initial_score"] = initial.get("score")
    final["evaluated_revision"] = 0
    r["transcription_quality"] = final
    if final["decision"] == "pass":
        logger.info("[QUALITY-GATE] PASS score=%s job=%s", final["score"], job_id)
    else:
        logger.warning(
            "[QUALITY-GATE] REVIEW_REQUIRED score=%s reasons=%s windows=%d job=%s",
            final["score"], [x.get("code") for x in final["reasons"]],
            len(windows), job_id,
        )
    r.pop("_asr_words", None)
    r.pop("_independent_asr_words", None)
    return r


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
        _validate_audio_file_on_disk,
        _looks_live, _maybe_anchor_align, _maybe_ctc_retime,
        _maybe_adlib_filter, _maybe_chorus_snap, _maybe_gap_rescue,
        _maybe_phrase_segment, _maybe_repetition_reconcile,
        _maybe_timing_consistency, _maybe_word_vote,
        _resolve_postprocess_language, _run_transcription_for_job,
    )
    from jobs import update_job
    import storage

    if not filename:
        filename = os.path.basename(audio_path)

    # 1. Status flip a "transcribing" para que el polling lo vea ya en marcha.
    try:
        update_job(
            job_id,
            status="transcribing",
            current_step="transcribe.prepare",
            progress=2,
        )
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

    # The async worker is the first process that materializes the R2 object.
    # Validate here instead of downloading once in API and once again here.
    # A corrupt upload is persisted as transcription_failed and the existing
    # polling UI exposes the retryable error to the operator.
    try:
        _validate_audio_file_on_disk(filename, audio_path)
    except Exception as exc:
        detail = getattr(exc, "detail", None) or str(exc)
        return _fail(job_id, f"Archivo de audio inválido: {str(detail)[:200]}")

    # 3. Llamar al pipeline async existente. `request` y `current_user` son
    #    ignorados dentro del cuerpo (verified) — passing None es seguro.
    try:
        async def _run_with_retime():
            r = await _run_transcription_for_job(
                None, None, job_id, audio_path,
                language=language, artist=artist, title=title, filename=filename,
                live=live,
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
            _post_lang = _resolve_postprocess_language(
                language, r, job_id=job_id,
            )
            r = await _maybe_adlib_filter(
                r, audio_path, job_id,
                live_hint=live or _looks_live(title, filename),
                language=_post_lang,
            )
            r = _maybe_repetition_reconcile(r, job_id)
            r = await _maybe_gap_rescue(r, audio_path, job_id, _post_lang)
            r = await _maybe_word_vote(
                r, audio_path, job_id, _post_lang,
                live_hint=live or _looks_live(title, filename),
            )
            r = _maybe_chorus_snap(r, job_id)
            r = _maybe_phrase_segment(r, job_id)
            from lyrics_format import format_lyrics_pass as _fmt
            _antes = _coverage_de(r)
            r = await _fmt(r, language=_post_lang)
            r = _drop_final_credit_hallucinations(r, job_id)
            # Último post-pase: re-encuadra cada cartel a sus propias palabras
            # (audit 2026-08-13). Va al final porque todas las etapas de
            # arriba pueden haber movido start/end o words de forma
            # independiente. Lockstep con los dos caminos HTTP de main.py.
            r = _maybe_timing_consistency(r, job_id)
            return await _quality_gate_and_retry(
                r, audio_path, job_id, _post_lang, _antes,
                _maybe_timing_consistency,
                live_hint=live or _looks_live(title, filename),
            )

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
        quality = None
        persisted_revision = 0
        persisted_hash = ""
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
        from database import Job, SessionLocal
        _persist_db = SessionLocal()
        try:
            row = (
                _persist_db.query(Job).filter(Job.job_id == job_id)
                .with_for_update().first()
            )
            if row is None:
                raise LookupError("job disappeared before transcription persistence")
            current_revision = int(row.segments_revision or 0)
            if current_revision > 0 and segments != row.segments_json:
                # An operator edited while the ASR was running. Preserve the
                # human version and do not attach a verdict for discarded data.
                logger.warning(
                    "[segments-occ] transcription result discarded job=%s revision=%s",
                    job_id, current_revision,
                )
                quality = None
            else:
                if segments is not None:
                    row.segments_json = segments
                previous_quality = (
                    dict(row.transcription_quality)
                    if isinstance(row.transcription_quality, dict) else {}
                )
                quality = result.get("transcription_quality") if isinstance(result, dict) else None
                if isinstance(quality, dict):
                    quality = dict(quality)
                    quality["evaluated_revision"] = current_revision
                    quality["timing_source"] = row.timing_source or "unknown"
                    from transcription_quality import quality_fingerprint
                    quality["quality_fingerprint"] = quality_fingerprint(
                        quality,
                        revision=current_revision,
                        content_hash=str(quality.get("segments_hash") or ""),
                    )
                    row.transcription_quality = quality
                    from quality_shadow import record_shadow_decision
                    record_shadow_decision(
                        _persist_db, row, quality,
                        previous_quality=previous_quality,
                        evaluation_stage=(
                            "terminal" if not quality.get("unsafe_windows")
                            else "initial"
                        ),
                    )
            row.status = "transcribed_pending"
            row.current_step = "editing"
            _persist_db.commit()
            persisted_revision = current_revision
            persisted_tenant_id = str(row.tenant_id or "")
            persisted_hash = (
                quality.get("segments_hash") if isinstance(quality, dict) else None
            )
        finally:
            _persist_db.close()
        # Quality analysis is isolated from the latency-sensitive
        # transcription/bg_preview fleet.  It is suggestion-only and OCC-bound
        # to the exact revision/hash persisted above.
        if (
            isinstance(quality, dict)
            and quality.get("decision") != "pass"
            and quality.get("unsafe_windows")
        ):
            try:
                from queue_jobs import enqueue_transcription_quality
                enqueue_transcription_quality(
                    job_id, expected_revision=persisted_revision,
                    expected_segments_hash=persisted_hash or "",
                    filename=filename, tenant_id=persisted_tenant_id,
                )
            except Exception as enqueue_exc:
                logger.warning(
                    "[QUALITY-QUEUE] enqueue declined job=%s: %r",
                    job_id, enqueue_exc,
                )
        # reference_lyrics no tiene columna en el modelo Job (defer a otro PR
        # si el editor lo necesita post-transcribe). Lo dejo en el log para
        # diagnóstico mientras tanto.
        if reference_lyrics:
            logger.info("[TRANSCRIBE-WORKER] job=%s ref_lyrics=%d chars (no persistido aún)",
                        job_id, len(reference_lyrics))
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
