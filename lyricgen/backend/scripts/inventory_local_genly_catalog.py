#!/usr/bin/env python3
"""Read-only, metadata-only inventory of approved portal deliveries.

No inference, downloads, or writes to the database. Publication counters are
NOT editor revisions; this inventory does not manufacture training labels.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import json
import os
from pathlib import Path


def inventory(connection, *, environment: str, page_size: int = 100) -> dict:
    from psycopg2.extras import RealDictCursor

    if not 1 <= page_size <= 500:
        raise ValueError("page_size must be between 1 and 500")
    connection.set_session(readonly=True, isolation_level="REPEATABLE READ")
    deliveries = []
    cursor_id = 0
    with connection.cursor(cursor_factory=RealDictCursor) as cursor:
        cursor.execute("SET LOCAL statement_timeout = '15000ms'")
        while True:
            cursor.execute("""
                SELECT d.id, d.job_id, d.portal_id, d.approved_at,
                       d.published_revision AS publication_counter,
                       d.published_render_fingerprint, d.content_updated_at,
                       d.stale_since, d.artist_snapshot,
                       (d.published_file_keys IS NOT NULL AND d.published_file_keys <> '{}'::jsonb)
                           AS has_published_keys,
                       (j.job_id IS NOT NULL) AS job_present,
                       j.input_audio_sha256, j.audio_revision,
                       (COALESCE(j.input_r2_key, '') <> '') AS has_source_audio_key,
                       j.machine_snapshot_required,
                       (CASE WHEN jsonb_typeof(ed.original_segments)='array'
                             THEN jsonb_array_length(ed.original_segments)>0 ELSE FALSE END)
                           AS has_original_segments,
                       EXISTS(SELECT 1 FROM editor_versions ev
                              WHERE ev.job_id=d.job_id AND ev.is_approved)
                           AS has_approved_editor_version
                FROM deliveries d
                LEFT JOIN jobs j ON j.job_id=d.job_id
                LEFT JOIN editor_documents ed ON ed.job_id=d.job_id
                WHERE d.removed_at IS NULL AND d.approved_at IS NOT NULL
                      AND d.id > %s
                ORDER BY d.id LIMIT %s
            """, (cursor_id, page_size))
            page = [dict(row) for row in cursor.fetchall()]
            if not page:
                break
            deliveries.extend(page)
            cursor_id = page[-1]["id"]
    return {
        "schema": "local-genly-catalog-inventory-v1",
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "environment": environment, "read_only": True,
        "contains_audio_or_lyrics": False,
        "summary": {
            "approved_active_deliveries": len(deliveries),
            "distinct_jobs": len({row["job_id"] for row in deliveries}),
            "distinct_audio_hashes": len({row["input_audio_sha256"] for row in deliveries
                                          if row["input_audio_sha256"]}),
            "by_portal": dict(Counter(row["portal_id"] for row in deliveries)),
            "missing_job": sum(not row["job_present"] for row in deliveries),
            "missing_audio_hash": sum(not row["input_audio_sha256"] for row in deliveries),
            "missing_original": sum(not row["has_original_segments"] for row in deliveries),
            "missing_approved_editor_version": sum(not row["has_approved_editor_version"]
                                                    for row in deliveries),
            "stale": sum(row["stale_since"] is not None for row in deliveries),
        },
        "label_status": "requires_exact_render_to_editor_revision_link",
        "deliveries": deliveries,
    }


def write_private(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, "w") as stream:
        json.dump(payload, stream, ensure_ascii=False, indent=2, default=str)
        stream.write("\n")


def locate_source_jobs(connection, job_ids: list[str]) -> list[dict]:
    """Locate cross-environment history without equating it to an approved cut."""
    from psycopg2.extras import RealDictCursor
    connection.set_session(readonly=True, isolation_level="REPEATABLE READ")
    rows = []
    identities = sorted(set(job_ids))
    with connection.cursor(cursor_factory=RealDictCursor) as cursor:
        cursor.execute("SET LOCAL statement_timeout = '15000ms'")
        for offset in range(0, len(identities), 100):
            cursor.execute("""
                SELECT j.job_id, j.artist, j.song_title, j.input_audio_sha256,
                       j.audio_revision, j.segments_revision,
                       (COALESCE(j.input_r2_key, '') <> '') AS has_source_audio_key,
                       j.machine_snapshot_required,
                       (CASE WHEN jsonb_typeof(ed.original_segments)='array'
                             THEN jsonb_array_length(ed.original_segments)>0 ELSE FALSE END)
                           AS has_original_segments,
                       (SELECT COUNT(*) FROM editor_versions ev WHERE ev.job_id=j.job_id)
                           AS editor_version_count,
                       EXISTS(SELECT 1 FROM editor_versions ev
                              WHERE ev.job_id=j.job_id AND ev.is_approved)
                           AS has_approved_editor_version
                FROM jobs j LEFT JOIN editor_documents ed ON ed.job_id=j.job_id
                WHERE j.job_id=ANY(%s) ORDER BY j.job_id
            """, (identities[offset:offset + 100],))
            rows.extend(dict(row) for row in cursor.fetchall())
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-url-env", default="GENLY_AUDIT_DATABASE_URL")
    parser.add_argument("--environment", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--source-jobs-from", type=Path,
                        help="Locate source metadata for another environment's inventory")
    args = parser.parse_args()
    import psycopg2
    try:
        with psycopg2.connect(os.environ[args.database_url_env], connect_timeout=10,
                              options="-c default_transaction_read_only=on") as connection:
            if args.source_jobs_from:
                previous = json.loads(args.source_jobs_from.read_text())
                ids = [row["job_id"] for row in previous["deliveries"]]
                rows = locate_source_jobs(connection, ids)
                report = {"schema": "local-genly-source-locations-v1", "read_only": True,
                          "environment": args.environment,
                          "portal_environment": previous["environment"],
                          "label_status": "publication_link_not_verified",
                          "summary": {"portal_jobs": len(set(ids)), "located_jobs": len(rows)},
                          "jobs": rows}
            else:
                report = inventory(connection, environment=args.environment)
        write_private(args.output, report)
    except Exception as exc:
        # Database exception text can contain connection credentials or hosts.
        print(json.dumps({"status": "failed", "error_type": type(exc).__name__}))
        return 2
    print(json.dumps(report["summary"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
