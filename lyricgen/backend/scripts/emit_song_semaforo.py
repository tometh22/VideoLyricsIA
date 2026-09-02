#!/usr/bin/env python3
"""Song-level traffic light ("semáforo") v1 from persisted, gold-free signals.

The verdict is emitted BEFORE any human review and stored append-only in
``AuditLog`` (``action = semaforo.verdict.v1``) so the confidence protocol can
build the verdict × real-error matrix afterwards. It is deliberately NOT
written into ``Job.transcription_quality``: the editor returns that payload to
reviewers and the protocol wants them blind to the verdict.

Rule v1 (song level; line-level certification is Capa 1 and comes later):

* RED   if live, or LoRA↔base disagreement >= 0.082 (every pilot song at or
        above that score had baseline WER > 10%), or audio coverage < 0.90,
        or more than 10 unsafe windows, or the quality replay is missing /
        failed (in doubt, degrade).
* GREEN if disagreement <= 0.035 (every pilot song at or below that score was
        easy), coverage >= 0.97, zero unsafe windows, not live, and the quality
        gate did not require review.
* YELLOW otherwise (mostly certified, a bounded number of dubious windows).

Live is never green. Missing signals degrade. Thresholds come from
.context/lora-disagreement-router-pilot-20260902.json (AUC 0.971, 41 songs).

Examples::

    python scripts/emit_song_semaforo.py --tenant universal_music --status pending_review
    python scripts/emit_song_semaforo.py --tenant universal_music --commit --output queue.json
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
import sys
from typing import Any

BACKEND_ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, BACKEND_ROOT)

RULE_VERSION = "semaforo-v1"
ACTION = "semaforo.verdict.v1"
DISAGREEMENT_GREEN_MAX = 0.035
DISAGREEMENT_RED_MIN = 0.082
COVERAGE_GREEN_MIN = 0.97
COVERAGE_RED_MAX = 0.90
UNSAFE_WINDOWS_RED_MIN = 11


def _num(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        try:
            value = float(value)
        except (TypeError, ValueError):
            return None
    value = float(value)
    return value if value == value else None


def song_verdict(quality: dict | None, paired: dict | None = None) -> dict[str, Any]:
    """Pure rule: quality payload -> {color, reasons, inputs, rank_key}.

    ``paired`` optionally supplies the pilot-scale disagreement (turbo base vs
    turbo+LoRA on identical chunks, see paired_disagreement_offline.py). When
    given it replaces the runtime ``difficulty_router`` score, which compares
    WhisperX against LoRA and is not on the pilot's scale.
    """
    quality = quality if isinstance(quality, dict) else {}
    metrics = quality.get("metrics") if isinstance(quality.get("metrics"), dict) else {}
    router = metrics.get("difficulty_router") if isinstance(metrics.get("difficulty_router"), dict) else {}
    disagreement_source = "runtime_difficulty_router"
    disagreement = _num(router.get("score"))
    if isinstance(paired, dict) and _num(paired.get("disagreement")) is not None:
        disagreement = _num(paired.get("disagreement"))
        disagreement_source = str(paired.get("source") or "paired_offline")
    coverage = _num(metrics.get("audio_coverage"))
    is_live = bool(metrics.get("is_live"))
    unsafe = [w for w in (quality.get("unsafe_windows") or []) if isinstance(w, dict)]
    decision = str(quality.get("decision") or "unknown")
    analysis_status = str(quality.get("analysis_status") or "none")
    retry = quality.get("retry") if isinstance(quality.get("retry"), dict) else {}
    windows_resolved = int(_num(retry.get("windows_resolved")) or 0)

    inputs = {
        "disagreement": disagreement, "disagreement_source": disagreement_source,
        "audio_coverage": coverage,
        "is_live": is_live, "unsafe_windows": len(unsafe),
        "windows_resolved": windows_resolved, "decision": decision,
        "analysis_status": analysis_status,
        "language": metrics.get("language"),
    }
    reasons: list[str] = []
    if is_live:
        reasons.append("live_never_green")
    if disagreement is None:
        reasons.append("disagreement_missing")
    elif disagreement >= DISAGREEMENT_RED_MIN:
        reasons.append("disagreement_high")
    if coverage is None:
        reasons.append("coverage_missing")
    elif coverage < COVERAGE_RED_MAX:
        reasons.append("coverage_low")
    if len(unsafe) >= UNSAFE_WINDOWS_RED_MIN:
        reasons.append("too_many_unsafe_windows")
    if decision in {"retry_failed", "unsafe", "fail", "failed", "blocked"}:
        reasons.append(f"decision_{decision}")
    if unsafe and analysis_status != "complete":
        reasons.append("replay_not_complete")

    if reasons:
        color = "red"
    elif (
        disagreement <= DISAGREEMENT_GREEN_MAX
        and coverage >= COVERAGE_GREEN_MIN
        and not unsafe
        and decision in {"pass", "approved", "safe"}
    ):
        color = "green"
    else:
        color = "yellow"
        if unsafe:
            reasons.append(f"unsafe_windows_{len(unsafe)}")
        if disagreement > DISAGREEMENT_GREEN_MAX:
            reasons.append("disagreement_ambiguous")
        if coverage < COVERAGE_GREEN_MIN:
            reasons.append("coverage_partial")
        if decision not in {"pass", "approved", "safe"}:
            reasons.append(f"decision_{decision}")

    # Delivery order: easiest first. Missing disagreement sorts last.
    rank_key = disagreement if disagreement is not None else 9.0
    return {
        "rule_version": RULE_VERSION, "color": color, "reasons": reasons,
        "inputs": inputs, "rank_key": rank_key,
    }


def _existing_verdicts(db, job_ids: list[str]) -> dict[str, dict]:
    from database import AuditLog
    wanted = set(job_ids)
    rows = (
        db.query(AuditLog)
        .filter(AuditLog.action == ACTION)
        .order_by(AuditLog.created_at.asc())
        .all()
    )
    return {
        str(row.detail.get("job_id")): dict(row.detail)
        for row in rows
        if isinstance(row.detail, dict) and str(row.detail.get("job_id")) in wanted
    }


def collect(db, *, tenant: str | None, status: str | None, job_ids: list[str] | None):
    from database import Job
    query = db.query(Job)
    if job_ids:
        query = query.filter(Job.job_id.in_(job_ids))
    if tenant:
        query = query.filter(Job.tenant_id == tenant)
    if status:
        query = query.filter(Job.status == status)
    return query.order_by(Job.created_at.asc()).all()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--tenant")
    parser.add_argument("--status", default="pending_review")
    parser.add_argument("--job-id", action="append", dest="job_ids")
    parser.add_argument("--commit", action="store_true", help="persist verdicts in AuditLog (default: dry run)")
    parser.add_argument("--force", action="store_true", help="re-emit even if a verdict for this rule exists")
    parser.add_argument("--output", help="write the ranked queues as JSON")
    parser.add_argument("--editor-base", default=os.environ.get("SEMAFORO_EDITOR_BASE", "https://staging.genly.pro/edit-lyrics/"))
    parser.add_argument("--disagreement-file", help="JSON from paired_disagreement_offline.py (keyed by sha256, with job_id)")
    args = parser.parse_args()
    paired_by_job: dict[str, dict] = {}
    if args.disagreement_file:
        for row in json.load(open(args.disagreement_file)).values():
            if isinstance(row, dict) and row.get("job_id"):
                paired_by_job[str(row["job_id"])] = row
    if not (args.tenant or args.job_ids):
        parser.error("--tenant or --job-id is required")

    from database import AuditLog, SessionLocal
    db = SessionLocal()
    try:
        jobs = collect(db, tenant=args.tenant, status=args.status, job_ids=args.job_ids)
        existing = _existing_verdicts(db, [j.job_id for j in jobs])
        emitted_at = datetime.now(timezone.utc).isoformat()
        rows = []
        for job in jobs:
            verdict = song_verdict(job.transcription_quality, paired_by_job.get(job.job_id))
            prior = existing.get(job.job_id)
            reused = bool(prior) and not args.force
            record = {
                "job_id": job.job_id, "filename": job.filename,
                "tenant_id": job.tenant_id, "status": job.status,
                **(prior if reused else verdict),
                "emitted_at": prior.get("emitted_at") if reused else emitted_at,
                "reused_existing": reused,
            }
            if args.commit and not reused:
                db.add(AuditLog(user_id=None, action=ACTION, detail={
                    "job_id": job.job_id, "filename": job.filename,
                    "tenant_id": job.tenant_id, "job_status": job.status,
                    **verdict, "emitted_at": emitted_at, "blind_review": True,
                }))
            rows.append(record)
        if args.commit:
            db.commit()
    finally:
        db.close()

    delivery = sorted(rows, key=lambda r: ({"green": 0, "yellow": 1, "red": 2}[r["color"]], r["rank_key"]))
    learning = sorted(rows, key=lambda r: -r["rank_key"])
    counts = {c: sum(1 for r in rows if r["color"] == c) for c in ("green", "yellow", "red")}
    report = {
        "schema": "song-semaforo-queue-v1", "rule_version": RULE_VERSION,
        "generated_at": emitted_at, "committed": bool(args.commit),
        "songs": len(rows), "counts": counts,
        "delivery_order": [
            {"rank": i + 1, "job_id": r["job_id"], "color": r["color"], "filename": r["filename"],
             "disagreement": r["inputs"].get("disagreement"), "reasons": r["reasons"],
             "editor_url": args.editor_base + r["job_id"]}
            for i, r in enumerate(delivery)
        ],
        "learning_order": [r["job_id"] for r in learning],
    }
    if args.output:
        with open(args.output, "w", encoding="utf-8") as handle:
            json.dump(report, handle, ensure_ascii=False, indent=2)
    print(json.dumps({"songs": len(rows), "counts": counts, "committed": bool(args.commit),
                      "reused_existing": sum(1 for r in rows if r["reused_existing"])}, ensure_ascii=False))
    for r in delivery:
        print(f"{r['color']:6} {str(r['inputs'].get('disagreement'))[:6]:>6}  {r['job_id']}  {r['filename'][:48]}  {','.join(r['reasons'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
