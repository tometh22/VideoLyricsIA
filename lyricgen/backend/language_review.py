"""Shared language / reference-discrepancy review contract.

One implementation used by the transcription chokepoint, the reload serializers
(`/status`, `/transcription-status`) and BOTH approval gates (`/approve` and the
campaign `approve-lyrics`).  Keeping it in one place is the point: an alert that
is recomputed differently in one of those places could be shown, lost on reload,
or bypassed on approval.  The divergence signal is a DISCREPANCY alert (the
output does not match its own audio-derived reference); it never forces a
language and never rewrites the lyrics.
"""

from __future__ import annotations

from transcription_language import build_language_contract


def reference_text_of(transcription_quality) -> str:
    """The audio-derived reference lyrics persisted on the job, or ''."""
    tq = transcription_quality if isinstance(transcription_quality, dict) else {}
    hypothesis = tq.get("reference_hypothesis")
    hypothesis = hypothesis if isinstance(hypothesis, dict) else {}
    return str(hypothesis.get("reference_text") or "")


def resolution_matches(transcription_quality, revision, segments) -> bool:
    """True only when a human resolution is bound to the CURRENT revision+hash.

    Binding to revision + content hash means an edit (new revision / changed
    text) automatically invalidates a stale resolution, so a fresh discrepancy
    must be reviewed again — even if the record survives in the JSONB blob.
    """
    tq = transcription_quality if isinstance(transcription_quality, dict) else {}
    resolution = tq.get("language_resolution")
    if not isinstance(resolution, dict):
        return False
    if int(resolution.get("revision", -1)) != int(revision or 0):
        return False
    from transcription_quality import segments_hash
    return resolution.get("segments_hash") == segments_hash(segments or [])


def review_payload(segments, transcription_quality, revision) -> dict:
    """Recompute the language/discrepancy contract from PERSISTED data.

    Used by the reload serializers and the approval gates so the alert survives
    reload/deep-link, recalculates when the lyrics or reference change, and is
    enforced identically on the server (an old client cannot skip it).
    """
    payload = build_language_contract(
        segments or [], reference_text_of(transcription_quality),
    )
    payload["language_review_resolved"] = resolution_matches(
        transcription_quality, revision, segments,
    )
    return payload
