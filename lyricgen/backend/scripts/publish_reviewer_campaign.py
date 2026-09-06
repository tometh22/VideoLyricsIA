"""Explicit staging release import. No inference or audio downloads.

Build a private JSON bundle locally, transfer it by the release owner's chosen
channel, inspect --dry-run in staging, then use --execute. Per-song transactions
and immutable candidate identities make interrupted imports resumable.
"""
import argparse
import json
import os
import re
from pathlib import Path

from reviewer_campaign import atomic_json
from reviewer_campaign_product import CAMPAIGN, publish_song
from reviewer_shadow import source_binding
from shadow_reference_import import digest


def build_bundle(root, snapshot_path, output):
    manifest = json.loads((root / "campaign-300" / "manifest.json").read_text())
    snapshot = json.loads(snapshot_path.read_text())
    keys = {"job_id", "audio_sha256", "audio_revision", "segments_revision", "segments_sha256",
            "segments", "original_segments", "duration_seconds"}
    songs = [{k: s[k] for k in keys if k in s} for s in snapshot["jobs"]]
    artifacts = {}
    for row in manifest["songs"]:
        if row["status"] != "complete": continue
        folder = root / "campaign-300" / row["job_id"]
        if (folder / "candidate.json").exists() and (folder / "review.json").exists():
            artifacts[row["job_id"]] = {"candidate": json.loads((folder / "candidate.json").read_text()),
                "review": json.loads((folder / "review.json").read_text())}
    bundle = {"schema": "reviewer-campaign-release-bundle-v1", "campaign_id": manifest["campaign_id"],
        "manifest": manifest, "songs": songs, "artifacts": artifacts}
    validate_bundle(bundle)
    atomic_json(output, bundle)
    return {"count": len(songs), "complete_artifacts": len(artifacts), "bundle_sha256": digest(bundle)}


def validate_bundle(bundle):
    if bundle.get("schema") != "reviewer-campaign-release-bundle-v1" or bundle.get("campaign_id") != CAMPAIGN:
        raise ValueError("exact_campaign_release_bundle_required")
    songs = {s["job_id"]: s for s in bundle["songs"]}
    rows = {r["job_id"]: r for r in bundle["manifest"]["songs"]}
    if (len(songs) != 300 or len(bundle["songs"]) != 300 or len(rows) != 300
            or len(bundle["manifest"]["songs"]) != 300 or set(rows) != set(songs)
            or len(bundle["manifest"].get("execution_order", [])) != 300
            or set(bundle["manifest"].get("execution_order", [])) != set(songs)
            or bundle["manifest"]["campaign_id"] != CAMPAIGN):
        raise ValueError("exact_unique_300_roster_required")
    for jid, song in songs.items():
        if source_binding(song) != rows[jid]["source"] or digest(song["segments"]) != song["segments_sha256"]:
            raise ValueError("bundle_source_binding_mismatch")
    return songs, rows


def publish_bundle(bundle, *, execute=False):
    if os.getenv("ENVIRONMENT", "").lower() != "staging":
        raise ValueError("staging_environment_required")
    songs, rows = validate_bundle(bundle)
    from database import SessionLocal, BatchCampaign, Job
    db = SessionLocal()
    results = []
    try:
        campaign = db.query(BatchCampaign).filter(BatchCampaign.id == CAMPAIGN).one()
        ids = {j.job_id for j in db.query(Job).filter(Job.campaign_id == CAMPAIGN, Job.tenant_id == campaign.tenant_id).all()}
        if ids != set(songs): raise ValueError("live_campaign_roster_mismatch")
        for jid in bundle["manifest"]["execution_order"]:
            try:
                result = publish_song(db, campaign, songs[jid], rows[jid], bundle["artifacts"].get(jid), execute=execute)
                if execute: db.commit()
                else: db.rollback()
            except Exception as exc:
                db.rollback()
                result = {"job_id": jid, "status": "blocked", "reason": "candidate_import_failed", "error_type": type(exc).__name__}
                if isinstance(exc, ValueError) and re.fullmatch(r"[a-z_]{1,100}", str(exc)):
                    result["error_code"] = str(exc)
                if execute:
                    blocked = {**rows[jid], "status": "blocked", "blocker": "candidate_import_failed"}
                    try:
                        publish_song(db, campaign, songs[jid], blocked, execute=True)
                        db.commit()
                    except Exception:
                        db.rollback()
            results.append(result)
    finally:
        db.close()
    return {"schema": "reviewer-campaign-publication-report-v1", "campaign_id": CAMPAIGN,
        "bundle_sha256": digest(bundle), "execute": execute, "results": results,
        "counts": {s: sum(r["status"] == s for r in results) for s in ("complete", "partial", "pending", "blocked", "stale")},
        "inference_calls": 0, "documents_modified": False, "approvals_modified": False}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--build-bundle", type=Path)
    parser.add_argument("--snapshot", type=Path)
    parser.add_argument("--bundle", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--execute", action="store_true")
    modes.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    if args.build_bundle:
        if not args.snapshot or args.execute: parser.error("bundle build requires snapshot and is not execute")
        print(json.dumps(build_bundle(args.build_bundle, args.snapshot, args.output)))
    else:
        if not args.bundle: parser.error("--bundle required")
        if args.bundle.stat().st_size > 300 * 1024 * 1024: parser.error("bundle size limit")
        result = publish_bundle(json.loads(args.bundle.read_text()), execute=args.execute)
        atomic_json(args.output, result)
        print(json.dumps({k:v for k,v in result.items() if k != "results"}))
