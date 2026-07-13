"""Read-only retrospective audit of Universal background safety evidence.

Run inside the target Railway environment so DATABASE_URL points at the
intended database:

    railway run --environment staging --service api \
      python scripts/audit_universal_safety.py --json

    railway run --environment production --service api \
      python scripts/audit_universal_safety.py --json --output /tmp/umg-audit.json

The command never changes a job. Legacy v4/v5 validation is reported as
legacy evidence, not silently upgraded to the current v6 byte attestation.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
from collections import Counter
from datetime import datetime, timezone
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from background_attestation import (  # noqa: E402
    BACKGROUND_ATTESTATION_VERSION,
    DELIVERY_ATTESTATION_VERSION,
)
from background_policy import POLICY_VERSION  # noqa: E402


VISIBLE_STATUSES = frozenset({"pending_review", "done", "approved"})


def _normalise(value: Any) -> str:
    return str(value or "").strip().lower().replace("-", "_")


def is_universal_account(tenant_id: Any, billing_group: Any) -> bool:
    tenant = _normalise(tenant_id)
    group = _normalise(billing_group)
    return bool(
        re.fullmatch(r"universal(?:_[a-z0-9_]+)?", tenant)
        or group in {"universal", "universal_music", "umg"}
    )


def classify_job(job, user) -> dict[str, Any]:
    validation = dict(job.validation_result or {})
    attestation = validation.get("attestation") or {}
    delivery = validation.get("delivery_attestation") or {}
    visible = job.status in VISIBLE_STATUSES
    current_policy = validation.get("policy_version") == POLICY_VERSION
    safe_verdict = bool(
        validation.get("passed") is True
        and validation.get("allow_people") is False
    )
    current_background_seal = bool(
        safe_verdict
        and current_policy
        and attestation.get("version") == BACKGROUND_ATTESTATION_VERSION
        and attestation.get("policy_version") == POLICY_VERSION
        and attestation.get("is_universal") is True
        and attestation.get("allow_people") is False
        and attestation.get("asset_sha256")
        and attestation.get("validation_sha256")
    )
    current_delivery_seal = bool(
        delivery.get("version") == DELIVERY_ATTESTATION_VERSION
        and delivery.get("policy_version") == POLICY_VERSION
        and delivery.get("background_sha256") == attestation.get("asset_sha256")
        and delivery.get("deliverables")
    )

    if visible and current_background_seal and current_delivery_seal:
        category = "current_attested_delivery"
    elif visible and safe_verdict:
        category = "visible_legacy_evidence_needs_revalidation"
    elif visible:
        category = "visible_without_safe_evidence"
    elif job.status == "validation_failed":
        category = "blocked_by_legacy_validator"
    elif current_background_seal:
        category = "current_background_attested_not_visible"
    elif safe_verdict:
        category = "nonvisible_legacy_evidence"
    else:
        category = "nonvisible_without_safe_evidence"

    return {
        "job_id": job.job_id,
        "created_at": job.created_at.isoformat() if job.created_at else None,
        "tenant_id": job.tenant_id,
        "billing_group": getattr(user, "billing_group", None),
        "status": job.status,
        "progress": job.progress,
        "policy_version": validation.get("policy_version"),
        "validation_passed": validation.get("passed"),
        "allow_people": validation.get("allow_people"),
        "validation_scope": validation.get("validation_scope"),
        "background_attested": current_background_seal,
        "delivery_attested": current_delivery_seal,
        "category": category,
        "error": (job.error or "")[:240] or None,
    }


def build_report(rows: list[tuple[Any, Any]]) -> dict[str, Any]:
    records = [
        classify_job(job, user)
        for job, user in rows
        if is_universal_account(job.tenant_id, getattr(user, "billing_group", None))
    ]
    categories = Counter(record["category"] for record in records)
    statuses = Counter(record["status"] for record in records)
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "policy_version": POLICY_VERSION,
        "read_only": True,
        "universal_jobs": len(records),
        "categories": dict(sorted(categories.items())),
        "statuses": dict(sorted(statuses.items())),
        "requires_attention": [
            record for record in records
            if record["category"] in {
                "visible_legacy_evidence_needs_revalidation",
                "visible_without_safe_evidence",
                "blocked_by_legacy_validator",
            }
        ],
        "records": records,
    }


def deep_local_scan(rows: list[tuple[Any, Any]], *, limit: int | None = None) -> dict:
    """Re-scan cached backgrounds with the provider-free detector.

    This does not write validation_result or change a job. It is suitable for
    retrospective incident review without paying for a second Gemini pass.
    """
    import storage
    from content_validator import (
        LOCAL_VALIDATOR_VERSION,
        LocalDetectorCheckError,
        _check_frame_with_local_detector,
        _cleanup_extracted_frames,
        _extract_frames_with_opencv,
    )

    targets = [
        (job, user) for job, user in rows
        if job.status in VISIBLE_STATUSES
        and job.bg_r2_key_cached
        and is_universal_account(job.tenant_id, getattr(user, "billing_group", None))
    ]
    if limit is not None:
        targets = targets[:max(0, limit)]

    results = []
    for index, (job, _user) in enumerate(targets, start=1):
        print(
            f"deep-local {index}/{len(targets)} job={job.job_id}",
            file=sys.stderr,
            flush=True,
        )
        suffix = os.path.splitext(str(job.bg_r2_key_cached))[1] or ".mp4"
        with tempfile.TemporaryDirectory(prefix="genly_umg_audit_") as work_dir:
            asset_path = os.path.join(work_dir, f"background{suffix}")
            if not storage.download_object(job.bg_r2_key_cached, asset_path):
                results.append({
                    "job_id": job.job_id,
                    "result": "download_error",
                    "validator": LOCAL_VALIDATOR_VERSION,
                })
                continue

            frame_paths: list[str] = []
            frame_dir: str | None = None
            planned = 0
            checked = 0
            evidence: list[dict[str, Any]] = []
            errors = 0
            try:
                if suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"}:
                    frame_paths, frame_dir, planned = [asset_path], None, 1
                else:
                    frame_paths, frame_dir, planned = _extract_frames_with_opencv(
                        asset_path, interval_seconds=0.25, max_frames=192
                    )
                for frame_index, frame_path in enumerate(frame_paths):
                    try:
                        verdict = _check_frame_with_local_detector(frame_path)
                    except LocalDetectorCheckError as exc:
                        errors += 1
                        evidence.append({
                            "frame": frame_index,
                            "error": str(exc)[:160],
                        })
                        break
                    checked += 1
                    if verdict.get("people") is True:
                        evidence.append({
                            "frame": frame_index,
                            "evidence": verdict.get("evidence") or [],
                        })
                        break
            finally:
                _cleanup_extracted_frames(frame_paths, frame_dir)

            complete = bool(planned > 0 and checked == planned and errors == 0)
            result = (
                "human_evidence" if evidence and errors == 0
                else "clean" if complete
                else "incomplete"
            )
            results.append({
                "job_id": job.job_id,
                "result": result,
                "frames_checked": checked,
                "frames_planned": planned,
                "errors": errors,
                "evidence": evidence,
                "validator": LOCAL_VALIDATOR_VERSION,
            })

    counts = Counter(item["result"] for item in results)
    return {
        "read_only": True,
        "targets": len(targets),
        "counts": dict(sorted(counts.items())),
        "results": results,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="Print full JSON report")
    parser.add_argument("--output", help="Also write the JSON report to this path")
    parser.add_argument(
        "--fail-on-gap",
        action="store_true",
        help="Exit 2 when a visible Universal delivery lacks current v6 seals",
    )
    parser.add_argument(
        "--deep-local",
        action="store_true",
        help="Download visible cached backgrounds and re-scan them locally (read-only)",
    )
    parser.add_argument(
        "--deep-local-limit",
        type=int,
        help="Cap the number of cached backgrounds scanned (default: all)",
    )
    args = parser.parse_args()

    if not os.environ.get("DATABASE_URL"):
        parser.error("DATABASE_URL is required; run inside the Railway environment")

    from database import Job, SessionLocal, User

    db = SessionLocal()
    try:
        rows = db.query(Job, User).join(User, User.id == Job.user_id).all()
    finally:
        # Deep scans can take minutes. Detach the fully-loaded rows and release
        # PostgreSQL before any R2 download/CV work so an idle SSL timeout at
        # the end cannot discard an otherwise complete read-only report.
        try:
            db.expunge_all()
            db.close()
        except Exception:
            pass

    report = build_report(rows)
    if args.deep_local:
        report["deep_local"] = deep_local_scan(
            rows, limit=args.deep_local_limit
        )

    payload = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True)
    if args.output:
        with open(args.output, "w", encoding="utf-8") as handle:
            handle.write(payload + "\n")
    if args.json:
        print(payload)
    else:
        print(f"Universal jobs: {report['universal_jobs']}")
        for category, count in report["categories"].items():
            print(f"  {category}: {count}")
        print(f"Requires attention: {len(report['requires_attention'])}")

    visible_gaps = sum(
        count for category, count in report["categories"].items()
        if category in {
            "visible_legacy_evidence_needs_revalidation",
            "visible_without_safe_evidence",
        }
    )
    return 2 if args.fail_on_gap and visible_gaps else 0


if __name__ == "__main__":
    raise SystemExit(main())
