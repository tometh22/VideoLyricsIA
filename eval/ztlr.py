"""Zero-touch line rate (ZTLR) for the historical human-approved corpus.

ZTLR is intentionally computed from the pre-human snapshot and the approved
snapshot.  A line is zero-touch only when its text and both display boundaries
survive unchanged.  Added and deleted lines are work units too, so the
denominator is the union produced by the one-to-one line alignment rather than
only the final line count.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Sequence

from eval.bootstrap import song_bootstrap_ci
from eval.canonical import read_json, segments_to_lines, write_json
from eval.metrics import align_lines, normalize_text


NET_UNTOUCHED_TOLERANCE_S = 0.001


def _song_row(song_id: str, raw: Sequence[dict[str, Any]], approved: Sequence[dict[str, Any]]) -> dict[str, Any]:
    raw_lines = segments_to_lines(raw)
    approved_lines = segments_to_lines(approved)
    alignment = align_lines(approved_lines, raw_lines)
    zero_touch = text_touch = timing_touch = text_and_timing_touch = 0
    units: list[dict[str, Any]] = []

    for match in alignment["matches"]:
        approved_idx, raw_idx = match["ref_idx"], match["hyp_idx"]
        before, after = raw_lines[raw_idx], approved_lines[approved_idx]
        text_changed = normalize_text(before["text"]) != normalize_text(after["text"])
        timing_changed = (
            abs(before["start_s"] - after["start_s"]) > NET_UNTOUCHED_TOLERANCE_S
            or abs(before["end_s"] - after["end_s"]) > NET_UNTOUCHED_TOLERANCE_S
        )
        if not text_changed and not timing_changed:
            zero_touch += 1
            category = "zero_touch"
        elif text_changed and timing_changed:
            text_and_timing_touch += 1
            category = "text_and_timing"
        elif text_changed:
            text_touch += 1
            category = "text_only"
        else:
            timing_touch += 1
            category = "timing_only"
        units.append({
            "category": category,
            "raw_idx": raw_idx,
            "approved_idx": approved_idx,
            "raw_text": before["text"],
            "approved_text": after["text"],
            "start_delta_ms": round((after["start_s"] - before["start_s"]) * 1000.0, 3),
            "end_delta_ms": round((after["end_s"] - before["end_s"]) * 1000.0, 3),
        })

    for raw_idx in alignment["invented_hyp_indices"]:
        text_and_timing_touch += 1
        units.append({
            "category": "deleted_line", "raw_idx": raw_idx, "approved_idx": None,
            "raw_text": raw_lines[raw_idx]["text"], "approved_text": None,
        })
    for approved_idx in alignment["omitted_ref_indices"]:
        text_and_timing_touch += 1
        units.append({
            "category": "added_line", "raw_idx": None, "approved_idx": approved_idx,
            "raw_text": None, "approved_text": approved_lines[approved_idx]["text"],
        })

    total = len(units)
    return {
        "song_id": song_id,
        "raw_lines": len(raw_lines),
        "approved_lines": len(approved_lines),
        "work_units": total,
        "zero_touch_lines": zero_touch,
        "ztlr": zero_touch / max(1, total),
        "text_only_touched": text_touch,
        "timing_only_touched": timing_touch,
        "text_and_timing_touched": text_and_timing_touch,
        "units": units,
    }


def _ratio(rows: Sequence[dict[str, Any]]) -> float:
    return sum(row["zero_touch_lines"] for row in rows) / max(1, sum(row["work_units"] for row in rows))


def calculate(golden: Path, output: Path, qualities: set[str]) -> dict[str, Any]:
    manifest = read_json(golden / "manifest.json")
    rows = []
    for item in manifest["cases"]:
        if item["raw_quality"] not in qualities:
            continue
        case = golden / item["path"]
        raw = read_json(case / "raw_pipeline_output.json")["segments"]
        approved = read_json(case / "approved.json")
        rows.append(_song_row(item["song_id"], raw, approved))

    total_units = sum(row["work_units"] for row in rows)
    zero_touch = sum(row["zero_touch_lines"] for row in rows)
    category_counts: dict[str, int] = {}
    for row in rows:
        for unit in row["units"]:
            category = unit["category"]
            category_counts[category] = category_counts.get(category, 0) + 1
    report = {
        "schema_version": 1,
        "definition": {
            "name": "zero-touch line rate",
            "numerator": "aligned line with normalized text and both display boundaries unchanged",
            "denominator": "matched + added + deleted line work units",
            "timing_tolerance_ms": NET_UNTOUCHED_TOLERANCE_S * 1000,
            "source": "pre-human snapshot versus approved snapshot",
            "warning": "net historical ZTLR; old UI sessions did not persist a reliable active-review timer",
        },
        "cohort": sorted(qualities),
        "songs": len(rows),
        "work_units": total_units,
        "zero_touch_lines": zero_touch,
        "ztlr": _ratio(rows),
        "ztlr_song_bootstrap_ci": song_bootstrap_ci(rows, _ratio),
        "category_counts": dict(sorted(category_counts.items())),
        "minutes": {
            "status": "NOT_HISTORICALLY_MEASURABLE",
            "reason": "audit events preserve edits but not editor foreground/active time; wall-clock spans include multi-day gaps",
            "next_measurement": "use the staging review timer for actual before/after minutes",
        },
        "by_song": rows,
    }
    write_json(output, report)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--golden", type=Path, default=Path("eval/golden"))
    parser.add_argument("--output", type=Path, default=Path("eval/runs/ztlr_baseline/report.json"))
    args = parser.parse_args()
    report = calculate(args.golden.resolve(), args.output.resolve(), {"exact", "reconstructed"})
    print(json.dumps({key: report[key] for key in ("songs", "work_units", "zero_touch_lines", "ztlr", "ztlr_song_bootstrap_ci", "category_counts", "minutes")}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
