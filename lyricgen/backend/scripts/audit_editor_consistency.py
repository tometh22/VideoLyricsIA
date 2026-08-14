"""Report Job/EditorDocument divergence before enabling Editor 2.0.

Read-only by design. Usage:
    python -m scripts.audit_editor_consistency [--tenant TENANT]
"""

from __future__ import annotations

import argparse
import json

from database import EditorDocument, EditorVersion, Job, SessionLocal


def audit(tenant_id: str | None = None) -> dict:
    db = SessionLocal()
    try:
        query = db.query(Job)
        if tenant_id:
            query = query.filter(Job.tenant_id == tenant_id)
        jobs = query.all()
        rows = []
        counts = {
            "ok": 0,
            "missing_document": 0,
            "revision_mismatch": 0,
            "content_mismatch": 0,
            "missing_current_version": 0,
            "version_content_mismatch": 0,
            "future_version": 0,
        }
        for job in jobs:
            document = db.query(EditorDocument).filter(EditorDocument.job_id == job.job_id).first()
            statuses = []
            if not document:
                statuses.append("missing_document")
            else:
                if int(document.revision or 0) != int(job.segments_revision or 0):
                    statuses.append("revision_mismatch")
                if (document.current_segments or []) != (job.segments_json or []):
                    statuses.append("content_mismatch")
                latest = db.query(EditorVersion).filter(
                    EditorVersion.job_id == job.job_id,
                ).order_by(EditorVersion.revision.desc()).first()
                current_version = db.query(EditorVersion).filter(
                    EditorVersion.job_id == job.job_id,
                    EditorVersion.revision == document.revision,
                ).first()
                if latest and latest.revision > document.revision:
                    statuses.append("future_version")
                if not current_version:
                    # Expected briefly while a draft checkpoint is waiting for
                    # the 5-second durable version, still useful before rollout.
                    statuses.append("missing_current_version")
                elif (current_version.segments or []) != (document.current_segments or []):
                    statuses.append("version_content_mismatch")
            if not statuses:
                counts["ok"] += 1
            else:
                for status in statuses:
                    counts[status] += 1
                rows.append({
                    "job_id": job.job_id,
                    "tenant_id": job.tenant_id,
                    "statuses": statuses,
                    "job_revision": int(job.segments_revision or 0),
                    "document_revision": int(document.revision or 0) if document else None,
                })
        return {"counts": counts, "issues": rows}
    finally:
        db.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tenant", default=None)
    args = parser.parse_args()
    print(json.dumps(audit(args.tenant), indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
