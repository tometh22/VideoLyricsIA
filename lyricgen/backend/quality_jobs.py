"""Isolated, revision-safe transcription quality analysis jobs.

These jobs never mutate lyric rows.  They may attach evidence/suggestions to
``Job.transcription_quality`` only while the persisted segment revision and
content hash still match the snapshot captured at enqueue time.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import hmac
import json
import logging
import math
import os
import re
import resource
import tempfile
import time
import unicodedata


logger = logging.getLogger("genly.quality_jobs")


def _attach_structural_t4_shadow(quality: dict, segments: list[dict]) -> dict:
    """Persist T4 evidence only when the staging observation flag is on."""

    if os.environ.get(
        "QUALITY_T4_STRUCTURAL_OBSERVE_ENABLED", "0",
    ).strip().lower() not in {"1", "true", "yes", "on"}:
        return quality
    from structural_t4_shadow import build_structural_t4_shadow

    output = dict(quality)
    output["t4_structural_shadow"] = build_structural_t4_shadow(segments)
    return output


def _pending_marker_is_stale(quality: dict, *, now=None, max_age_s: int = 900) -> bool:
    """Pure predicate for recovering a crash between DB marker and Redis."""
    from datetime import datetime, timezone
    if not isinstance(quality, dict) or not quality.get("analysis_pending"):
        return False
    try:
        created = datetime.fromisoformat(str(quality.get("analysis_enqueued_at") or ""))
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        now = now or datetime.now(timezone.utc)
        return (now - created).total_seconds() >= max(60, int(max_age_s))
    except (TypeError, ValueError):
        return True


def reconcile_stale_pending_quality_jobs() -> dict:
    """Republish or fail stale pending markers; always schedule the next scan."""
    from database import Job, SessionLocal
    from queue_jobs import (
        _active_rq_job, _init_redis, _mark_transcription_quality_enqueue_failed,
        enqueue_transcription_quality, ensure_quality_pending_reconciler_scheduled,
    )
    from transcription_quality import segments_hash

    scanned = republished = active = failed = expired_proposals = 0
    try:
        _init_redis()
        from queue_jobs import _redis
        db = SessionLocal()
        try:
            from editor import expire_stale_quality_proposals
            expired_proposals = expire_stale_quality_proposals(db)
            if expired_proposals:
                db.commit()
            page_size = max(10, int(os.environ.get(
                "QUALITY_PENDING_SCAN_PAGE_SIZE", "200",
            )))
            pending_flag = Job.transcription_quality[
                "analysis_pending"
            ].as_boolean()
            pending_since = Job.transcription_quality[
                "analysis_enqueued_at"
            ].as_string()
            # Filter pending rows in SQL and stream every page oldest-first.
            # There is deliberately no global LIMIT: a fixed first page can
            # starve later orphaned jobs forever when the table grows.
            rows = db.query(Job).filter(
                Job.transcription_quality.isnot(None),
                pending_flag.is_(True),
            ).order_by(pending_since.asc(), Job.job_id.asc()).yield_per(page_size)
            max_age = int(os.environ.get("QUALITY_PENDING_MAX_AGE_SECONDS", "900"))
            for row in rows:
                quality = dict(row.transcription_quality or {})
                if not _pending_marker_is_stale(quality, max_age_s=max_age):
                    continue
                scanned += 1
                revision = int(row.segments_revision or 0)
                content_hash = segments_hash(row.segments_json or [])
                rq_id = str(quality.get("analysis_job_id") or "")
                if (
                    rq_id and _redis is not None
                    and _active_rq_job(_redis, rq_id) is not None
                ):
                    active += 1
                    continue
                try:
                    result = enqueue_transcription_quality(
                        row.job_id, expected_revision=revision,
                        expected_segments_hash=content_hash,
                        filename=os.path.basename(row.filename or "audio.mp3"),
                        tenant_id=str(row.tenant_id or ""),
                    )
                    if str(result).startswith("transcription-quality:"):
                        republished += 1
                    else:
                        _mark_transcription_quality_enqueue_failed(
                            row.job_id, revision, content_hash, rq_id,
                            str(result).split(":", 1)[0],
                        )
                        failed += 1
                except Exception as exc:
                    _mark_transcription_quality_enqueue_failed(
                        row.job_id, revision, content_hash, rq_id,
                        type(exc).__name__,
                    )
                    failed += 1
        finally:
            db.close()
    finally:
        try:
            ensure_quality_pending_reconciler_scheduled()
        except Exception:
            logger.error("[QUALITY-QUEUE] pending reconciler reschedule failed")
    return {
        "scanned": scanned, "republished": republished,
        "active": active, "failed": failed,
        "expired_proposals": expired_proposals,
    }

_WINDOW_REASON_PRIORITY = {
    "timeline_inversion": 100,
    "invalid_timing_range": 100,
    "duplicate_line_starts": 95,
    "live_structural_disagreement": 90,
    "ctc_short_repeated_motif": 92,
    "event_count": 90,
    "strong_unassigned_vocal_events": 90,
    "provider_timing_collapsed": 95,
    "text_word_cardinality_mismatch": 90,
    "isolated_tail_low_support": 90,
    "low_ctc_timing_confidence": 85,
    "low_asr_content_confidence": 75,
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


def _cardinality_route(structure: dict, existing_count: int) -> dict:
    """Decide whether acoustic cardinality truly excludes the editor count.

    The best HSMM path is only one hypothesis.  Routing a destructive
    structural retry is justified only when the editor count lies outside the
    exact 90% posterior credible set.  Calibration is still pending, so this
    function can request review but never authorize mutation.
    """
    diagnostics = (structure or {}).get("diagnostics") or {}
    best_count = int(
        ((structure or {}).get("best_partition") or {}).get("event_count") or 0
    )
    credible = []
    for value in diagnostics.get("cardinality_credible_counts_90") or []:
        try:
            credible.append(int(value))
        except (TypeError, ValueError):
            continue
    posterior = (structure or {}).get("cardinality_posterior") or {}
    if not credible and posterior:
        ranked = []
        for key, value in posterior.items():
            try:
                ranked.append((int(key), float(value)))
            except (TypeError, ValueError):
                continue
        cumulative = 0.0
        for count, probability in sorted(ranked, key=lambda item: (-item[1], item[0])):
            credible.append(count)
            cumulative += probability
            if cumulative >= .90:
                break
    excluded = bool(credible and existing_count not in credible)
    return {
        "route_disagreement": excluded,
        "best_count": best_count,
        "existing_count": int(existing_count),
        "credible_counts_90": sorted(set(credible)),
        "ambiguous_best_path": bool(
            best_count and best_count != existing_count and not excluded
        ),
        "automatic_apply_allowed": False,
    }


def _max_windows() -> int:
    try:
        configured = int(os.environ.get("TRANSCRIPTION_QUALITY_MAX_WINDOWS", "4"))
    except (TypeError, ValueError):
        configured = 4
    # High-gap live recordings can legitimately contain dozens of review
    # windows.  The downstream billed-audio and wall-clock budgets remain the
    # authoritative cost/safety limits; this cap only guards malformed input.
    return max(1, min(64, configured))


def _release() -> str:
    return str(
        os.environ.get("RELEASE")
        or os.environ.get("RAILWAY_GIT_COMMIT_SHA")
        or "unknown"
    )[:64]


def _snapshot(job_id: str):
    from database import EditorDocument, Job, SessionLocal
    from sqlalchemy import func

    db = SessionLocal()
    try:
        row = db.query(Job).filter(Job.job_id == job_id).first()
        if row is None:
            return None
        document = db.query(EditorDocument).filter(
            EditorDocument.job_id == row.job_id,
            EditorDocument.tenant_id == row.tenant_id,
        ).first()
        # Bounded same-artist vocabulary is a decoding hint, never evidence.
        # It can help names/slang already present in this tenant's approved
        # catalogue, but a suggestion still requires independent audio
        # consensus before it reaches the operator.
        lexicon_terms: list[str] = []
        lexicon_enabled = os.environ.get(
            "ARTIST_LEXICON_RAG_ENABLED", "0",
        ).strip().lower() in {"1", "true", "yes", "on"}
        artist = str(row.artist or "").strip()
        if artist and lexicon_enabled:
            catalog_rows = db.query(Job.segments_json).filter(
                Job.tenant_id == row.tenant_id,
                Job.job_id != row.job_id,
                Job.status == "done",
                func.lower(Job.artist) == artist.lower(),
                Job.segments_json.isnot(None),
            ).order_by(Job.approved_at.desc()).limit(12).all()
            seen = set()
            for (catalog_segments,) in catalog_rows:
                for segment in catalog_segments or []:
                    if not isinstance(segment, dict):
                        continue
                    for token in re.findall(
                        r"[A-Za-zÀ-ÖØ-öø-ÿÑñ0-9][A-Za-zÀ-ÖØ-öø-ÿÑñ0-9'’-]{2,}",
                        str(segment.get("text") or ""),
                    ):
                        folded = unicodedata.normalize("NFKC", token).casefold()
                        if folded in seen:
                            continue
                        seen.add(folded)
                        lexicon_terms.append(token)
                        if len(lexicon_terms) >= 120:
                            break
                    if len(lexicon_terms) >= 120:
                        break
                if len(lexicon_terms) >= 120:
                    break
        return {
            "revision": int(row.segments_revision or 0),
            "segments": [dict(item) for item in (row.segments_json or [])],
            # The immutable machine snapshot carries the LoRA word stream
            # after the worker removes transport-only keys.  Quality replay
            # must be able to compare the attested family with/without it.
            "machine_evidence": (
                dict(document.machine_evidence)
                if document is not None
                and isinstance(document.machine_evidence, dict)
                else None
            ),
            "quality": dict(row.transcription_quality or {}),
            "input_r2_key": row.input_r2_key,
            "audio_revision": int(row.audio_revision or 0),
            "audio_sha256": str(row.input_audio_sha256 or ""),
            "active_quality_attempt_id": str(row.active_quality_attempt_id or ""),
            "filename": row.filename,
            "artist_lexicon_prompt": (
                "Vocabulario posible del catálogo del mismo artista: "
                + ", ".join(lexicon_terms)
            )[:850] if lexicon_terms else "",
            "artist_lexicon_terms": len(lexicon_terms),
            "artist_lexicon_enabled": lexicon_enabled,
        }
    finally:
        db.close()


def _attested_asr_context(
    segments: list[dict], machine_evidence: dict | None = None,
) -> dict:
    """Recover provider-family witnesses without trusting segment labels.

    New transcriptions persist word timing plus an HMAC-attested provenance
    row on each segment.  Grouping by the attested correlated family lets the
    targeted retry compare its fresh recognizer family with the original ASR
    while keeping reference/catalog text out of the witness streams.
    """
    from evidence_contracts import (
        freeze_provider_output,
        verify_content_provenance_attestation,
    )

    grouped: dict[str, list[dict]] = {}
    for segment in segments or []:
        if not isinstance(segment, dict):
            continue
        provenance = segment.get("content_provenance")
        if (
            not isinstance(provenance, dict)
            or str(provenance.get("role") or "") != "asr_witness"
            or not verify_content_provenance_attestation(provenance)
        ):
            continue
        lineage = provenance.get("lineage") or {}
        family = str(lineage.get("correlated_family") or "").strip()
        if not family:
            continue
        provider = segment.get("provider_evidence")
        if not isinstance(provider, dict):
            continue
        frozen_row = provider.get("frozen_provider_output")
        if not isinstance(frozen_row, dict):
            continue
        declared_hashes = (
            str(provenance.get("raw_output_sha256") or ""),
            str(provider.get("raw_output_sha256") or ""),
            str(frozen_row.get("output_sha256") or ""),
        )
        if any(not re.fullmatch(r"[0-9a-f]{64}", value) for value in declared_hashes):
            continue
        actual_hash = freeze_provider_output(provider).output_sha256
        if any(not hmac.compare_digest(actual_hash, value) for value in declared_hashes):
            continue
        # Consume only the provider snapshot whose words are covered by the
        # frozen output digest and the provenance HMAC.  The visible segment
        # words are editable and therefore cannot be recognition evidence.
        words = provider.get("words")
        if not isinstance(words, list):
            continue
        for word in words:
            if not isinstance(word, dict) or not str(word.get("word") or "").strip():
                continue
            try:
                start, end = float(word.get("start")), float(word.get("end"))
            except (TypeError, ValueError):
                continue
            if start < 0 or end <= start:
                continue
            grouped.setdefault(family, []).append({
                **word, "start": start, "end": end,
            })
    ranked = sorted(
        grouped.items(), key=lambda item: (-len(item[1]), item[0]),
    )
    primary = ranked[0] if ranked else ("", [])
    independent = ranked[1] if len(ranked) > 1 else ("", [])
    context = {
        "_asr_words": primary[1],
        "_primary_asr_family": primary[0],
        "_independent_asr_words": independent[1],
        "_independent_asr_family": independent[0],
    }
    # LoRA is persisted as a private hypothesis, not on editable segment
    # rows.  Accept only the exact attested family and a matching snapshot
    # hash; user-edited text can therefore never manufacture a witness.
    if isinstance(machine_evidence, dict):
        from machine_evidence import snapshot_hash

        for hypothesis in machine_evidence.get("hypotheses_by_family") or []:
            if not isinstance(hypothesis, dict):
                continue
            family = str(hypothesis.get("family") or "").strip()
            if family != "openai_whisper_large_v3_turbo_lora_v1":
                continue
            events = hypothesis.get("events")
            if (
                hypothesis.get("kind") != "word_stream"
                or not isinstance(events, list)
                or hypothesis.get("events_sha256") != snapshot_hash(events)
            ):
                continue
            words = [item for item in events if isinstance(item, dict)]
            context["_lora_asr_words"] = words
            context["_lora_asr_family"] = family
            break
    return context


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


def _safe_audio_identity_metrics(
    audio_sha256: str, stem_sha256: str,
) -> dict[str, str | None]:
    """Pseudonymize stable audio identities before persisting telemetry."""
    from evidence_contracts import privacy_fingerprint

    return {
        "audio_fingerprint": privacy_fingerprint(
            "quality-source-audio", audio_sha256,
        ) if re.fullmatch(r"[0-9a-f]{64}", str(audio_sha256 or "")) else None,
        "stem_fingerprint": privacy_fingerprint(
            "quality-vocal-stem", stem_sha256,
        ) if re.fullmatch(r"[0-9a-f]{64}", str(stem_sha256 or "")) else None,
    }


def _persist_if_current(job_id: str, expected_revision: int,
                        expected_hash: str, quality: dict, *,
                        expected_audio_revision: int | None = None,
                        expected_audio_sha256: str = "",
                        analysis_attempt_id: str = "",
                        quality_proposal: dict | None = None,
                        quality_observation: dict | None = None,
                        operator_proposal: dict | None = None) -> bool:
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
        audio_revision = int(row.audio_revision or 0)
        audio_sha256 = str(row.input_audio_sha256 or "")
        attempt_id = str(row.active_quality_attempt_id or "")
        if (
            revision != int(expected_revision)
            or current_hash != expected_hash
            or (
                expected_audio_revision is not None
                and audio_revision != int(expected_audio_revision)
            )
            or (expected_audio_sha256 and audio_sha256 != expected_audio_sha256)
            or (analysis_attempt_id and attempt_id != analysis_attempt_id)
        ):
            logger.info(
                "[QUALITY-OCC] stale result discarded job=%s "
                "segment_revision=%s/%s segment_identity_match=%s "
                "audio_revision=%s/%s audio_identity_match=%s "
                "attempt_identity_match=%s",
                job_id, expected_revision, revision,
                hmac.compare_digest(current_hash, expected_hash),
                expected_audio_revision, audio_revision,
                hmac.compare_digest(audio_sha256, expected_audio_sha256),
                hmac.compare_digest(attempt_id, analysis_attempt_id),
            )
            return False
        previous = dict(row.transcription_quality or {})
        ack = previous.get("acknowledgement")
        quality = dict(quality)
        # The quality replay is analytical and may finish after the
        # transcription worker has attached the batch reference contract (or
        # even after a reviewer has approved it).  Replacing the JSON blob
        # must not erase those durable, operator-facing gates.  They are
        # produced outside the replay and are therefore authoritative over
        # any same-named field in the analytical result.
        for durable_key in (
            "reference_hypothesis",
            "pre_background_approval",
        ):
            if durable_key in previous:
                quality[durable_key] = previous[durable_key]
        quality["analysis_status"] = (
            "failed" if quality.get("decision") == "retry_failed" else "complete"
        )
        quality["analysis_pending"] = False
        if previous.get("analysis_job_id"):
            quality["analysis_job_id"] = str(previous["analysis_job_id"])
        quality["evaluated_revision"] = revision
        quality["segments_hash"] = current_hash
        quality["audio_revision"] = audio_revision
        quality["audio_sha256"] = audio_sha256
        quality["analysis_attempt_id"] = analysis_attempt_id or attempt_id
        try:
            from quality_learning_model import shadow_prediction_for_quality
            quality["learning_shadow"] = shadow_prediction_for_quality(
                quality, str(row.timing_source or "unknown"),
            )
        except Exception:
            logger.warning(
                "[QUALITY-LEARNING] shadow prediction unavailable job=%s",
                job_id,
            )
            quality["learning_shadow"] = {
                "available": False, "reason": "prediction_failed",
                "mutated_segments": False,
            }
        if operator_proposal:
            try:
                from editor import persist_operator_review_proposal_if_current
                quality["operator_suggestions_persisted"] = bool(
                    persist_operator_review_proposal_if_current(
                        db, job_id=job_id,
                        expected_revision=expected_revision,
                        expected_segments_hash=expected_hash,
                        expected_audio_revision=audio_revision,
                        expected_audio_sha256=audio_sha256,
                        proposal=operator_proposal,
                    )
                )
            except Exception:
                logger.error(
                    "[OPERATOR-SUGGESTION] fail-closed persistence job=%s",
                    job_id,
                )
                quality["operator_suggestions_persisted"] = False
        elif quality_proposal:
            try:
                from editor import persist_quality_proposal_if_current
                quality["review_proposal_persisted"] = bool(
                    persist_quality_proposal_if_current(
                        db, job_id=job_id,
                        expected_revision=expected_revision,
                        expected_segments_hash=expected_hash,
                        expected_audio_revision=audio_revision,
                        expected_audio_sha256=audio_sha256,
                        proposal=quality_proposal,
                    )
                )
            except Exception:
                logger.error(
                    "[QUALITY-PROPOSAL] fail-closed persistence job=%s", job_id,
                )
                quality["review_proposal_persisted"] = False
        elif quality_observation:
            try:
                from editor import persist_quality_observation_if_current
                quality["review_observation_persisted"] = bool(
                    persist_quality_observation_if_current(
                        db, job_id=job_id,
                        expected_revision=expected_revision,
                        expected_segments_hash=expected_hash,
                        expected_audio_revision=audio_revision,
                        expected_audio_sha256=audio_sha256,
                        proposal=quality_observation,
                    )
                )
            except Exception:
                logger.error(
                    "[QUALITY-OBSERVATION] fail-closed persistence job=%s", job_id,
                )
                quality["review_observation_persisted"] = False
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
        row.active_quality_attempt_id = None
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
        "performance_graph_id": structure.get("performance_graph_id"),
        "subevents": structure.get("subevents") or [],
        "performance_boundaries": structure.get("performance_boundaries") or [],
        "diagnostics": structure.get("diagnostics") or {},
        "boundary_count": len(structure.get("boundaries") or []),
        "n_best_count": len(structure.get("n_best") or []),
    }


_ANALYTICAL_KEYS = frozenset({
    # Containers and identity-safe references.
    "windows", "window", "window_id", "id", "parent_window_id",
    "analysis_windows", "acoustic_structure", "content_mapping", "events",
    "editorial_content_route", "omission_content_route",
    "subevents", "performance_boundaries", "best_partition", "n_best",
    "cardinality_posterior", "motif_groups", "self_similarity", "diagnostics",
    "phonetic_evidence", "phonetic_candidates", "model_identity",
    "evidence_lineage", "parent_coverage", "resolved_window_ids",
    "review_proposal", "blockers", "reasons", "reason", "declined",
    "structural_hybrid_diagnostics", "current_segments", "proposed_segments",
    # Booleans, counters and bounded measurements.
    "accepted", "complete", "attempted", "failed", "blocked", "suggested",
    "allow_lexical_ranking", "safe_for_auto_insert", "review_required",
    "applied", "mutated_segments", "phonetic_verified",
    "v6_legacy_mutation_blocked",
    "independent_content_verified", "acoustic_crowd_evidence", "cache_hit",
    "windows_processed", "windows_total", "windows_skipped", "windows_truncated",
    "content_gate_declined", "omission_only",
    "windows_considered", "windows_tiled", "windows_resolved", "window_count",
    "parent_windows_total", "parent_windows_incomplete", "boundary_count",
    "n_best_count", "event_count", "word_count", "text_count", "text_length",
    "text_present", "provider_attempts", "asr_calls", "gemini_calls",
    "residual_asr_calls", "slowed_asr_calls", "demucs_attempts", "cache_hits",
    "cache_misses", "lines_suggested", "lines_replaced", "lines_inserted",
    "targets_removed", "structural_repairs", "structural_events",
    "structural_hybrid_attempts", "structural_hybrid_accepts",
    "strong_unassigned_events", "unassigned_events", "viable_hypotheses",
    "authorized_windows", "candidates", "invalid_candidates",
    # Paired LoRA-v1 shadow attribution (with/without the additional family).
    # Keep these counters in the analytical allow-list so the quality row can
    # be aggregated over the first 30–50 real songs.
    "lora_shadow", "lora_contributed_lines", "new_consensus_lines",
    "lost_consensus_lines", "with_consensus", "without_consensus",
    "comparisons", "enabled",
    "start", "end", "core_start", "core_end", "duration", "margin",
    "phase_margin", "max_phase_delta", "median_phase_delta", "starts",
    "raw_score", "score", "confidence", "probability", "coverage",
    "audio_seconds_billed", "submitted_audio_seconds", "gemini_audio_seconds",
    "residual_audio_seconds", "slowed_audio_seconds", "api_cost_usd",
    "estimated_openai_asr_cost_usd", "estimated_gemini_cost_usd",
    "cost_complete", "stem_cache_hit", "calibration_id", "schema",
    "policy_version", "kind", "taxonomy", "composition", "source", "status",
    "policy_id", "content_type", "display",
    "failure_reason", "selected_candidate_id", "evidence_sha256",
    "model_revision", "stem_sha256", "mix_sha256", "cache_ref",
    "evidence_fingerprint", "stem_fingerprint", "mix_fingerprint",
})

_ANALYTICAL_RAW_HASH_KEYS = frozenset({
    "evidence_sha256", "stem_sha256", "mix_sha256",
})

_ANALYTICAL_STRING_VALUES = frozenset({
    "lyrics-quality-v6", "lyrics-quality-v6-diagnostic-v1",
    "lyrics-quality-v6-review-proposal-v1", "review_proposal",
    "review_proposal_window", "diagnostic_finding", "unknown", "accepted",
    "declined", "pending", "complete", "failed", "retry_failed",
    "review_required", "transcription_quality", "performance_graph_content_lattice",
    "progressive_asr_consensus", "ctc-phonetic-evidence-v1",
    "cross_occurrence_content_consensus", "recurrence_content_disagreement",
    "SUNG_LEAD", "SUNG_CROWD", "SPEECH", "NONLEXICAL", "METADATA",
    "CROWD_NOISE", "UNKNOWN", "lexical", "vocalization", "sustained",
    "none", "speech", "lexical_candidate", "melodic_vocalization", "ambiguous",
    "normal", "parenthesize", "do_not_show", "review",
    "no_acoustic_events", "long_melodic_interjection",
    "short_melodic_interjection", "speech_compositionality_unknown",
    "independent_text_consensus_required", "mixed_or_unknown_acoustic_events",
    "rotor-umg-display-policy-v2", "acoustic-editorial-route-v1",
    "omission-content-route-v1", "content_gate_abstention",
    "acoustic_content_supports_lexical_ranking", "not_an_omission_only_window",
    "lexical_plus_vocalization", "source_audio_demucs",
    *_WINDOW_REASON_PRIORITY.keys(),
    "acoustic_cardinality_disagreement", "quality_windows_unprocessed",
    "quality_windows_truncated", "cost_budget", "cost_budget_base_views",
    "cost_budget_residual_view", "cost_budget_slow_view", "cost_budget_gemini_view",
    "stage_deadline", "stage_deadline_after_stem", "invalid_unbounded_tile",
    "proposal_kill_switch_off", "pinned_artifacts_missing", "invalid_contract",
    "invalid_candidate_contract", "proposal_current_segments_mismatch",
    "proposal_certification_conflict", "runtime_identity_mismatch",
    "input_r2_key_missing", "source_audio_changed", "vocal_stem_unavailable",
    "provider_timeout", "redacted",
})


def _safe_analytical_string(key: str, value) -> str:
    raw = str(value or "").strip()
    if raw in _ANALYTICAL_STRING_VALUES:
        return raw
    if key.endswith("_fingerprint") and re.fullmatch(
        r"hmac-sha256:v1:[a-z0-9_.-]{1,32}:[0-9a-f]{64}", raw,
    ):
        return raw
    if key in {"id", "window_id", "parent_window_id", "selected_candidate_id"} and re.fullmatch(
        r"(?:qw_[0-9a-f]{16}|quality-window-[0-9]+|[0-9a-f]{16,64})", raw,
    ):
        return raw
    if key.endswith("sha256") and re.fullmatch(r"[0-9a-f]{40,64}", raw):
        return raw
    if key == "model_revision" and re.fullmatch(r"[0-9a-f]{40,64}", raw):
        return raw
    return "redacted"


def _sanitize_analytical_evidence(value, *, _key: str = ""):
    """Project diagnostics through a recursive allow-list.

    Unknown keys are discarded rather than searched for a handful of forbidden
    names.  Free-form strings under known keys are reduced to a fixed enum or a
    non-content identifier.  This makes future provider payload expansion safe
    by default.
    """
    if isinstance(value, dict):
        sanitized = {}
        for key, item in value.items():
            if key == "text":
                sanitized["text_present"] = bool(str(item or "").strip())
                sanitized["text_length"] = len(str(item or ""))
                continue
            if key == "texts":
                sanitized["text_count"] = len(item or []) if isinstance(item, list) else 0
                continue
            if key == "words":
                sanitized["word_count"] = len(item or []) if isinstance(item, list) else 0
                continue
            if key in _ANALYTICAL_RAW_HASH_KEYS:
                raw_hash = str(item or "").strip().lower()
                if re.fullmatch(r"[0-9a-f]{64}", raw_hash):
                    from evidence_contracts import privacy_fingerprint
                    fingerprint = privacy_fingerprint(
                        f"quality-analytics:{key}", raw_hash,
                    )
                    if fingerprint:
                        sanitized[key.removesuffix("_sha256") + "_fingerprint"] = (
                            fingerprint
                        )
                continue
            if key not in _ANALYTICAL_KEYS:
                continue
            sanitized[key] = _sanitize_analytical_evidence(item, _key=key)
        return sanitized
    if isinstance(value, list):
        return [
            _sanitize_analytical_evidence(item, _key=_key)
            for item in value
            if isinstance(item, (dict, list, bool, int, float))
            or (isinstance(item, str) and _safe_analytical_string(_key, item) != "redacted")
        ]
    if isinstance(value, str):
        return _safe_analytical_string(_key, value)
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, (int, float)):
        try:
            return value if math.isfinite(float(value)) else None
        except (OverflowError, TypeError, ValueError):
            return None
    return None


def _build_review_proposal(segments: list[dict], raw_windows: list[dict],
                           coverage: dict[str, dict], *,
                           observation_only: bool = False) -> tuple[dict | None, dict]:
    """Build a typed tenant-scoped proposal; return only text-free telemetry."""
    from quality_v6_calibration import runtime_review_proposal_authorization
    from quality_v6_contracts import (
        POLICY_VERSION, PROPOSAL_WINDOW_SCHEMA, REVIEW_PROPOSAL_SCHEMA,
        ReviewProposal, ReviewProposalCandidate,
    )

    grouped: dict[str, dict] = {}
    invalid_candidates = 0
    for raw in raw_windows or []:
        if not isinstance(raw, dict):
            invalid_candidates += 1
            continue
        try:
            candidate = ReviewProposalCandidate.from_mapping(raw)
        except (TypeError, ValueError):
            invalid_candidates += 1
            continue
        parent_id = candidate.parent_window_id
        if not parent_id or not (coverage.get(parent_id) or {}).get("complete"):
            continue
        group = grouped.setdefault(parent_id, {
            "kind": "review_proposal_window",
            "schema": PROPOSAL_WINDOW_SCHEMA,
            "id": parent_id, "start": candidate.start,
            "end": candidate.end, "reasons": set(),
            "current_segments": [], "proposed_segments": [],
            "certification": candidate.certification,
            "certification_conflict": False,
            "source_families": set(),
        })
        if group["certification"] != candidate.certification:
            group["certification_conflict"] = True
        group["start"] = min(group["start"], candidate.start)
        group["end"] = max(group["end"], candidate.end)
        group["reasons"].update(candidate.reasons)
        group["current_segments"].extend(dict(item) for item in candidate.current_segments)
        group["proposed_segments"].extend(dict(item) for item in candidate.proposed_segments)
        if isinstance(raw.get("source_families"), (list, tuple)):
            group["source_families"].update(
                str(item).strip() for item in raw["source_families"]
                if str(item).strip()
            )

    def key(item: dict) -> tuple:
        return (
            str(item.get("_id") or item.get("id") or ""),
            round(float(item.get("start") or 0), 4),
            round(float(item.get("end") or 0), 4),
            str(item.get("text") or ""),
        )

    windows = []
    authorization_blockers: set[str] = set()
    current_keys = {key(item) for item in segments if isinstance(item, dict)}
    for group in grouped.values():
        if group.pop("certification_conflict", False):
            authorization_blockers.add("proposal_certification_conflict")
            continue
        # Bind every declared replacement row to the exact current editor
        # snapshot supplied to this builder. A malformed/stale candidate may
        # never turn an intended replacement into an overlapping insertion.
        if any(key(item) not in current_keys for item in group["current_segments"]):
            authorization_blockers.add("proposal_current_segments_mismatch")
            continue
        certification = group.pop("certification", None)
        if observation_only:
            from consensus_review_certificate import canonical_source_family
            independent_families = {
                canonical_source_family(item) for item in group["source_families"]
                if canonical_source_family(item)
            }
            if len(independent_families) < 2:
                authorization_blockers.add("independent_source_family_missing")
                continue
            group["source_families"] = sorted(independent_families)
        else:
            authorization = runtime_review_proposal_authorization(certification)
            authorization_blockers.update(authorization.get("blockers") or [])
            if not authorization.get("authorized"):
                continue
            # The signed production proposal contract intentionally contains
            # no observational metadata.
            group.pop("source_families", None)
        for field in ("current_segments", "proposed_segments"):
            unique = {}
            for item in group[field]:
                unique[key(item)] = item
            group[field] = sorted(unique.values(), key=lambda item: (
                float(item.get("start") or 0), float(item.get("end") or 0),
            ))
        group["reasons"] = sorted(group["reasons"])
        windows.append(group)
    telemetry = {
        "candidates": len(grouped), "authorized_windows": len(windows),
        "blocked": bool(grouped) and not bool(windows),
        "blockers": sorted(authorization_blockers),
        "invalid_candidates": invalid_candidates,
    }
    if not windows:
        return None, telemetry
    try:
        proposal = ReviewProposal.from_mapping({
            "kind": "review_proposal",
            "schema": REVIEW_PROPOSAL_SCHEMA,
            "policy_version": POLICY_VERSION,
            "review_only": True,
            "windows": windows,
        }).to_dict()
        if observation_only:
            metadata = {
                str(item["id"]): list(item.get("source_families") or [])
                for item in windows
            }
            proposal["observation_only"] = True
            proposal["certificate_policy_version"] = (
                "independent-consensus-review-policy-v1"
            )
            for item in proposal["windows"]:
                item["source_families"] = metadata.get(str(item["id"]), [])
    except (TypeError, ValueError):
        telemetry["blocked"] = True
        telemetry["blockers"] = sorted(set(telemetry["blockers"] + ["invalid_contract"]))
        return None, telemetry
    return proposal, telemetry


def _normalise_lyric(text: str) -> str:
    value = unicodedata.normalize("NFKC", str(text or "")).casefold()
    return " ".join(re.findall(r"\w+", value, flags=re.UNICODE))


def _valid_independent_content_attestation(
    mapping: dict, *, expected_window: list | tuple,
    expected_stem_sha256: str, expected_mix_sha256: str,
) -> bool:
    attestation = mapping.get("independent_content_attestation")
    if not isinstance(attestation, dict):
        return False
    signature = str(attestation.get("signature_hmac_sha256") or "")
    payload = {
        key: value for key, value in attestation.items()
        if key != "signature_hmac_sha256"
    }
    families = set(payload.get("families") or [])
    from line_evidence import canonical_content_sequence

    content_sequence = canonical_content_sequence(mapping.get("events") or [])
    content_sha256 = hashlib.sha256(json.dumps(
        content_sequence, ensure_ascii=False, separators=(",", ":"),
    ).encode("utf-8")).hexdigest()
    try:
        actual_window = [
            round(float(value), 3) for value in (payload.get("window") or [])
        ]
        expected_window = [
            round(float(value), 3) for value in (expected_window or [])
        ]
    except (TypeError, ValueError):
        return False
    if (
        payload.get("schema") != "independent-content-consensus-v1"
        or len(families) < 2
        or "gemini_audio" not in families
        or any("ctc" in str(family).lower() for family in families)
        or not re.fullmatch(r"[0-9a-f]{64}", str(payload.get("content_sha256") or ""))
        or not content_sequence
        or any(not value for value in content_sequence)
        or not hmac.compare_digest(
            str(payload.get("content_sha256") or ""), content_sha256,
        )
        or payload.get("selected_candidate_id") is None
        or payload.get("selected_candidate_id") != mapping.get("selected_candidate_id")
        or len(expected_window) != 2
        or actual_window != expected_window
        or not re.fullmatch(r"[0-9a-f]{64}", str(expected_stem_sha256 or ""))
        or not re.fullmatch(r"[0-9a-f]{64}", str(expected_mix_sha256 or ""))
        or not hmac.compare_digest(
            str(payload.get("stem_sha256") or ""), str(expected_stem_sha256),
        )
        or not hmac.compare_digest(
            str(payload.get("mix_sha256") or ""), str(expected_mix_sha256),
        )
        or not re.fullmatch(r"[0-9a-f]{64}", signature)
    ):
        return False
    from evidence_contracts import strong_hmac_secret_bytes

    secret = strong_hmac_secret_bytes(
        os.environ.get("QUALITY_CONTENT_ATTESTATION_KEY")
        or os.environ.get("QUALITY_LEARNING_HMAC_KEY") or ""
    )
    if secret is None:
        return False
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")
    evidence_sha = hashlib.sha256(encoded).hexdigest()
    expected_signature = hmac.new(
        secret, encoded, hashlib.sha256,
    ).hexdigest()
    return bool(
        hmac.compare_digest(signature, expected_signature)
        and hmac.compare_digest(
            str(mapping.get("independent_content_evidence_sha256") or ""),
            evidence_sha,
        )
    )


def _confirmed_windows(segments: list[dict], windows: list[dict],
                       diagnostics: list[dict], *,
                       expected_stem_sha256: str,
                       expected_mix_sha256: str) -> tuple[list[dict], list[dict]]:
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
                # CTC aligns supplied text and therefore cannot certify that
                # the text exists. Resolution requires a separately attested
                # content witness (provider/reference/operator), not stem+mix
                # views of the same CTC family.
                or not mapping.get("independent_content_verified")
                or not re.fullmatch(
                    r"[0-9a-f]{64}",
                    str(mapping.get("independent_content_evidence_sha256") or ""),
                )
                or not _valid_independent_content_attestation(
                    mapping, expected_window=span,
                    expected_stem_sha256=expected_stem_sha256,
                    expected_mix_sha256=expected_mix_sha256,
                )
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
                                  filename: str = "",
                                  expected_audio_revision: int | None = None,
                                  expected_audio_sha256: str = "",
                                  analysis_attempt_id: str = "",
                                  quality_runtime_token: str = "") -> dict:
    """RQ entry point. Analyze unsafe windows and persist evidence only."""
    from observability import set_job_log_context
    from transcription_quality import calibration_identity, evaluate, segments_hash

    set_job_log_context(job_id)
    if quality_runtime_token:
        from queue_jobs import _transcription_quality_runtime_token
        if _transcription_quality_runtime_token() != quality_runtime_token:
            return {"status": "discarded", "reason": "runtime_identity_mismatch"}
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
        or (
            expected_audio_revision is not None
            and snapshot["audio_revision"] != int(expected_audio_revision)
        )
        or (
            expected_audio_sha256
            and snapshot["audio_sha256"] != expected_audio_sha256
        )
        or (
            analysis_attempt_id
            and snapshot["active_quality_attempt_id"] != analysis_attempt_id
        )
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
            is_live=bool((quality_before.get("metrics") or {}).get("is_live")),
            retry_stats={
                "attempted": True, "failed": True,
                "failure_reason": "input_r2_key_missing",
                "queue": "transcription_quality", "mutated_segments": False,
            },
        )
        failed["analysis_status"] = "failed"
        failed["analysis_pending"] = False
        _persist_if_current(
            job_id, expected_revision, expected_segments_hash, failed,
            expected_audio_revision=expected_audio_revision,
            expected_audio_sha256=expected_audio_sha256,
            analysis_attempt_id=analysis_attempt_id,
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
            if expected_audio_sha256 and audio_hash != expected_audio_sha256:
                return {"status": "discarded", "reason": "source_audio_changed"}
            from pipeline import _ffprobe_duration
            audio_duration = _ffprobe_duration(audio_path)
            if audio_duration is None or float(audio_duration) <= 0:
                raise RuntimeError("source_audio_duration_unavailable")
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
            max_windows = _max_windows()
            prioritized_windows = [
                {**item, "id": item.get("id") or f"quality-window-{index}"}
                for index, item in enumerate(_prioritize_windows(windows))
            ]
            windows = prioritized_windows
            from quality_windows import parent_coverage, tile_unsafe_windows
            from lyric_content_policy import (
                classify_acoustic_window, route_omission_window,
            )
            all_tiles = tile_unsafe_windows(
                prioritized_windows, core_seconds=24.0, context_seconds=3.0,
                audio_duration=float(audio_duration),
            )
            truncated_parent_ids = {
                str(tile.get("parent_window_id") or tile.get("id"))
                for tile in all_tiles if tile.get("analysis_truncated")
            }
            selected_tiles = all_tiles[:max_windows]
            for bounded in selected_tiles:
                structure, hit = _cached_structure(
                    cache, audio_hash, stem_hash, stem_path, audio_path, bounded,
                )
                core_start = float(bounded.get("core_start", bounded.get("start", 0)))
                core_end = float(bounded.get("core_end", bounded.get("end", core_start)))
                existing_count = sum(
                    1 for segment in snapshot["segments"]
                    if isinstance(segment, dict)
                    and float(segment.get("end") or 0) > core_start
                    and float(segment.get("start") or 0) < core_end
                )
                cardinality = _cardinality_route(structure, existing_count)
                bounded["acoustic_cardinality"] = cardinality
                if cardinality["route_disagreement"]:
                    reasons = set(bounded.get("reasons") or [])
                    reasons.add("acoustic_cardinality_disagreement")
                    bounded["reasons"] = sorted(reasons)
                    bounded["acoustic_phrase_count"] = cardinality["best_count"]
                    bounded["editor_segment_count"] = existing_count
                best_events = list(
                    (structure.get("best_partition") or {}).get("events") or []
                )
                bounded["acoustic_crowd_evidence"] = any(
                    str(event.get("taxonomy") or "") == "SUNG_CROWD"
                    or float(
                        (event.get("type_posterior") or {}).get(
                            "crowd_or_overlap", 0.0,
                        ) or 0.0
                    ) >= .35
                    for event in best_events if isinstance(event, dict)
                )
                cache_hits += int(hit)
                content_route = classify_acoustic_window(structure)
                omission_route = route_omission_window(bounded, content_route)
                bounded["editorial_content_route"] = content_route
                bounded["omission_content_route"] = omission_route
                analyses.append({
                    "window": bounded,
                    "structure": _structure_summary(structure),
                    "editorial_content_route": content_route,
                    "omission_content_route": omission_route,
                })
            # selected_tiles contains the same dictionaries mutated above;
            # provider retries now consume acoustic disagreement directly.

            retry_stats = {
                "attempted": True, "queue": "transcription_quality",
                "windows_processed": len(analyses),
                "windows_total": len(all_tiles),
                "parent_windows_total": len(windows),
                "windows_skipped": max(0, len(all_tiles) - len(analyses)),
                "windows_truncated": len(truncated_parent_ids),
                "cache_hits": cache_hits,
                "cache_misses": len(analyses) - cache_hits,
                "mutated_segments": False,
                "provider_attempts": 0,
                "audio_seconds_billed": 0.0,
                "stem_cache_hit": stem_cache_hit,
                "demucs_attempts": 0 if stem_cache_hit else 1,
            }
            lexical_retry_tiles = [
                item for item in selected_tiles
                if (item.get("omission_content_route") or {}).get(
                    "allow_lexical_ranking"
                ) is not False
            ]
            retry_stats["content_gate_declined"] = (
                len(selected_tiles) - len(lexical_retry_tiles)
            )
            raw_proposal_windows = []
            timing_review_candidates = []
            timing_review_report = {
                "enabled": False, "proposal_count": 0,
                "automatic_apply_allowed": False,
            }
            spanish_orthography_candidates = []
            spanish_orthography_report = {
                "enabled": False, "finding_count": 0, "candidate_count": 0,
                "automatic_apply_allowed": False,
            }
            # The provider/content retry remains separately kill-switchable.
            # Raw rows stay in memory until the typed, signed review-proposal
            # gate accepts them; global quality analytics receive only counts.
            if os.environ.get("TARGETED_CONSENSUS_ENABLED", "0").lower() in {
                "1", "true", "yes", "on",
            }:
                from targeted_consensus import reprocess
                asr_context = _attested_asr_context(
                    snapshot["segments"], snapshot.get("machine_evidence"),
                )
                if snapshot.get("artist_lexicon_prompt"):
                    asr_context["_artist_lexicon_prompt"] = snapshot[
                        "artist_lexicon_prompt"
                    ]
                _ignored, provider_stats = reprocess(
                    {"segments": snapshot["segments"], **asr_context},
                    audio_path, lexical_retry_tiles,
                    job_id=job_id, stem_path=stem_path,
                )
                raw_proposal_windows = list(
                    provider_stats.pop("quality_proposal_windows", []) or []
                )
                retry_stats.update(provider_stats)
                retry_stats["artist_lexicon_terms"] = int(
                    snapshot.get("artist_lexicon_terms") or 0
                )
                retry_stats["artist_lexicon_enabled"] = bool(
                    snapshot.get("artist_lexicon_enabled")
                )
                retry_stats["mutated_segments"] = False

            if os.environ.get(
                "QUALITY_OPERATOR_SUGGESTIONS_ENABLED", "0",
            ).strip().lower() in {"1", "true", "yes", "on"}:
                try:
                    from spanish_orthography import analyze_spanish_orthography

                    raw_spanish_report = analyze_spanish_orthography(
                        snapshot["segments"],
                    )
                    spanish_orthography_candidates = list(
                        raw_spanish_report.pop("candidates", []) or []
                    )
                    # Persist raw lyric text only in the revision-bound editor
                    # proposal. Global quality telemetry keeps counts/policy.
                    raw_spanish_report.pop("findings", None)
                    spanish_orthography_report = {
                        **raw_spanish_report, "enabled": True,
                    }
                except Exception as exc:
                    logger.warning(
                        "[SPANISH-ORTHOGRAPHY] fail-closed job=%s error=%s",
                        job_id, type(exc).__name__,
                    )
                    spanish_orthography_candidates = []
                    spanish_orthography_report = {
                        "enabled": True, "finding_count": 0,
                        "candidate_count": 0, "failure": type(exc).__name__,
                        "automatic_apply_allowed": False,
                    }
                try:
                    from pathlib import Path
                    from timing_review_suggestions import (
                        build_timing_review_candidates, load_acoustic_track,
                    )

                    acoustic_track = load_acoustic_track(Path(stem_path))
                    timing_review_candidates, timing_review_report = (
                        build_timing_review_candidates(
                            snapshot["segments"], acoustic_track,
                        )
                    )
                    timing_review_report = {
                        **timing_review_report, "enabled": True,
                    }
                except Exception as exc:
                    # Suggestions are an optional, human-operated layer. A
                    # pitch failure must not fail transcription quality.
                    logger.warning(
                        "[T4-SUGGESTION] fail-closed job=%s error=%s",
                        job_id, type(exc).__name__,
                    )
                    timing_review_candidates = []
                    timing_review_report = {
                        "enabled": True, "proposal_count": 0,
                        "failure": type(exc).__name__,
                        "automatic_apply_allowed": False,
                    }
            retry_stats["timing_review_suggestions"] = (
                _sanitize_analytical_evidence(timing_review_report)
            )
            retry_stats["spanish_orthography"] = (
                _sanitize_analytical_evidence(spanish_orthography_report)
            )

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
            diagnostic = {
                "windows": _sanitize_analytical_evidence(evidence_windows),
            }
            metrics = dict(quality_before.get("metrics") or {})
            hybrid_diagnostics = [
                item for item in retry_stats.get(
                    "structural_hybrid_diagnostics",
                ) or [] if isinstance(item, dict)
            ]
            processed_ids = {str(item["window"].get("id")) for item in analyses}
            coverage = parent_coverage(all_tiles, processed_ids)
            retry_stats["parent_coverage"] = coverage
            quality_proposal, proposal_telemetry = _build_review_proposal(
                snapshot["segments"], raw_proposal_windows, coverage,
            )
            retry_stats["review_proposal"] = proposal_telemetry
            quality_observation = None
            if (
                quality_proposal is None
                and os.environ.get(
                    "QUALITY_CONSENSUS_OBSERVATIONS_ENABLED", "0",
                ).strip().lower() in {"1", "true", "yes", "on"}
            ):
                quality_observation, observation_telemetry = _build_review_proposal(
                    snapshot["segments"], raw_proposal_windows, coverage,
                    observation_only=True,
                )
                retry_stats["review_observation"] = observation_telemetry
            complete_parent_ids = {
                parent_id for parent_id, item in coverage.items() if item.get("complete")
            }
            operator_proposal = None
            if os.environ.get(
                "QUALITY_OPERATOR_SUGGESTIONS_ENABLED", "0",
            ).strip().lower() in {"1", "true", "yes", "on"}:
                from operator_review_proposals import build_operator_review_proposal

                operator_proposal, operator_telemetry = (
                    build_operator_review_proposal(
                        snapshot["segments"],
                        timing_candidates=timing_review_candidates,
                        text_candidates=[
                            *raw_proposal_windows,
                            *spanish_orthography_candidates,
                        ],
                        complete_parent_ids=complete_parent_ids,
                    )
                )
                retry_stats["operator_suggestions"] = (
                    _sanitize_analytical_evidence(operator_telemetry)
                )
            complete_windows = [
                item for item in windows
                if str(item.get("id")) in complete_parent_ids
            ]
            incomplete_windows = [
                item for item in windows
                if str(item.get("id")) not in complete_parent_ids
            ]
            complete_remaining, resolved_windows = _confirmed_windows(
                snapshot["segments"], complete_windows, hybrid_diagnostics,
                expected_stem_sha256=stem_hash,
                expected_mix_sha256=audio_hash,
            )
            remaining_windows = [*incomplete_windows, *complete_remaining]
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
                is_live=bool(metrics.get("is_live")),
                retry_stats=_sanitize_analytical_evidence(retry_stats),
                acoustic_evidence=diagnostic,
                resolved_reason_counts=resolved_reasons,
                require_independent=calibration_identity()["calibrated"],
            )
            quality["analysis_windows"] = _sanitize_analytical_evidence(analyses)
            quality["timing_source"] = quality_before.get("timing_source", "unknown")
            quality = _attach_structural_t4_shadow(
                quality, snapshot["segments"],
            )

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
            **_safe_audio_identity_metrics(audio_hash, stem_hash),
            "api_cost_usd": retry_stats.get("api_cost_usd"),
            "cost_complete": bool(retry_stats.get("cost_complete", False)),
        }
        persisted = _persist_if_current(
            job_id, expected_revision, expected_segments_hash, quality,
            expected_audio_revision=expected_audio_revision,
            expected_audio_sha256=expected_audio_sha256,
            analysis_attempt_id=analysis_attempt_id,
            quality_proposal=quality_proposal,
            quality_observation=quality_observation,
            operator_proposal=operator_proposal,
        )
        return {
            "status": "persisted" if persisted else "discarded",
            "reason": None if persisted else "stale_after_analysis",
            "decision": quality.get("decision"),
        }
    except Exception as exc:
        # Provider exceptions may embed transcript excerpts.  Never log the
        # message or traceback on this content-processing boundary.
        logger.error(
            "[QUALITY-JOB] failed job=%s error_type=%s",
            job_id, type(exc).__name__,
        )
        # Let RQ Retry handle transient R2/Demucs/provider failures.  Only the
        # on_failure callback after retries are exhausted persists retry_failed.
        # RQ logs uncaught exception messages, so never re-raise a provider
        # exception that may contain transcript excerpts.
        raise RuntimeError(
            f"quality_job_failed:{type(exc).__name__}"
        ) from None
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
            is_live=bool(
                (snapshot["quality"].get("metrics") or {}).get("is_live")
            ),
            retry_stats={
                "attempted": True, "failed": True,
                "failure_reason": (
                    "provider_failure:"
                    f"{getattr(type_, '__name__', 'Error')}"
                ),
                "queue": "transcription_quality", "mutated_segments": False,
            },
        )
        failed["analysis_status"] = "failed"
        failed["analysis_pending"] = False
        _persist_if_current(
            job_id, int(kwargs.get("expected_revision", -1)),
            str(kwargs.get("expected_segments_hash") or ""), failed,
            expected_audio_revision=kwargs.get("expected_audio_revision"),
            expected_audio_sha256=str(kwargs.get("expected_audio_sha256") or ""),
            analysis_attempt_id=str(kwargs.get("analysis_attempt_id") or ""),
        )
    except Exception as exc:
        logger.error(
            "[QUALITY-JOB] failure callback failed error_type=%s",
            type(exc).__name__,
        )
