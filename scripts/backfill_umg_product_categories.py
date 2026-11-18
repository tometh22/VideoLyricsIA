"""Classify already published UMG deliveries using the staging job ledger.

Required environment: STAGING_DATABASE_URL and DELIVERIES_DATABASE_URL.
Dry run by default; --apply updates only known product categories. Never
modifies files, publication state, approvals, or download pointers.
"""

from __future__ import annotations

import argparse
import os
import sys
from collections import Counter
from pathlib import Path

import psycopg2

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lyricgen" / "backend"))
from delivery_products import product_category


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    staging_url = os.environ["STAGING_DATABASE_URL"]
    deliveries_url = os.environ["DELIVERIES_DATABASE_URL"]
    with psycopg2.connect(staging_url, sslmode="require") as staging, psycopg2.connect(deliveries_url, sslmode="require") as dest:
        with dest.cursor() as dcur, staging.cursor() as scur:
            dcur.execute("SELECT id, job_id, label, product_category FROM deliveries WHERE removed_at IS NULL AND portal_id = 'argentina'")
            rows = dcur.fetchall()
            scur.execute(
                "SELECT j.job_id, c.kind, c.default_render_params->>'product_channel', "
                "COALESCE(j.render_params->>'art_track', 'false') "
                "FROM jobs j LEFT JOIN batch_campaigns c ON c.id = j.campaign_id "
                "WHERE j.job_id = ANY(%s)",
                ([row[1] for row in rows],),
            )
            jobs = {job_id: (kind, channel, art_flag) for job_id, kind, channel, art_flag in scur.fetchall()}
            changes = []
            missing_jobs = 0
            for delivery_id, job_id, label, current in rows:
                record = jobs.get(job_id)
                if record is None:
                    missing_jobs += 1
                    continue
                kind, channel, art_flag = record
                kind = kind or ("art_track" if label == "Art Track" or art_flag.lower() == "true" else "lyric_video")
                target = product_category(kind, channel)
                if target is not None and current != target:
                    changes.append((target, delivery_id, current))
            print("active_argentina", len(rows), "matched_jobs", len(rows) - missing_jobs, "changes", len(changes))
            print("changes_by_category", dict(Counter(row[0] for row in changes)))
            if args.apply:
                for category, delivery_id, previous in changes:
                    dcur.execute(
                        "UPDATE deliveries SET product_category=%s WHERE id=%s AND product_category IS NOT DISTINCT FROM %s",
                        (category, delivery_id, previous),
                    )
                    if dcur.rowcount != 1:
                        raise RuntimeError("Delivery changed during backfill: " + str(delivery_id))
                dest.commit()
                print("applied", len(changes))
            else:
                dest.rollback()
                print("dry_run_only")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
