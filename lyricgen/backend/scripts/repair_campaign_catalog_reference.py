"""Repair untouched campaign drafts after removing an editorial sheet heading.

Dry-run by default. Apply only to drafts whose exact source audio and original
sheet text still match the manifest, and whose cleaned lyric independently
passes the existing audio-first gate. Alignment may still decline. Every
successful change creates an editor revision, provenance, and quality event.
"""

from __future__ import annotations

import argparse
import asyncio
import copy
import hashlib
import json
import sys
import tempfile
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from database import AuditLog, BatchCampaignItem, EditorDocument, Job, SessionLocal
from editor import _ensure_version, normalize_segments
from reference_attestation import assess_reference_attestation, reference_gate_action
from transcription_worker import _maybe_apply_catalog_reference, _sung_catalog_text


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _eligible(item: BatchCampaignItem, job: Job, document: EditorDocument | None) -> tuple[str, dict | None]:
    reference = dict((item.render_overrides or {}).get("source_reference") or {})
    quality = dict(job.transcription_quality or {})
    outcome = dict(quality.get("catalog_reference") or {})
    if job.status != "transcribed_pending" or outcome.get("used"):
        return "not_unreviewed_rejection", None
    if outcome.get("status") != "rejected" or reference.get("status") != "candidate":
        return "not_rejected_candidate", None
    if (not document or int(job.segments_revision or 0) != 0
            or int(document.revision or 0) != 0 or document.updated_by is not None
            or document.lock_user_id is not None
            or document.current_segments != job.segments_json):
        return "editor_changed_or_locked", None
    raw = str(reference.get("text") or "").strip()
    if (not raw or hashlib.sha256(raw.encode()).hexdigest() != reference.get("text_sha256")
            or reference.get("source_audio_sha256") != item.sha256
            or reference.get("source_asset_id") != item.sha256
            or (job.input_audio_sha256 and job.input_audio_sha256 != item.sha256)):
        return "source_binding_invalid", None
    sung, normalization = _sung_catalog_text(reference)
    if normalization != "editorial_title_heading_removed":
        return "no_editorial_heading", None
    live = "live" in str(item.title or "").lower() or "en vivo" in str(item.title or "").lower()
    report = assess_reference_attestation(
        sung, job.segments_json or [], reference_source="catalog_unverified",
        audio_duration_s=item.duration_seconds, is_live=live,
    )
    if reference_gate_action(report, mode="enforce", is_live=live) != "reference_allowed":
        return "audio_gate_declined", report
    return "candidate", report


def _campaign_rows(db, campaign_id: str):
    return (db.query(BatchCampaignItem, Job, EditorDocument)
            .join(Job, Job.campaign_item_id == BatchCampaignItem.id)
            .outerjoin(EditorDocument, EditorDocument.job_id == Job.job_id)
            .filter(BatchCampaignItem.campaign_id == campaign_id)
            .order_by(BatchCampaignItem.ordinal).all())


async def _align(item_id: str, campaign_id: str) -> tuple[str, str]:
    db = SessionLocal()
    try:
        item, job, document = (db.query(BatchCampaignItem, Job, EditorDocument)
                               .join(Job, Job.campaign_item_id == BatchCampaignItem.id)
                               .outerjoin(EditorDocument, EditorDocument.job_id == Job.job_id)
                               .filter(BatchCampaignItem.id == item_id,
                                       BatchCampaignItem.campaign_id == campaign_id).one())
        decision, _ = _eligible(item, job, document)
        if decision != "candidate":
            return str(item.ordinal), decision
        reference = copy.deepcopy((item.render_overrides or {})["source_reference"])
        original_segments = copy.deepcopy(job.segments_json or [])
        job_id = job.job_id
        audio_key = job.input_r2_key
        audio_sha256 = item.sha256
        filename = Path(item.filename).name
        live = "live" in str(item.title or "").lower() or "en vivo" in str(item.title or "").lower()
        ordinal = str(item.ordinal)
    finally:
        db.close()
    if not audio_key:
        return ordinal, "audio_unavailable"

    import storage
    from main import _maybe_anchor_align
    with tempfile.TemporaryDirectory(prefix="catalog-realign-") as temp:
        audio_path = Path(temp) / filename
        if not await asyncio.to_thread(storage.download_object, audio_key, str(audio_path)):
            return ordinal, "audio_download_failed"
        if _sha256(audio_path) != audio_sha256:
            return ordinal, "download_sha256_mismatch"
        aligned = await _maybe_apply_catalog_reference(
            {"segments": copy.deepcopy(original_segments)}, str(audio_path),
            job_id, reference, audio_sha256, live=live, aligner=_maybe_anchor_align,
        )
    outcome = dict(aligned.get("catalog_reference") or {})
    if not outcome.get("used"):
        return ordinal, str(outcome.get("status") or "alignment_declined")
    segments = normalize_segments(aligned.get("segments") or [])
    if not segments:
        return ordinal, "empty_alignment"

    db = SessionLocal()
    event_id = None
    try:
        item, job, document = (db.query(BatchCampaignItem, Job, EditorDocument)
                               .join(Job, Job.campaign_item_id == BatchCampaignItem.id)
                               .join(EditorDocument, EditorDocument.job_id == Job.job_id)
                               .filter(BatchCampaignItem.id == item_id,
                                       BatchCampaignItem.campaign_id == campaign_id)
                               .with_for_update().one())
        decision, _ = _eligible(item, job, document)
        if decision != "candidate" or job.segments_json != original_segments:
            db.rollback()
            return ordinal, "changed_while_aligning"
        if job.input_r2_key != audio_key or item.sha256 != audio_sha256:
            db.rollback()
            return ordinal, "audio_changed_while_aligning"

        quality = dict(job.transcription_quality or {})
        outcome["source_audio_revision"] = int(job.audio_revision or 0)
        quality["catalog_reference"] = outcome
        job.transcription_quality = quality
        job.segments_json = segments
        job.segments_revision = 1
        document.current_segments = segments
        document.revision = 1
        document.updated_at = datetime.now(timezone.utc)
        # This is a machine correction; original_segments and updated_by keep
        # the original acoustic draft and do not claim a human edited it.
        _ensure_version(db, document, 1, segments, None, "catalog_realign", provenance={
            "source_kind": "google_sheet",
            "source_document_id": reference.get("source_document_id"),
            "row_numbers": reference.get("row_numbers"),
            "text_sha256": outcome.get("text_sha256"),
            "effective_text_sha256": outcome.get("effective_text_sha256"),
            "source_audio_sha256": audio_sha256,
        })
        from main import _create_editor_quality_outbox, _invalidate_quality_after_editor_save
        superseded_quality = _invalidate_quality_after_editor_save(
            job, revision=1, segments=segments, previous_segments=original_segments,
        )
        # Superseding segment-bound verdicts intentionally drops catalogue
        # provenance. Restore only this newly audio-validated decision, bound
        # to the just-committed audio and editor revision.
        superseded_quality["catalog_reference"] = {
            **outcome, "editor_revision": 1,
        }
        job.transcription_quality = superseded_quality
        db.add(AuditLog(user_id=None, action="lyrics.catalog_realign", detail={
            "job_id": job_id, "campaign_id": campaign_id, "ordinal": item.ordinal,
            "from_revision": 0, "to_revision": 1,
            "source_audio_sha256": audio_sha256,
            "effective_text_sha256": outcome.get("effective_text_sha256"),
            "timing_source": outcome.get("timing_source"),
        }))
        event_id = _create_editor_quality_outbox(
            db, job, revision=1, segments=segments,
            quality=job.transcription_quality, reason="catalog_realign",
        )
        db.commit()
    finally:
        db.close()
    from main import _dispatch_editor_quality_outbox
    _dispatch_editor_quality_outbox(event_id)
    return ordinal, "aligned"


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--campaign", required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--limit", type=int, default=1)
    parser.add_argument("--ordinal", type=int)
    args = parser.parse_args()
    db = SessionLocal()
    try:
        rows = _campaign_rows(db, args.campaign)
        decisions = []
        for item, job, document in rows:
            if args.ordinal is not None and item.ordinal != args.ordinal:
                continue
            decision, _ = _eligible(item, job, document)
            decisions.append((item.id, item.ordinal, decision))
        print(json.dumps({"campaign": args.campaign, "decisions": dict(Counter(
            decision for _, _, decision in decisions)), "eligible_ordinals": [
                ordinal for _, ordinal, decision in decisions if decision == "candidate"
            ]}, ensure_ascii=False), flush=True)
    finally:
        db.close()
    if not args.apply:
        return 0
    results = []
    for item_id, ordinal, decision in decisions:
        if decision != "candidate" or len(results) >= max(0, args.limit):
            continue
        try:
            results.append(await _align(item_id, args.campaign))
        except Exception as exc:
            results.append((str(ordinal), f"error:{type(exc).__name__}"))
        print(json.dumps({"ordinal": results[-1][0], "result": results[-1][1]}), flush=True)
    print(json.dumps({"applied": dict(Counter(result for _, result in results))}), flush=True)
    return 1 if any(result.startswith("error:") for _, result in results) else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
