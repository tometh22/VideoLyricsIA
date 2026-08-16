"""Isolated, revision-safe transcription quality analysis jobs.

These jobs never mutate lyric rows.  They may attach evidence/suggestions to
``Job.transcription_quality`` only while the persisted segment revision and
content hash still match the snapshot captured at enqueue time.
"""
from __future__ import annotations

from datetime import datetime, timezone
import logging
import os
import re
import resource
import tempfile
import time
import unicodedata


logger = logging.getLogger("genly.quality_jobs")

_WINDOW_REASON_PRIORITY = {
    "timeline_inversion": 100,
    "invalid_timing_range": 100,
    "duplicate_line_starts": 95,
    "live_structural_disagreement": 90,
    "event_count": 90,
    "strong_unassigned_vocal_events": 90,
    "text_mismatch": 80,
    "independent_text_mismatch": 80,
    "uncovered_asr": 70,
    "independent_uncovered_asr": 70,
    "live_lexical_unverified": 65,
}


def _prioritize_windows(windows: list[dict]) -> list[dict]:
    """Analyze the highest-risk windows first, deterministically."""
    def key(window: dict):
        reasons = set(window.get("reasons") or [window.get("reason") or "unsafe"])
        priority = max((_WINDOW_REASON_PRIORITY.get(reason, 50) for reason in reasons),
                       default=50)
        duration = max(
            0.0,
            float(window.get("end") or 0.0) - float(window.get("start") or 0.0),
        )
        return (-priority, -duration, float(window.get("start") or 0.0),
                str(window.get("id") or ""))

    return sorted((dict(window) for window in windows), key=key)


def _max_windows() -> int:
    try:
        configured = int(os.environ.get("TRANSCRIPTION_QUALITY_MAX_WINDOWS", "4"))
    except (TypeError, ValueError):
        configured = 4
    return max(1, min(12, configured))


def _release() -> str:
    return str(
        os.environ.get("RELEASE")
        or os.environ.get("RAILWAY_GIT_COMMIT_SHA")
        or "unknown"
    )[:64]


def _snapshot(job_id: str):
    from database import Job, SessionLocal

    db = SessionLocal()
    try:
        row = db.query(Job).filter(Job.job_id == job_id).first()
        if row is None:
            return None
        return {
            "revision": int(row.segments_revision or 0),
            "segments": [dict(item) for item in (row.segments_json or [])],
            "quality": dict(row.transcription_quality or {}),
            "input_r2_key": row.input_r2_key,
            "filename": row.filename,
        }
    finally:
        db.close()


def _queue_wait_seconds() -> float | None:
    try:
        from rq import get_current_job

        rq_job = get_current_job()
        enqueued = getattr(rq_job, "enqueued_at", None)
        if enqueued is None:
            return None
        if enqueued.tzinfo is None:
            enqueued = enqueued.replace(tzinfo=timezone.utc)
        return max(0.0, (datetime.now(timezone.utc) - enqueued).total_seconds())
    except Exception:
        return None


def _persist_if_current(job_id: str, expected_revision: int,
                        expected_hash: str, quality: dict) -> bool:
    from database import Job, SessionLocal
    from transcription_quality import quality_fingerprint, segments_hash

    db = SessionLocal()
    try:
        row = (
            db.query(Job).filter(Job.job_id == job_id)
            .with_for_update().first()
        )
        if row is None:
            return False
        revision = int(row.segments_revision or 0)
        current_hash = segments_hash(row.segments_json or [])
        if revision != int(expected_revision) or current_hash != expected_hash:
            logger.info(
                "[QUALITY-OCC] stale result discarded job=%s expected=%s/%s "
                "current=%s/%s", job_id, expected_revision, expected_hash[:12],
                revision, current_hash[:12],
            )
            return False
        previous = dict(row.transcription_quality or {})
        ack = previous.get("acknowledgement")
        quality = dict(quality)
        quality["evaluated_revision"] = revision
        quality["segments_hash"] = current_hash
        try:
            from quality_learning_model import shadow_prediction_for_quality
            quality["learning_shadow"] = shadow_prediction_for_quality(
                quality, str(row.timing_source or "unknown"),
            )
        except Exception as exc:
            logger.warning(
                "[QUALITY-LEARNING] shadow prediction unavailable job=%s error=%s",
                job_id, str(exc)[:160],
            )
            quality["learning_shadow"] = {
                "available": False, "reason": "prediction_failed",
                "mutated_segments": False,
            }
        new_fingerprint = quality_fingerprint(
            quality, revision=revision, content_hash=current_hash,
        )
        quality["quality_fingerprint"] = new_fingerprint
        if (
            isinstance(ack, dict)
            and ack.get("quality_fingerprint") == new_fingerprint
        ):
            quality["acknowledgement"] = ack
        row.transcription_quality = quality
        from quality_shadow import record_shadow_decision
        record_shadow_decision(
            db, row, quality, previous_quality=previous,
            evaluation_stage="terminal",
        )
        db.commit()
        return True
    finally:
        db.close()


def _cached_structure(cache, audio_hash: str, stem_hash: str,
                      stem_path: str, audio_path: str,
                      window: dict) -> tuple[dict, bool]:
    from acoustic_structure import POLICY_VERSION, analyze_window
    from quality_cache import ArtifactKind, QualityCacheAddress

    start = round(float(window.get("start") or 0.0), 3)
    end = round(float(window.get("end") or 0.0), 3)
    config = {
        "window": [start, end], "sample_rate": 16000,
        "boundary_hop_ms": 10, "embedding_hop_ms": 20,
        "max_window_s": 45,
    }
    address = QualityCacheAddress(
        artifact=ArtifactKind.N_BEST,
        audio_hash=audio_hash,
        model={"acoustic_structure": POLICY_VERSION},
        config=config, release=_release(),
        lineage={
            "stem": "demucs", "mix": "original_correlated_view",
            "separator_model": os.environ.get(
                "REPLICATE_DEMUCS_MODEL", os.environ.get("DEMUCS_MODEL", "unknown")
            ),
            "separator_version": os.environ.get("DEMUCS_MODEL_VERSION", "unknown"),
            "separator_checksum": os.environ.get("DEMUCS_MODEL_CHECKSUM", "unknown"),
            "separator_variant": os.environ.get("DEMUCS_VARIANT", "unknown"),
            "stem_sha256": stem_hash,
            "mix_sha256": audio_hash,
        },
    )
    value = cache.get_json(address)
    if isinstance(value, dict):
        return value, True
    value = analyze_window(
        stem_path, audio_path, window_start=start, window_end=end,
        cache=cache, audio_hash=audio_hash, stem_hash=stem_hash,
        mix_hash=audio_hash, release=_release(),
    )
    value["cache_ref"] = address.digest
    cache.put_json(address, value)
    return value, False


def _structure_summary(structure: dict) -> dict:
    return {
        "accepted": bool(structure.get("accepted")),
        "reason": structure.get("reason"),
        "window": structure.get("window"),
        "cache_ref": structure.get("cache_ref"),
        "best_partition": structure.get("best_partition"),
        "cardinality_posterior": structure.get("cardinality_posterior") or {},
        "motif_groups": structure.get("motif_groups") or [],
        "self_similarity": structure.get("self_similarity") or {},
        "diagnostics": structure.get("diagnostics") or {},
        "boundary_count": len(structure.get("boundaries") or []),
        "n_best_count": len(structure.get("n_best") or []),
    }


def _normalise_lyric(text: str) -> str:
    value = unicodedata.normalize("NFKC", str(text or "")).casefold()
    return " ".join(re.findall(r"\w+", value, flags=re.UNICODE))


def _confirmed_windows(segments: list[dict], windows: list[dict],
                       diagnostics: list[dict]) -> tuple[list[dict], list[dict]]:
    """Resolve only accepted mappings that confirm the persisted rows."""
    unresolved: list[dict] = []
    resolved: list[dict] = []
    for window in windows:
        start = float(window.get("start") or 0.0)
        end = float(window.get("end") or start)
        existing = [
            segment for segment in segments if isinstance(segment, dict)
            and start <= (
                float(segment.get("start") or 0.0)
                + float(segment.get("end") or segment.get("start") or 0.0)
            ) / 2 <= end
        ]
        confirmed = False
        for diagnostic in diagnostics:
            evidence = diagnostic.get("evidence") or {}
            mapping = evidence.get("content_mapping") or {}
            phonetic = mapping.get("phonetic_evidence") or {}
            span = diagnostic.get("window") or []
            if (
                not mapping.get("accepted")
                or not mapping.get("phonetic_verified")
                or not phonetic.get("accepted")
                or phonetic.get("schema") != "ctc-phonetic-evidence-v1"
                or not phonetic.get("calibration_id")
                or not re.fullmatch(
                    r"[0-9a-f]{40}", str(
                        (phonetic.get("model_identity") or {}).get(
                            "model_revision",
                        ) or ""
                    ),
                )
                or not re.fullmatch(
                    r"[0-9a-f]{64}", str(
                        phonetic.get("evidence_sha256") or ""
                    ),
                )
                or int(mapping.get("strong_unassigned_events") or 0) != 0
                or len(span) != 2
                or float(span[0]) > start or float(span[1]) < end
            ):
                continue
            proposed = [
                event for event in (mapping.get("events") or [])
                if isinstance(event, dict)
                and start <= (
                    float(event.get("start") or 0.0)
                    + float(event.get("end") or event.get("start") or 0.0)
                ) / 2 <= end
            ]
            if len(existing) != len(proposed) or not existing:
                continue
            confirmed = all(
                _normalise_lyric(old.get("text"))
                == _normalise_lyric(new.get("text"))
                and abs(float(old.get("start")) - float(new.get("start"))) <= .50
                and abs(float(old.get("end")) - float(new.get("end"))) <= .75
                for old, new in zip(existing, proposed)
            )
            if confirmed:
                break
        (resolved if confirmed else unresolved).append(dict(window))
    return unresolved, resolved


def run_transcription_quality_job(job_id: str, *, expected_revision: int,
                                  expected_segments_hash: str,
                                  filename: str = "") -> dict:
    """RQ entry point. Analyze unsafe windows and persist evidence only."""
    from observability import set_job_log_context
    from transcription_quality import calibration_identity, evaluate, segments_hash

    set_job_log_context(job_id)
    started = time.monotonic()
    usage_before = resource.getrusage(resource.RUSAGE_SELF)
    queue_wait_s = _queue_wait_seconds()
    snapshot = _snapshot(job_id)
    if snapshot is None:
        return {"status": "discarded", "reason": "job_not_found"}
    actual_hash = segments_hash(snapshot["segments"])
    if (
        snapshot["revision"] != int(expected_revision)
        or actual_hash != expected_segments_hash
    ):
        return {"status": "discarded", "reason": "stale_snapshot"}
    quality_before = snapshot["quality"]
    windows = list(quality_before.get("unsafe_windows") or [])
    if not windows:
        return {"status": "discarded", "reason": "no_unsafe_windows"}
    if not snapshot.get("input_r2_key"):
        failed = evaluate(
            snapshot["segments"], quality_before.get("metrics") or {},
            unsafe_windows=windows,
            retry_stats={
                "attempted": True, "failed": True,
                "failure_reason": "input_r2_key_missing",
                "queue": "transcription_quality", "mutated_segments": False,
            },
        )
        _persist_if_current(
            job_id, expected_revision, expected_segments_hash, failed,
        )
        return {"status": "retry_failed", "reason": "input_r2_key_missing"}

    stem_path = None
    source_audio_bytes = None
    stem_audio_bytes = None
    try:
        from quality_cache import QualityCache, sha256_file
        import storage
        import vocal_sep

        with tempfile.TemporaryDirectory(prefix=f"genly-quality-{job_id}-") as tmp:
            safe_name = os.path.basename(filename or snapshot.get("filename") or "audio.mp3")
            audio_path = os.path.join(tmp, safe_name)
            if not storage.download_object(snapshot["input_r2_key"], audio_path):
                raise RuntimeError("source_audio_download_failed")
            audio_hash = sha256_file(audio_path)
            stem_path = vocal_sep.separate_vocals(audio_path, cache_only=True)
            stem_cache_hit = bool(stem_path)
            if not stem_path:
                stem_path = vocal_sep.separate_vocals(audio_path)
            if not stem_path:
                raise RuntimeError("vocal_stem_unavailable")
            source_audio_bytes = os.path.getsize(audio_path)
            stem_audio_bytes = os.path.getsize(stem_path)
            stem_hash = sha256_file(stem_path)

            cache = QualityCache()
            analyses = []
            cache_hits = 0
            windows_truncated = 0
            max_windows = _max_windows()
            prioritized_windows = _prioritize_windows(windows)
            for window in prioritized_windows[:max_windows]:
                raw_start = float(window.get("start") or 0.0)
                raw_end = float(window.get("end") or raw_start)
                start = max(0.0, raw_start - 3.0)
                end = min(start + 45.0, raw_end + 3.0)
                truncated = end + 1e-6 < raw_end + 3.0
                windows_truncated += int(truncated)
                bounded = {
                    **window, "start": start, "end": end,
                    "analysis_truncated": truncated,
                    "requested_end_with_context": raw_end + 3.0,
                }
                structure, hit = _cached_structure(
                    cache, audio_hash, stem_hash, stem_path, audio_path, bounded,
                )
                cache_hits += int(hit)
                analyses.append({
                    "window": bounded,
                    "structure": _structure_summary(structure),
                })

            retry_stats = {
                "attempted": True, "queue": "transcription_quality",
                "windows_processed": len(analyses),
                "windows_total": len(windows),
                "windows_skipped": max(0, len(windows) - len(analyses)),
                "windows_truncated": windows_truncated,
                "cache_hits": cache_hits,
                "cache_misses": len(analyses) - cache_hits,
                "mutated_segments": False,
                "provider_attempts": 0,
                "audio_seconds_billed": 0.0,
                "stem_cache_hit": stem_cache_hit,
                "demucs_attempts": 0 if stem_cache_hit else 1,
            }
            # The provider/content retry remains separately kill-switchable.
            # Even if it proposes rows, this worker persists only diagnostics.
            if os.environ.get("TARGETED_CONSENSUS_ENABLED", "0").lower() in {
                "1", "true", "yes", "on",
            }:
                from targeted_consensus import reprocess
                _ignored, provider_stats = reprocess(
                    {"segments": snapshot["segments"], "_asr_words": []},
                    audio_path, windows, job_id=job_id, stem_path=stem_path,
                )
                retry_stats.update(provider_stats)
                retry_stats["mutated_segments"] = False

            evidence_windows = [
                {
                    "window_id": item["window"].get("id"),
                    "acoustic_structure": item["structure"],
                }
                for item in analyses
            ]
            evidence_windows.extend(
                item["evidence"]
                for item in retry_stats.get("structural_hybrid_diagnostics") or []
                if isinstance(item, dict) and item.get("evidence")
            )
            diagnostic = {"windows": evidence_windows}
            metrics = dict(quality_before.get("metrics") or {})
            hybrid_diagnostics = [
                item for item in retry_stats.get(
                    "structural_hybrid_diagnostics",
                ) or [] if isinstance(item, dict)
            ]
            remaining_windows, resolved_windows = _confirmed_windows(
                snapshot["segments"], windows, hybrid_diagnostics,
            )
            retry_stats["windows_resolved"] = len(resolved_windows)
            retry_stats["resolved_window_ids"] = [
                item.get("id") for item in resolved_windows if item.get("id")
            ]
            resolved_reasons: dict[str, int] = {}
            for item in resolved_windows:
                for reason in item.get("reasons") or [item.get("reason")]:
                    if reason:
                        resolved_reasons[str(reason)] = (
                            resolved_reasons.get(str(reason), 0) + 1
                        )
            quality = evaluate(
                snapshot["segments"], metrics,
                unsafe_windows=remaining_windows,
                retry_stats=retry_stats, acoustic_evidence=diagnostic,
                resolved_reason_counts=resolved_reasons,
                require_independent=calibration_identity()["calibrated"],
            )
            quality["analysis_windows"] = analyses
            quality["timing_source"] = quality_before.get("timing_source", "unknown")

        usage_after = resource.getrusage(resource.RUSAGE_SELF)
        quality["quality_job"] = {
            "queue": "transcription_quality",
            "queue_wait_s": round(queue_wait_s, 3) if queue_wait_s is not None else None,
            "wall_s": round(time.monotonic() - started, 3),
            "cpu_user_s": round(usage_after.ru_utime - usage_before.ru_utime, 3),
            "cpu_system_s": round(usage_after.ru_stime - usage_before.ru_stime, 3),
            "max_rss_kb": int(usage_after.ru_maxrss),
            "release": _release(),
            "audio_seconds_processed": round(sum(
                max(0.0, float(item["window"]["end"]) - float(item["window"]["start"]))
                for item in analyses
            ), 3),
            "audio_seconds_billed": float(retry_stats.get("audio_seconds_billed") or 0),
            "provider_attempts": int(retry_stats.get("provider_attempts") or 0),
            "demucs_attempts": int(retry_stats.get("demucs_attempts") or 0),
            "stem_cache_hit": bool(retry_stats.get("stem_cache_hit")),
            "source_audio_bytes": source_audio_bytes,
            "stem_audio_bytes": stem_audio_bytes,
            "audio_sha256": audio_hash,
            "stem_sha256": stem_hash,
            "api_cost_usd": retry_stats.get("api_cost_usd"),
            "cost_complete": bool(retry_stats.get("cost_complete", False)),
        }
        persisted = _persist_if_current(
            job_id, expected_revision, expected_segments_hash, quality,
        )
        return {
            "status": "persisted" if persisted else "discarded",
            "reason": None if persisted else "stale_after_analysis",
            "decision": quality.get("decision"),
        }
    except Exception:
        logger.exception("[QUALITY-JOB] failed job=%s", job_id)
        # Let RQ Retry handle transient R2/Demucs/provider failures.  Only the
        # on_failure callback after retries are exhausted persists retry_failed.
        raise
    finally:
        if stem_path:
            try:
                os.unlink(stem_path)
            except OSError:
                pass


def transcription_quality_failure_callback(job, connection, type_, value, traceback):
    """RQ failure callback; never changes job status or lyric segments."""
    try:
        # RQ 1.16 invokes the custom callback before deciding whether to
        # requeue. Do not publish a false retry_failed state on attempt one.
        if int(getattr(job, "retries_left", 0) or 0) > 0:
            logger.info(
                "[QUALITY-JOB] transient failure retained for retry job=%s retries_left=%s",
                getattr(job, "id", "unknown"), getattr(job, "retries_left", 0),
            )
            return
        kwargs = dict(getattr(job, "kwargs", {}) or {})
        args = list(getattr(job, "args", ()) or ())
        job_id = str(args[0] if args else "")
        if not job_id:
            return
        snapshot = _snapshot(job_id)
        if snapshot is None:
            return
        from transcription_quality import evaluate

        failed = evaluate(
            snapshot["segments"], snapshot["quality"].get("metrics") or {},
            unsafe_windows=snapshot["quality"].get("unsafe_windows") or [],
            retry_stats={
                "attempted": True, "failed": True,
                "failure_reason": f"{getattr(type_, '__name__', 'Error')}:{str(value)[:160]}",
                "queue": "transcription_quality", "mutated_segments": False,
            },
        )
        _persist_if_current(
            job_id, int(kwargs.get("expected_revision", -1)),
            str(kwargs.get("expected_segments_hash") or ""), failed,
        )
    except Exception:
        logger.exception("[QUALITY-JOB] failure callback failed")
