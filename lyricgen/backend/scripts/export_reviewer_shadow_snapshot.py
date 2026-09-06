"""Run inside API container; stdout is private data. Transaction is READ ONLY.

Example: python scripts/export_reviewer_shadow_snapshot.py --campaign ID
Redirect stdout to a mode-0600 local artifact; never commit it or log lyrics.
"""
import argparse
import hashlib
import json
from datetime import datetime, timezone


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--campaign", required=True)
    args = parser.parse_args()
    from sqlalchemy import text
    from sqlalchemy.orm import defer
    from database import SessionLocal, Job, EditorDocument, EditorVersion, BatchCampaignItem
    db = SessionLocal()
    try:
        db.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"))
        records = db.query(Job, EditorDocument, BatchCampaignItem).options(defer(EditorDocument.machine_evidence)).join(
            EditorDocument, EditorDocument.job_id == Job.job_id).join(
            BatchCampaignItem, BatchCampaignItem.id == Job.campaign_item_id).filter(
            Job.campaign_id == args.campaign).order_by(BatchCampaignItem.ordinal).all()
        output = []
        def sha(value):
            return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                             separators=(",", ":")).encode()).hexdigest()
        initial_versions = {v.job_id: v for v in db.query(EditorVersion).filter(
            EditorVersion.job_id.in_([j.job_id for j, _, _ in records]),
            EditorVersion.revision == 0, EditorVersion.reason == "transcription").all()}
        for job, doc, item in records:
            initial = initial_versions.get(job.job_id)
            quality = job.transcription_quality or {}
            output.append({
                "job_id": job.job_id, "ordinal": item.ordinal, "artist": job.artist,
                "title": job.song_title, "filename": item.filename,
                "duration_seconds": item.duration_seconds, "status": job.status,
                "audio_sha256": job.input_audio_sha256, "audio_revision": job.audio_revision,
                "input_r2_key": job.input_r2_key, "render_overrides": item.render_overrides,
                "segments": doc.current_segments, "segments_revision": doc.revision,
                "approved_at": job.approved_at.isoformat() if job.approved_at else None,
                "approved_by": job.approved_by,
                "updated_by": doc.updated_by,
                "segments_sha256": sha(doc.current_segments),
                "original_segments": initial.segments if initial else None,
                "original_revision": initial.revision if initial else None,
                "original_sha256": sha(initial.segments) if initial else None,
                "baseline_human_gold": False, "updated_at": doc.updated_at.isoformat(),
                "reference_hypothesis": quality.get("reference_hypothesis"),
                "machine_evidence": None,
                "machine_evidence_fetch": "deferred_until_sample_freeze",
                "existing_proposal": doc.quality_proposal,
                "quality_status": quality.get("analysis_status"),
                "language_evidence": {k: v for k, v in quality.items() if "language" in k or "lid" in k},
            })
        result = {"schema": "reviewer-shadow-snapshot-v1", "campaign_id": args.campaign,
                  "captured_at": datetime.now(timezone.utc).isoformat(), "jobs": output,
                  "read_only_transaction": True, "automatic_apply_allowed": False}
        result["snapshot_sha256"] = sha(output)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    finally:
        db.rollback()
        db.close()


if __name__ == "__main__":
    main()
