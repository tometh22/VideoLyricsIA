"""Supervised bridge to existing operator suggestions; default-off rollout.

No alternate approval flow. Preparation is pure and can run with rollout off.
Publication additionally obeys existing text/timing rollout gates and revision
checks. Neither path applies edits, approves songs, or schedules media.
"""
from copy import deepcopy
import os

from operator_review_proposals import build_operator_review_proposal
from reviewer_shadow import assert_current, review_window
from shadow_reference_import import digest


def enabled():
    return os.environ.get("REVIEWER_ASSIST_ENABLED", "0").lower().strip() in {
        "1", "true", "yes", "on"}


def prepare(song, decisions):
    candidates, diagnostics = [], []
    for decision in decisions:
        assert_current(decision, song)
        # Re-run the frozen selector rather than trusting a serialized 'propose'.
        checked = review_window(song, decision["window"], evidence=decision["evidence"],
                                commit=decision["commit"])
        current = checked["current"]
        protected = bool(current.get("locked") or current.get("operator_locked"))
        diagnostics.append({"proposal_id": checked["proposal_id"],
            "human_protected": protected, "content": checked["content"],
            "timing": checked["timing"], "tool_errors": checked["tool_errors"]})
        if protected:
            continue
        for kind, verdict in (("text", checked["content"]), ("timing", checked["timing"])):
            if verdict["decision"] != "propose":
                continue
            proposed = deepcopy(current)
            if kind == "text":
                proposed["text"] = verdict["text"]
            else:
                proposed["end"] = verdict["end_seconds"]
            candidates.append({"kind": "operator_review_candidate",
                "id": digest([checked["proposal_id"], kind]), "suggestion_type": kind,
                "start": current["start"], "end": max(current["end"], proposed["end"]),
                "current_segments": [current], "proposed_segments": [proposed],
                "confidence": "medium", "reasons": ["reviewer_audio_evidence"],
                "current_end": current["end"], "proposed_end": proposed["end"],
                "preview_start": checked["window"]["start"],
                "preview_end": checked["window"]["end"],
                "source_families": verdict.get("families", []),
                "selector_policy": checked["policy"]["version"]})
    proposal, telemetry = build_operator_review_proposal(song["segments"],
        text_candidates=[c for c in candidates if c["suggestion_type"] == "text"],
        timing_candidates=[c for c in candidates if c["suggestion_type"] == "timing"])
    if proposal:
        proposal["reviewer_assist"] = {"version": "supervised-v1",
            "source": deepcopy(decisions[0]["source"]),
            "decision_ids": [d["proposal_id"] for d in decisions],
            "evidence_sha256": digest(decisions), "correctness_certified": False}
    return {"proposal": proposal, "telemetry": telemetry, "diagnostics": diagnostics,
            "evidence": deepcopy(decisions), "automatic_apply_allowed": False}


def publish(db, song, decisions):
    if not enabled():
        return {"published": False, "reason": "reviewer_assist_disabled"}
    prepared = prepare(song, decisions)
    if prepared["proposal"] is None:
        return {"published": False, "reason": "no_qualified_proposals", **prepared}
    from editor import persist_operator_review_proposal_if_current
    from transcription_quality import segments_hash
    from database import EditorDocument
    document = db.query(EditorDocument).filter(
        EditorDocument.job_id == song["job_id"]).with_for_update().first()
    if document is None:
        return {"published": False, "reason": "document_missing"}
    if document.quality_proposal:
        return {"published": False, "reason": "existing_proposal_preserved"}
    published = persist_operator_review_proposal_if_current(db, job_id=song["job_id"],
        expected_revision=song["segments_revision"],
        expected_segments_hash=segments_hash(song["segments"]),
        expected_audio_revision=song["audio_revision"],
        expected_audio_sha256=song["audio_sha256"], proposal=prepared["proposal"])
    return {"published": published, **prepared}


def operational_counts(generated_ids, events):
    """Ignored != rejected; receipts describe operation, not objective precision."""
    generated = set(generated_ids)
    seen, examined, decisions, event_ids = set(), set(), {}, set()
    active = 0.0
    for event in events:
        if not event.get("event_id") or event["event_id"] in event_ids:
            continue
        event_ids.add(event["event_id"])
        identity = event.get("proposal_id")
        if identity not in generated:
            continue
        kind = event.get("kind")
        if kind == "shown":
            seen.add(identity)
        if kind == "examined":
            examined.add(identity)
        if kind in {"accepted", "edited", "rejected"}:
            decisions[identity] = kind
            examined.add(identity)
        if kind == "active_seconds":
            seconds = event.get("seconds", 0)
            if isinstance(seconds, (int, float)) and 0 <= seconds <= 60:
                active += seconds
    return {"generated": len(generated), "shown": len(seen),
            "unexamined": len(generated - examined),
            **{k: sum(v == k for v in decisions.values()) for k in ("accepted", "edited", "rejected")},
            "active_seconds": active, "objective_precision": None, "causal_time_saved": None}
