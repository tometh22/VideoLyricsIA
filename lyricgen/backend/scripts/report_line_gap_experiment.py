#!/usr/bin/env python3
"""Resultado de la prueba A/B del aire mínimo (LYRIC_MIN_GAP_AB), por brazo.

Solo lectura. Lee las asignaciones (``experiment.lyric_min_gap.assigned``)
y la exposición (``.exposure``) del AuditLog, y para cada job asignado que ya
tenga primera aprobación mide, con las mismas reglas que la fase 1:

- ediciones de inicio y de fin por job (>= 50 ms) y su magnitud mediana;
- minutos activos por job hasta la primera aprobación (latidos, huecos
  <= 25 s, todas las sesiones);
- líneas recortadas por el tratamiento (exposición).

Uso:
    DATABASE_URL=... python3.11 scripts/report_line_gap_experiment.py --json-out /tmp/ab.json
"""
from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import timezone
import importlib.util
import json
import os
from pathlib import Path
import statistics
import sys

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

from training_corpus import _finite_time, _match_rows  # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "report_review_baseline", BACKEND / "scripts" / "report_review_baseline.py",
)
baseline = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(baseline)

ASSIGNED = "experiment.lyric_min_gap.assigned"
EXPOSURE = "experiment.lyric_min_gap.exposure"
EDIT_S = 0.05


def boundary_edits(machine, approved) -> dict:
    """Ediciones de inicio y fin (>= 50 ms) en líneas emparejadas."""
    machine = [dict(r) for r in (machine or []) if isinstance(r, dict)]
    approved = [dict(r) for r in (approved or []) if isinstance(r, dict)]
    matched, _removed, _inserted, complete = _match_rows(machine, approved)
    starts, ends = [], []
    if complete:
        for b, a, _ in matched:
            ds = abs(_finite_time(approved[a].get("start")) - _finite_time(machine[b].get("start")))
            de = abs(_finite_time(approved[a].get("end")) - _finite_time(machine[b].get("end")))
            if ds >= EDIT_S:
                starts.append(ds)
            if de >= EDIT_S:
                ends.append(de)
    return {"complete": bool(complete), "lines": len(approved), "starts": starts, "ends": ends}


def summarize_arm(rows: list[dict]) -> dict:
    approved = [r for r in rows if r["approved"]]
    timed = [r for r in approved if r["heartbeats"]]

    def med(values):
        return round(statistics.median(values), 3) if values else None

    starts = [v for r in approved for v in r["edits"]["starts"]]
    ends = [v for r in approved for v in r["edits"]["ends"]]
    return {
        "assigned": len(rows),
        "approved": len(approved),
        "start_edits_per_job_median": med([len(r["edits"]["starts"]) for r in approved]),
        "end_edits_per_job_median": med([len(r["edits"]["ends"]) for r in approved]),
        "start_edits_per_line": round(len(starts) / max(1, sum(r["edits"]["lines"] for r in approved)), 4),
        "end_edits_per_line": round(len(ends) / max(1, sum(r["edits"]["lines"] for r in approved)), 4),
        "start_edit_magnitude_median_s": med(starts),
        "end_edit_magnitude_median_s": med(ends),
        "minutes_per_job_median": med([r["active_minutes"] for r in timed]),
        "minutes_per_job_p90": (
            round(sorted(r["active_minutes"] for r in timed)[int(0.9 * (len(timed) - 1))], 2)
            if timed else None
        ),
        "lines_trimmed_per_job_median": med([r["lines_trimmed"] for r in rows if r["lines_trimmed"] is not None]),
    }


def collect(conn) -> list[dict]:
    cur = conn.cursor()
    cur.execute("select detail from audit_log where action = %s order by id", (ASSIGNED,))
    assignments = {}
    for (detail,) in cur.fetchall():
        if isinstance(detail, dict) and detail.get("job_id") and detail["job_id"] not in assignments:
            assignments[detail["job_id"]] = detail
    if not assignments:
        return []
    job_ids = list(assignments)
    cur.execute("select detail from audit_log where action = %s", (EXPOSURE,))
    exposure = {}
    for (detail,) in cur.fetchall():
        if isinstance(detail, dict) and detail.get("job_id"):
            exposure[detail["job_id"]] = detail
    cur.execute(
        """
        select job_id, revision, reason, is_approved, created_at, segments
          from editor_versions where job_id = any(%s)
        """,
        (job_ids,),
    )
    versions: dict[str, list[dict]] = defaultdict(list)
    for job_id, revision, reason, is_approved, created_at, segments in cur.fetchall():
        versions[job_id].append({"revision": revision, "reason": reason, "is_approved": is_approved,
                                 "created_at": created_at, "segments": segments})
    cur.execute(
        """
        select job_id, user_id, coalesce(occurred_at, created_at) from product_events
         where name = 'editor_activity_heartbeat' and job_id = any(%s)
        """,
        (job_ids,),
    )
    beats: dict[str, dict] = defaultdict(lambda: defaultdict(list))
    for job_id, user_id, when in cur.fetchall():
        if when is not None:
            beats[job_id][user_id].append(when if when.tzinfo else when.replace(tzinfo=timezone.utc))

    rows = []
    for job_id, assignment in assignments.items():
        chosen = baseline.pick_versions(versions.get(job_id, []))
        approved = chosen["approved"]
        edits = (boundary_edits(chosen["machine"]["segments"], approved["segments"])
                 if approved and chosen["machine"] else {"complete": False, "lines": 0, "starts": [], "ends": []})
        seconds, count = 0.0, 0
        if approved:
            cutoff = approved["created_at"] + baseline.APPROVAL_SLACK
            for stamps in beats.get(job_id, {}).values():
                inside = sorted(t for t in stamps if t <= cutoff)
                count += len(inside)
                if inside:
                    seconds += baseline.active_seconds(inside, 25.0)
        expo = exposure.get(job_id) or {}
        rows.append({
            "job_id": job_id,
            "arm": assignment.get("arm"),
            "gap_ms": assignment.get("gap_ms"),
            "approved": approved is not None,
            "edits": edits,
            "heartbeats": count,
            "active_minutes": round(seconds / 60.0, 2),
            "lines_trimmed": expo.get("lines_trimmed"),
            "lines_word_floor": expo.get("lines_word_floor"),
        })
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--json-out", default="")
    args = parser.parse_args()
    url = os.environ.get("DATABASE_URL", "")
    if not url:
        print("DATABASE_URL requerido", file=sys.stderr)
        return 2
    import psycopg2

    conn = psycopg2.connect(url, options="-c default_transaction_read_only=on -c statement_timeout=300000")
    try:
        rows = collect(conn)
    finally:
        conn.close()
    by_arm: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        by_arm[str(row["arm"])].append(row)
    report = {"arms": {arm: summarize_arm(subset) for arm, subset in sorted(by_arm.items())}}
    text = json.dumps(report, ensure_ascii=False, indent=1)
    if args.json_out:
        Path(args.json_out).write_text(text)
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
