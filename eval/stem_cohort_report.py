#!/usr/bin/env python3
"""Split realignment and MSS-ALT metrics by the 26/15 stem cohorts."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Sequence

from eval.bootstrap import percentile, song_bootstrap_ci
from eval.canonical import read_json, write_json
from eval.stem_cohort_audit import RUNPOD_ORIGIN


def cohort_ids(manifest: dict[str, Any]) -> dict[str, set[str]]:
    new = {
        str(row["song_id"]) for row in manifest["cases"]
        if row.get("origin") == RUNPOD_ORIGIN
    }
    previous = {str(row["song_id"]) for row in manifest["cases"]} - new
    return {"previous_26": previous, "runpod_15": new}


def _realign_metrics(rows: Sequence[dict[str, Any]]) -> dict[str, Any]:
    def boundaries(sample):
        return [float(value) for row in sample for value in row["boundary_abs_ms"]]

    def quantile(sample, fraction):
        values = boundaries(sample)
        return percentile(values, fraction) if values else float("inf")

    def weighted(sample, numerator, denominator):
        return sum(float(row[numerator]) * int(row[denominator]) for row in sample) / max(
            1, sum(int(row[denominator]) for row in sample)
        )

    return {
        "songs_scored": len(rows),
        "boundaries": len(boundaries(rows)),
        "p50_boundary_abs_ms": song_bootstrap_ci(rows, lambda sample: quantile(sample, 0.50)),
        "p90_boundary_abs_ms": song_bootstrap_ci(rows, lambda sample: quantile(sample, 0.90)),
        "within_150ms_both": song_bootstrap_ci(
            rows, lambda sample: weighted(sample, "within_150ms_both", "approved_lines"),
        ),
        "coverage": song_bootstrap_ci(
            rows, lambda sample: sum(int(row["aligned_lines"]) for row in sample) /
            max(1, sum(int(row["approved_lines"]) for row in sample)),
        ),
    }


def _mss_metrics(rows: Sequence[dict[str, Any]]) -> dict[str, Any]:
    def wer(sample, family):
        edits = sum(int(row["families"][family]["word_edits"]) for row in sample)
        words = sum(int(row["families"][family]["reference_words"]) for row in sample)
        return edits / max(1, words)

    def relative(sample):
        native = wer(sample, "native")
        mss = wer(sample, "mss_rms_vad")
        return (native - mss) / max(1e-12, native)

    regressions = []
    fixed = regressed = 0
    for row in rows:
        native = row["families"]["native"]
        mss = row["families"]["mss_rms_vad"]
        delta = float(mss["wer"]) - float(native["wer"])
        if delta > 0.02:
            regressions.append({"song_id": row["song_id"], "absolute_wer_regression": delta})
        native_correct = set(native["correct_reference_line_indices"])
        mss_correct = set(mss["correct_reference_line_indices"])
        fixed += len(mss_correct - native_correct)
        regressed += len(native_correct - mss_correct)
    return {
        "songs_scored": len(rows),
        "native_wer": song_bootstrap_ci(rows, lambda sample: wer(sample, "native")),
        "mss_rms_vad_wer": song_bootstrap_ci(rows, lambda sample: wer(sample, "mss_rms_vad")),
        "paired_relative_wer_improvement": song_bootstrap_ci(rows, relative),
        "songs_regressing_more_than_2pct_absolute": regressions,
        "reference_lines": {"fixed": fixed, "regressed": regressed, "net": fixed - regressed},
    }


def run(
    stems_manifest: Path, global_report: Path, hierarchical_report: Path,
    mss_report: Path, output: Path,
) -> dict[str, Any]:
    manifest = read_json(stems_manifest)
    cohorts = cohort_ids(manifest)
    origins = {}
    for row in manifest["cases"]:
        origins[str(row.get("origin"))] = origins.get(str(row.get("origin")), 0) + 1

    global_payload = read_json(global_report)["aligners"]["current_xlsr"]
    hierarchical_payload = read_json(hierarchical_report)["aligners"]["current_xlsr_hierarchical"]
    mss_payload = read_json(mss_report)
    report: dict[str, Any] = {
        "schema_version": 1,
        "experiment": "stem-origin-cohort-comparability",
        "cohort_definition": {
            "previous_26": "all cached stems present before the RunPod completion; includes 21 documented local MPS and 5 legacy with unrecorded origin",
            "runpod_15": "15 missing stems generated on RunPod CUDA with mdx_extra / Demucs 4.0.1",
            "origin_counts": origins,
        },
        "cohorts": {},
    }
    for name, ids in cohorts.items():
        global_rows = [row for row in global_payload["by_song"] if row["song_id"] in ids]
        hierarchical_rows = [row for row in hierarchical_payload["by_song"] if row["song_id"] in ids]
        mss_rows = [row for row in mss_payload["by_song"] if row["song_id"] in ids]
        report["cohorts"][name] = {
            "expected_songs": len(ids),
            "song_ids": sorted(ids),
            "realignment": {
                "current_xlsr_global": {
                    **_realign_metrics(global_rows),
                    "declined_songs": sorted(ids - {row["song_id"] for row in global_rows}),
                },
                "current_xlsr_hierarchical": {
                    **_realign_metrics(hierarchical_rows),
                    "declined_songs": sorted(ids - {row["song_id"] for row in hierarchical_rows}),
                },
            },
            "mss_alt": _mss_metrics(mss_rows),
        }
    write_json(output, report)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stems-manifest", type=Path, default=Path("eval/cache/full_stems/manifest.json"))
    parser.add_argument("--global-report", type=Path, default=Path("eval/runs/final_text_realign/report.json"))
    parser.add_argument("--hierarchical-report", type=Path, default=Path("eval/runs/final_text_realign_hierarchical_26/report.json"))
    parser.add_argument("--mss-report", type=Path, default=Path("eval/runs/mss_alt/large-v3-turbo/report.json"))
    parser.add_argument("--output", type=Path, default=Path("eval/runs/stem_cohort_comparison/report.json"))
    args = parser.parse_args()
    report = run(
        args.stems_manifest.resolve(), args.global_report.resolve(),
        args.hierarchical_report.resolve(), args.mss_report.resolve(), args.output.resolve(),
    )
    print(json.dumps(report["cohorts"], indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
