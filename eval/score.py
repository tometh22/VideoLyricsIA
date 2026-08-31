#!/usr/bin/env python3
"""Score canonical hypotheses and emit auditable per-song/aggregate artifacts."""

from __future__ import annotations

import argparse
import csv
import json
from collections import Counter
from pathlib import Path
from typing import Any

from eval.canonical import read_json, segments_to_lines, write_json
from eval.metrics import (
    NORMALIZATION_VERSION, aggregate_edit_effort, aggregate_song_metrics,
    score_edit_effort, score_song,
)
from eval.raw_cohort import RAW_TRUSTED


def _hypothesis(golden_case: Path, variant: str, hypothesis_root: Path | None) -> list[dict[str, Any]] | None:
    if variant == "prod_raw":
        path = golden_case / "raw_pipeline_output.json"
        if not path.is_file():
            return None
        return segments_to_lines(read_json(path)["segments"])
    if hypothesis_root is None:
        raise ValueError("--hypothesis-root is required outside prod_raw")
    path = hypothesis_root / golden_case.name / "hypothesis.json"
    if not path.is_file():
        return None
    payload = read_json(path)
    return payload["lines"] if isinstance(payload, dict) and "lines" in payload else payload


def run(golden: Path, variant: str, output: Path, hypothesis_root: Path | None = None) -> dict:
    manifest = read_json(golden / "manifest.json")
    output.mkdir(parents=True, exist_ok=True)
    rows, skipped, taxonomy = [], [], Counter()
    fieldnames = [
        "song_id", "ref_idx", "hyp_idx", "type", "ref_text", "hyp_text",
        "ref_start", "hyp_start", "ref_end", "hyp_end",
    ]
    all_errors = []
    edit_effort = []
    for item in manifest["cases"]:
        song_id = item["song_id"]
        case = golden / item["path"]
        hypothesis = _hypothesis(case, variant, hypothesis_root)
        if hypothesis is None:
            skipped.append({"song_id": song_id, "reason": "hypothesis_unavailable"})
            continue
        reference = read_json(case / "lines.json")
        reference_words = read_json(case / "words.json") if (case / "words.json").is_file() else None
        metrics, alignment, errors = score_song(
            song_id, reference, hypothesis, reference_words=reference_words,
        )
        meta = read_json(case / "meta.json")
        metrics.update({
            "raw_quality": meta["raw_quality"],
            "job_origin": meta["job_origin"],
            "historical": bool(meta.get("raw_historical")),
        })
        case_out = output / song_id
        write_json(case_out / "hypothesis.json", hypothesis)
        write_json(case_out / "alignment.json", alignment)
        write_json(case_out / "metrics.json", metrics)
        with (case_out / "errors.csv").open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(errors)
        rows.append(metrics)
        all_errors.extend(errors)
        taxonomy.update(error["type"] for error in errors)
        edits_path = case / "edits.json"
        if edits_path.is_file():
            effort = score_edit_effort(read_json(edits_path), len(hypothesis))
            effort["song_id"] = song_id
            effort["raw_quality"] = meta["raw_quality"]
            edit_effort.append(effort)

    exact_reconstructed = [row for row in rows if row["raw_quality"] in RAW_TRUSTED]
    all_historical = [row for row in rows if row["raw_quality"] in {"exact", "reconstructed", "estimated"}]
    summary = {
        "schema_version": 1,
        "variant": variant,
        "normalization_version": NORMALIZATION_VERSION,
        "cohorts": {
            "raw_exact_plus_reconstructed": aggregate_song_metrics(exact_reconstructed),
            "raw_all_57": aggregate_song_metrics(all_historical),
            "all_available_hypotheses": aggregate_song_metrics(rows),
        },
        "taxonomy": dict(sorted(taxonomy.items())),
        "edit_effort": {
            "aggregate": aggregate_edit_effort(edit_effort),
            "songs": edit_effort,
        },
        "scored_songs": len(rows),
        "skipped": skipped,
    }
    write_json(output / "summary.json", summary)
    with (output / "errors.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(all_errors)
    (output / "summary.md").write_text(render_markdown(summary), encoding="utf-8")
    return summary


def render_markdown(summary: dict[str, Any]) -> str:
    lines = [
        f"# {summary['variant']}", "",
        "| Cohorte | Canciones | WER main corpus | Perfectas | Casi perfectas |",
        "|---|---:|---:|---:|---:|",
    ]
    for name, values in summary["cohorts"].items():
        if not values.get("songs"):
            lines.append(f"| {name} | 0 | — | — | — |")
            continue
        lines.append(
            f"| {name} | {values['songs']} | {100 * values['wer_main_corpus']:.2f}% | "
            f"{100 * values['song_perfect_pct']:.2f}% | {100 * values['song_near_perfect_pct']:.2f}% |"
        )
    lines.extend(["", f"Omitidas: {summary['taxonomy'].get('omitted_other', 0) + summary['taxonomy'].get('omitted_repeat', 0)}. Inventadas: {summary['taxonomy'].get('invented', 0)}.", ""])
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--golden", type=Path, default=Path("eval/golden"))
    parser.add_argument("--variant", required=True)
    parser.add_argument("--hypothesis-root", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    output = args.output or Path("eval/runs") / args.variant
    report = run(args.golden.resolve(), args.variant, output.resolve(), args.hypothesis_root)
    print(render_markdown(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
