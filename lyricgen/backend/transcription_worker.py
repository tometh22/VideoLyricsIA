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
import re

logger = logging.getLogger("genly.transcription_worker")
_EXCEPTION_TYPE_RE = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,63}\Z")


def _safe_exception_code(exc: BaseException) -> str:
    """Return a bounded class label; never serialize exception text/args."""
    name = getattr(type(exc), "__name__", "Exception")
    return name if isinstance(name, str) and _EXCEPTION_TYPE_RE.fullmatch(name) else "Exception"


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
        from lyric_content_policy import should_include_as_lyric
        kept = []
        prior_end = None
        for index, segment in enumerate(segments):
            if not isinstance(segment, dict):
                kept.append(segment)
                continue
            text = str(segment.get("text") or "")
            start = float(segment.get("start") or 0.0)
            isolated_tail = bool(
                index >= max(1, len(segments) - 3)
                and prior_end is not None and start - prior_end >= 2.5
            )
            provider_kind = str(
                segment.get("content_kind") or segment.get("kind") or ""
            )
            if not _is_whisper_hallucination(text) and should_include_as_lyric(
                text, provider_kind=provider_kind, isolated_tail=isolated_tail,
            ):
                kept.append(segment)
            prior_end = max(prior_end or 0.0, float(segment.get("end") or start))
    except Exception as exc:
        logger.warning(
            "[FINAL-HALLUCINATION] declined error_type=%s job=%s",
            _safe_exception_code(exc), job_id,
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
                except Exception as exc:
                    logger.info(
                        "[COVERAGE] sin stem para voiced_gaps error_type=%s",
                        _safe_exception_code(exc),
                    )
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
            _view_disagreements = (
                ((r.get("postpass_stats") or {}).get("gap_rescue") or {})
                .get("view_disagreements") or []
            )
            _ctc_declines = (
                ((r.get("postpass_stats") or {}).get("ctc_retime") or {})
                .get("unsafe_windows") or []
            )
            c["stem_mix_evidence_disagreements"] = len(_view_disagreements)
            c["ctc_short_repeated_motif_windows"] = len(_ctc_declines)
            _windows = build_unsafe_windows(
                r.get("segments") or [], words, voiced_gaps=_vg,
                independent_words=_independent,
                lexical_unverified=_lexical_verification["details"],
                structural_disagreements=_structural_disagreements,
                evidence_view_disagreements=_view_disagreements,
                ctc_declines=_ctc_declines,
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
    except Exception as exc:
        logger.warning(
            "[COVERAGE] medición final falló error_type=%s job=%s",
            _safe_exception_code(exc), job_id,
        )
    finally:
        if strip_internal and isinstance(r, dict):
            r.pop("_asr_words", None)
            r.pop("_independent_asr_words", None)
            r.pop("_pre_anchor_provider_segments", None)
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
    from transcription_quality import POLICY_VERSION, calibration_identity, evaluate
    from line_evidence import annotate_provider_evidence

    r = dict(r)
    r["segments"] = annotate_provider_evidence(r.get("segments") or [])

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
        is_live=live_hint,
        reference_attestation=r.get("reference_attestation"),
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
            candidate_result, retry_stats = await asyncio.to_thread(
                reprocess, r, audio_path, windows,
                language=language, job_id=job_id,
            )
            if POLICY_VERSION == "lyrics-quality-v6":
                changed = (
                    isinstance(candidate_result, dict)
                    and candidate_result.get("segments") != r.get("segments")
                )
                if changed or retry_stats.get("lines_replaced") or retry_stats.get("lines_inserted"):
                    retry_stats = dict(retry_stats)
                    retry_stats["v6_legacy_mutation_blocked"] = True
                    retry_stats["mutated_segments"] = False
                    retry_stats["lines_replaced"] = 0
                    retry_stats["lines_inserted"] = 0
                # Suggestions/evidence may be measured, but v6 never adopts an
                # inline result containing legacy in-place lyric mutations.
            else:
                r = candidate_result
            if retry_stats.get("lines_replaced") or retry_stats.get("lines_inserted"):
                r = timing_consistency_fn(r, job_id)
                r = _medir_cobertura_final(
                    r, job_id, antes_fmt, audio_path, strip_internal=False,
                    live_hint=live_hint,
                )
                post = r.get("postpass_stats") or {}
                windows = post.get("quality_windows") or []
    except Exception as exc:
        logger.warning(
            "[QUALITY-GATE] targeted retry failed error_type=%s job=%s",
            _safe_exception_code(exc), job_id,
        )
        retry_stats = {
            "attempted": True, "failed": True,
            "failure_reason": f"exception:{type(exc).__name__}",
            "declined": [f"exception:{type(exc).__name__}"],
        }

    diagnostics = retry_stats.get("structural_hybrid_diagnostics") or []
    acoustic_evidence = diagnostics[-1].get("evidence") if diagnostics else None
    from quality_jobs import _sanitize_analytical_evidence

    final = evaluate(
        r.get("segments") or [], post.get("coverage_final"),
        unsafe_windows=windows,
        retry_stats=_sanitize_analytical_evidence(retry_stats),
        require_independent=require_independent,
        is_live=live_hint,
        acoustic_evidence=_sanitize_analytical_evidence(acoustic_evidence),
        reference_attestation=r.get("reference_attestation"),
    )
    final["initial_decision"] = initial.get("decision")
    final["initial_score"] = initial.get("score")
    final["evaluated_revision"] = 0
    final_metrics = dict(final.get("metrics") or {})
    final_metrics["is_live"] = bool(live_hint)
    if live_hint:
        from lyric_content_policy import EDITORIAL_POLICY_ID
        final_metrics["editorial_policy_id"] = EDITORIAL_POLICY_ID
    final_metrics["language"] = str(language or "unknown")[:16]
    final["metrics"] = final_metrics
    r["transcription_quality"] = final
    if final["decision"] == "pass":
        logger.info("[QUALITY-GATE] PASS score=%s job=%s", final["score"], job_id)
    else:
        logger.warning(
            "[QUALITY-GATE] REVIEW_REQUIRED score=%s reasons=%s windows=%d job=%s",
            final["score"], [x.get("code") for x in final["reasons"]],
            len(windows), job_id,
        )
    # Capture the private recognition streams before removing transport-only
    # keys.  Persistence later binds this to EditorDocument.original_segments
    # in the same transaction that exposes the job to the editor.
    from machine_evidence import build_machine_evidence
    r["_machine_evidence"] = build_machine_evidence(r)
    r.pop("_asr_words", None)
    r.pop("_independent_asr_words", None)
    r.pop("_pre_anchor_provider_segments", None)
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
    except Exception as exc:
        logger.warning(
            "[TRANSCRIBE-WORKER] failed to flip status error_type=%s",
            _safe_exception_code(exc),
        )

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
        except Exception as exc:
            logger.warning(
                "[TRANSCRIBE-WORKER] get_job_model failed job=%s error_type=%s",
                job_id, _safe_exception_code(exc),
            )
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
        error_type = _safe_exception_code(exc)
        logger.warning(
            "[TRANSCRIBE-WORKER] audio validation failed job=%s error_type=%s",
            job_id, error_type,
        )
        return _fail(
            job_id,
            f"Archivo de audio inválido. Código: {error_type}.",
        )

    from quality_cache import sha256_file
    source_audio_sha256 = sha256_file(audio_path)
    source_audio_revision = 0
    # Establish the immutable source identity before the expensive pipeline.
    # Direct browser PUTs necessarily start at a mutable upload key because
    # the server does not see their bytes; the first worker materialization
    # promotes that object to its content-addressed destination.
    from database import Job as _IdentityJob, SessionLocal as _IdentitySession
    _identity_db = _IdentitySession()
    try:
        _identity_row = (
            _identity_db.query(_IdentityJob)
            .filter(_IdentityJob.job_id == job_id)
            .with_for_update().first()
        )
        if _identity_row is None:
            return {"job_id": job_id, "status": "discarded", "reason": "job_not_found"}
        if (
            _identity_row.input_audio_sha256
            and str(_identity_row.input_audio_sha256) != source_audio_sha256
        ):
            return {
                "job_id": job_id, "status": "discarded",
                "reason": "source_audio_changed",
            }
        if storage.is_enabled():
            immutable_key = storage.content_addressed_input_key(
                str(_identity_row.tenant_id or ""), job_id,
                source_audio_sha256, filename,
            )
            if str(_identity_row.input_r2_key or "") != immutable_key:
                # upload_file is idempotent at the content-addressed key and
                # avoids trusting a mutable source object after validation.
                if storage.upload_file(audio_path, immutable_key) != immutable_key:
                    raise RuntimeError("content_addressed_audio_promotion_failed")
                _identity_row.input_r2_key = immutable_key
            _identity_row.input_audio_etag = (
                storage.object_etag(immutable_key) or source_audio_sha256
            )
        elif not _identity_row.input_audio_etag:
            _identity_row.input_audio_etag = source_audio_sha256
        if not _identity_row.input_audio_sha256:
            _identity_row.input_audio_sha256 = source_audio_sha256
        if int(_identity_row.audio_revision or 0) <= 0:
            _identity_row.audio_revision = max(
                1, int(_identity_row.audio_revision or 0),
            )
        source_audio_revision = int(_identity_row.audio_revision or 0)
        _identity_db.commit()
    finally:
        _identity_db.close()

    # 3. Llamar al pipeline async existente. `request` y `current_user` son
    #    ignorados dentro del cuerpo (verified) — passing None es seguro.
    try:
        async def _run_with_retime():
            r = await _run_transcription_for_job(
                None, None, job_id, audio_path,
                language=language, artist=artist, title=title, filename=filename,
                live=live,
            )
            # Immutable provider evidence must exist before anchor CTC or any
            # other timing/content post-pass can replace words and bounds.
            from line_evidence import freeze_result_provider_evidence
            r = freeze_result_provider_evidence(r)
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
            r = await _quality_gate_and_retry(
                r, audio_path, job_id, _post_lang, _antes,
                _maybe_timing_consistency,
                live_hint=live or _looks_live(title, filename),
            )
            from delivery_repair_shadow import attach_delivery_repair_shadow
            return attach_delivery_repair_shadow(
                r,
                artist=artist,
                title=title,
                filename=filename,
                is_live=live or _looks_live(title, filename),
            )

        result = asyncio.run(_run_with_retime())
    except Exception as exc:
        error_type = _safe_exception_code(exc)
        logger.error(
            "[TRANSCRIBE-WORKER] job=%s failed error_type=%s",
            job_id, error_type,
        )
        # OBSERVABILITY (audit 2026-05-24): the orchestrator catches +
        # re-raises, but worker process exceptions don't reach Sentry
        # via FastAPI's auto-capture. Capture explicitly here so prod
        # crashes are visible in the Sentry dashboard, not just stdout.
        try:
            import sentry_sdk
            with sentry_sdk.push_scope() as scope:
                scope.set_tag("job_id", job_id)
                scope.set_tag("layer", "transcription_worker")
                scope.set_tag("error_type", error_type)
                sentry_sdk.capture_message(
                    "transcription_worker_failure", level="error",
                )
        except Exception:
            pass  # never let Sentry failures break the error path
        return _fail(
            job_id,
            f"No pudimos completar la transcripción. Código: {error_type}.",
        )

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
        machine_evidence = (
            result.pop("_machine_evidence", None)
            if isinstance(result, dict) else None
        )
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
            if (
                row.input_audio_sha256
                and str(row.input_audio_sha256) != source_audio_sha256
            ):
                logger.warning(
                    "[audio-occ] transcription result discarded job=%s "
                    "audio_identity_match=false",
                    job_id,
                )
                _persist_db.rollback()
                return {
                    "job_id": job_id, "status": "discarded",
                    "reason": "source_audio_changed",
                }
            if int(row.audio_revision or 0) != source_audio_revision:
                logger.warning(
                    "[audio-occ] transcription revision discarded job=%s "
                    "expected=%s actual=%s",
                    job_id, source_audio_revision, int(row.audio_revision or 0),
                )
                _persist_db.rollback()
                return {
                    "job_id": job_id, "status": "discarded",
                    "reason": "source_audio_revision_changed",
                }
            if not row.input_audio_sha256:
                row.input_audio_sha256 = source_audio_sha256
                row.input_audio_etag = source_audio_sha256
                row.audio_revision = max(1, int(row.audio_revision or 0))
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
                if not isinstance(segments, list):
                    raise RuntimeError("transcription_segments_missing")
                row.segments_json = segments
                # Freeze the exact machine snapshot before the editor can
                # autosave. Lazy creation was too late for legacy clients:
                # their first save could become `original_segments`.
                from editor import get_or_create_document
                document = get_or_create_document(
                    _persist_db, job_id, row.tenant_id, segments,
                    initial_reason="transcription",
                )
                previous_quality = (
                    dict(row.transcription_quality)
                    if isinstance(row.transcription_quality, dict) else {}
                )
                quality = result.get("transcription_quality") if isinstance(result, dict) else None
                if isinstance(quality, dict):
                    quality = dict(quality)
                    quality["machine_evidence_required"] = True
                    quality["machine_evidence_schema"] = (
                        "machine-transcription-evidence-v1"
                    )
                    quality["audio_sha256"] = source_audio_sha256
                    quality["audio_revision"] = int(row.audio_revision or 0)
                    quality["evaluated_revision"] = current_revision
                    quality["timing_source"] = row.timing_source or "unknown"
                    try:
                        from quality_learning_model import shadow_prediction_for_quality
                        quality["learning_shadow"] = shadow_prediction_for_quality(
                            quality, quality["timing_source"],
                        )
                    except Exception:
                        quality["learning_shadow"] = {
                            "available": False, "reason": "prediction_failed",
                            "mutated_segments": False,
                        }
                    from transcription_quality import quality_fingerprint
                    quality["quality_fingerprint"] = quality_fingerprint(
                        quality,
                        revision=current_revision,
                        content_hash=str(quality.get("segments_hash") or ""),
                    )
                    row.transcription_quality = quality
                    from correction_learning import machine_snapshot_provenance
                    from editor import attach_machine_provenance
                    attach_machine_provenance(
                        _persist_db, job_id,
                        machine_snapshot_provenance(row, quality),
                    )
                    from quality_shadow import record_shadow_decision
                    record_shadow_decision(
                        _persist_db, row, quality,
                        previous_quality=previous_quality,
                        evaluation_stage=(
                            "terminal" if not quality.get("unsafe_windows")
                            else "initial"
                        ),
                    )
                quality = dict(
                    quality if isinstance(quality, dict)
                    else row.transcription_quality or {}
                )
                quality["machine_evidence_required"] = True
                quality["machine_evidence_schema"] = (
                    "machine-transcription-evidence-v1"
                )
                row.transcription_quality = quality
                from machine_evidence import finalize_machine_evidence
                durable_evidence = finalize_machine_evidence(
                    machine_evidence,
                    original_segments=document.original_segments or [],
                    quality=quality,
                    audio_sha256=source_audio_sha256,
                    audio_revision=int(row.audio_revision or 0),
                )
                from editor import attach_machine_evidence, require_machine_snapshot
                attach_machine_evidence(_persist_db, document, durable_evidence)
                row.machine_snapshot_required = True
                require_machine_snapshot(row, document)
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
                    "[QUALITY-QUEUE] enqueue declined job=%s error_type=%s",
                    job_id, _safe_exception_code(enqueue_exc),
                )
        # reference_lyrics no tiene columna en el modelo Job (defer a otro PR
        # si el editor lo necesita post-transcribe). Lo dejo en el log para
        # diagnóstico mientras tanto.
        if reference_lyrics:
            logger.info("[TRANSCRIBE-WORKER] job=%s ref_lyrics=%d chars (no persistido aún)",
                        job_id, len(reference_lyrics))
    except Exception as exc:
        logger.warning(
            "[TRANSCRIBE-WORKER] failed to persist final state error_type=%s",
            _safe_exception_code(exc),
        )
        return _fail(
            job_id,
            "No pudimos guardar la evidencia de la transcripción. Reintentá.",
        )
    return result


def _fail(job_id: str, error_msg: str, tb: str = "") -> dict:
    """Marca el job como transcription_failed con un mensaje para el frontend.

    ``tb`` se conserva sólo por compatibilidad de firma y se descarta. Un
    traceback puede incluir audio paths, respuestas ASR o letras.
    """
    from jobs import update_job
    try:
        update_job(
            job_id,
            status="transcription_failed",
            current_step="error",
            error=error_msg[:500],
        )
    except Exception as exc:
        logger.warning(
            "[TRANSCRIBE-WORKER] failed to persist error job=%s error_type=%s",
            job_id, _safe_exception_code(exc),
        )
    return {"job_id": job_id, "status": "transcription_failed", "error": error_msg}
