"""Guarded offline-full-review adapter to the existing human suggestion flow.

Preparation never writes. Publication is opt-in and never commits, applies a
candidate or approves a song. Coverage receipts describe inspected intervals,
not correctness; dual families may listen to the SAME original-mix clock.
"""
from copy import deepcopy
import math
import re

from reviewer_assist import enabled, prepare
from reviewer_candidate import build_candidate
from reviewer_shadow import assert_current, source_binding, validate_snapshot
from shadow_reference_import import digest

REQUIRED_AUDIO_FAMILIES = frozenset({"openai/whisper-1", "google/gemini-2.5-flash-audio"})

def _coverage(song, review):
    if review.get("schema") != "full-song-review-v1":
        raise ValueError("full_review_receipt_required")
    assert_current(review, song)
    if review.get("reconciliation_complete") is not True:
        raise ValueError("reconciliation_incomplete")
    families = review.get("required_families", [])
    if set(families) != REQUIRED_AUDIO_FAMILIES or len(families) != 2:
        raise ValueError("frozen_independent_audio_families_required")
    duration = float(song["duration_seconds"])
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError("invalid_duration")
    covered = {}
    for family in families:
        intervals = []
        for receipt in review.get("audio_evidence", []):
            if receipt.get("family") != family or receipt.get("tool_status") != "ok":
                continue
            if receipt.get("received_audio") is not True:
                continue
            if receipt.get("source") != source_binding(song):
                raise ValueError("stale_audio_evidence")
            if not re.fullmatch(r"[a-f0-9]{64}", receipt.get("evidence_sha256", "")):
                raise ValueError("audio_evidence_identity_missing")
            # Cross-signal coverage is not transferable without a verified clock.
            if receipt.get("clock") != "original_mix_decoded":
                raise ValueError("coverage_clock_unverified")
            start, end = float(receipt["start"]), float(receipt["end"])
            if not all(math.isfinite(x) for x in (start, end)) or not 0 <= start < end <= duration + 1e-6:
                raise ValueError("invalid_coverage_interval")
            intervals.append((start, min(duration, end)))
        cursor = 0.
        for start, end in sorted(intervals):
            if start > cursor + 1e-6:
                raise ValueError("audio_coverage_incomplete")
            cursor = max(cursor, end)
        if cursor < duration - 1e-6:
            raise ValueError("audio_coverage_incomplete")
        covered[family] = cursor
    return covered


def prepare_batch_candidate(song, candidate, review, *, original_segments=None,
                            allowed_suggestion_types=("text", "timing")):
    """Revalidate source, full coverage and frozen selectors, excluding held edits.

    The caller must retain raw evidence files named by receipt hashes. Neither a
    forged `complete` label nor a supplied full-length document earns readiness.
    Held decisions remain visible as unresolved metadata, never bulk-adoptable.
    """
    validate_snapshot(song)
    assert_current(candidate, song)
    coverage = _coverage(song, review)
    if candidate.get("baseline") != song["segments"]:
        raise ValueError("candidate_baseline_mismatch")
    rebuilt = build_candidate(song, candidate.get("decision_evidence", []))
    if candidate.get("segments") != rebuilt["segments"] or candidate.get("candidate_sha256") != rebuilt["candidate_sha256"]:
        raise ValueError("candidate_contains_unbacked_changes")
    held = set(review.get("held_decision_ids", []))
    # IDs alone do not prove that a row still equals its machine baseline.
    def content_key(row):
        return digest([row.get("text"), row.get("start"), row.get("end")])
    original_keys = ({content_key(row) for row in original_segments}
                     if original_segments is not None else None)
    for change in rebuilt["changes"]:
        row = song["segments"][change["line_index"]]
        kind = "text" if change["field"] == "text" else "timing"
        if (kind not in allowed_suggestion_types
                or original_keys is not None and content_key(row) not in original_keys):
            held.add(change["evidence_id"])
    backed_ids = {change["evidence_id"] for change in rebuilt["changes"]}
    decisions = [d for d in candidate.get("decision_evidence", [])
                 if d["proposal_id"] not in held and d["proposal_id"] in backed_ids]
    safe = build_candidate(song, decisions)
    prepared = prepare(song, decisions)
    if prepared["proposal"]:
        prepared["proposal"]["reviewer_assist"].update({
            "campaign_id": song.get("campaign_id"),
            "candidate": {"segments": safe["segments"],
                "baseline_sha256": safe["baseline_sha256"],
                "candidate_sha256": safe["candidate_sha256"],
                "approved": False, "unchanged_lines_certified": False},
            "batch_review": {"receipt_sha256": digest(review), "coverage_seconds": coverage,
                "reconciliation_complete": True, "held_decision_ids": sorted(held),
                "correctness_certified": False}})
    return {**prepared, "candidate": safe, "review_complete": True,
        "coverage_seconds": coverage, "held_decision_ids": sorted(held),
        "ready_for_human_review": True, "approved": False}


def publish_batch_candidate(db, song, candidate, review):
    """Future authorized import, using the same row locks/order as persistence.

    No DB access at all while the existing rollout flag is off. The caller owns
    the transaction and authorization; flags are necessary, not authorization.
    """
    if not enabled():
        return {"published": False, "reason": "reviewer_assist_disabled"}
    from reviewer_assist_scope import publication_enabled
    if not publication_enabled(song.get("campaign_id")):
        return {"published": False, "reason": "candidate_publication_disabled_or_out_of_scope"}
    from database import EditorDocument, Job
    from editor import (operator_suggestions_enabled, operator_suggestion_type_enabled,
                        persist_operator_review_proposal_if_current)
    from transcription_quality import segments_hash
    if not operator_suggestions_enabled():
        return {"published": False, "reason": "operator_suggestions_disabled"}
    job = db.query(Job).filter(Job.job_id == song["job_id"]).populate_existing().with_for_update().first()
    document = db.query(EditorDocument).filter(EditorDocument.job_id == song["job_id"]).populate_existing().with_for_update().first()
    if job is None or document is None:
        return {"published": False, "reason": "document_or_job_missing"}
    if not publication_enabled(getattr(job, "campaign_id", None)):
        return {"published": False, "reason": "campaign_out_of_scope"}
    from datetime import datetime, timezone
    expiry = getattr(document, "lock_expires_at", None)
    if expiry and expiry.tzinfo is None:
        expiry = expiry.replace(tzinfo=timezone.utc)
    if getattr(document, "lock_user_id", None) and expiry and expiry > datetime.now(timezone.utc):
        return {"published": False, "reason": "active_human_review_preserved"}
    if getattr(document, "approved_at", None) or getattr(job, "approved_at", None) or getattr(job, "status", None) in {"lyrics_approved", "done"}:
        return {"published": False, "reason": "human_approval_preserved"}
    if document.quality_proposal:
        return {"published": False, "reason": "existing_proposal_preserved"}
    live = {**song, "segments": deepcopy(document.current_segments or []),
        "segments_revision": document.revision, "audio_revision": job.audio_revision,
        "audio_sha256": job.input_audio_sha256,
        "segments_sha256": digest(document.current_segments or [])}
    prepared = prepare_batch_candidate(live, candidate, review,
        original_segments=document.original_segments or [],
        allowed_suggestion_types=tuple(k for k in ("text", "timing")
                                       if operator_suggestion_type_enabled(k)))
    if not prepared["proposal"]:
        return {"published": False, "reason": "no_backed_changes", **prepared}
    published = persist_operator_review_proposal_if_current(db, job_id=song["job_id"],
        expected_revision=live["segments_revision"], expected_segments_hash=segments_hash(live["segments"]),
        expected_audio_revision=live["audio_revision"], expected_audio_sha256=live["audio_sha256"],
        proposal=prepared["proposal"])
    return {"published": published, **prepared}
