#!/usr/bin/env python3
"""Read-only report for the paired LoRA-v1 consensus shadow.

The transcription worker stores the paired with/without-LoRA counters in
``Job.transcription_quality.retry.lora_shadow``.  This command deliberately
does not read lyric text or audio and never updates a job.  It reports the
first N songs with an actual LoRA comparison so the second evaluation can be
run continuously on real traffic.

Examples::

    python scripts/report_lora_shadow.py --limit 50
    python scripts/report_lora_shadow.py --limit 30 --json
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

from database import Job, SessionLocal  # noqa: E402


def _shadow(quality: Any) -> dict[str, int | bool] | None:
    if not isinstance(quality, dict):
        return None
    retry = quality.get("retry")
    payload = retry.get("lora_shadow") if isinstance(retry, dict) else None
    if not isinstance(payload, dict):
        return None
    try:
        comparisons = max(0, int(payload.get("comparisons") or 0))
    except (TypeError, ValueError):
        comparisons = 0
    if comparisons == 0:
        return None
    keys = (
        "with_consensus", "without_consensus", "lora_contributed_lines",
        "new_consensus_lines", "lost_consensus_lines",
    )
    result: dict[str, int | bool] = {
        "enabled": bool(payload.get("enabled", True)),
        "comparisons": comparisons,
    }
    for key in keys:
        try:
            result[key] = max(0, int(payload.get(key) or 0))
        except (TypeError, ValueError):
            result[key] = 0
    return result


def collect(limit: int, since: datetime | None = None) -> dict[str, Any]:
    db = SessionLocal()
    try:
        query = db.query(Job).order_by(Job.created_at.asc())
        if since is not None:
            query = query.filter(Job.created_at >= since)
        rows: list[dict[str, Any]] = []
        # The limit applies to songs with observations, not arbitrary jobs
        # that predate the LoRA family or did not enter quality replay.
        for job in query.yield_per(100):
            shadow = _shadow(job.transcription_quality)
            if shadow is None:
                continue
            rows.append({
                "job_id": str(job.job_id),
                "created_at": (
                    job.created_at.isoformat() if job.created_at else None
                ),
                "shadow": shadow,
            })
            if len(rows) >= limit:
                break
    finally:
        db.close()

    totals = {
        key: sum(int(row["shadow"].get(key) or 0) for row in rows)
        for key in (
            "comparisons", "with_consensus", "without_consensus",
            "lora_contributed_lines", "new_consensus_lines",
            "lost_consensus_lines",
        )
    }
    total_with = totals["with_consensus"]
    totals["new_consensus_rate"] = round(
        totals["new_consensus_lines"] / total_with, 4
    ) if total_with else None
    return {
        "schema": "lora-v1-shadow-report-v1",
        "limit": limit,
        "songs_observed": len(rows),
        "songs": rows,
        "totals": totals,
        "replacement_allowed": False,
    }


def _parse_since(value: str | None) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=50)
    parser.add_argument("--since", help="ISO-8601 lower bound for job creation")
    parser.add_argument("--json", action="store_true", dest="as_json")
    args = parser.parse_args()
    if args.limit < 1 or args.limit > 500:
        parser.error("--limit debe estar entre 1 y 500")
    try:
        report = collect(args.limit, _parse_since(args.since))
    except (TypeError, ValueError) as exc:
        parser.error(str(exc))
    if args.as_json:
        print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    else:
        totals = report["totals"]
        print(
            f"LoRA shadow: {report['songs_observed']} canciones, "
            f"{totals['comparisons']} comparaciones, "
            f"{totals['new_consensus_lines']} líneas nuevas, "
            f"{totals['lost_consensus_lines']} pérdidas"
        )
        print(json.dumps(totals, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
