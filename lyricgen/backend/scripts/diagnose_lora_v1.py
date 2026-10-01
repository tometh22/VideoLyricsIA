#!/usr/bin/env python3
"""Run a non-gating LoRA replay report for reconstructed/estimated songs.

The canonical evaluator intentionally accepts exactly 23 held-out songs.  A
diagnostic cohort must not be able to accidentally look like a promotion gate,
so this command derives its cohort from the private manifest and labels the
result explicitly as non-gating.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from lora_v1 import evaluate_predictions, read_jsonl, song_bootstrap  # noqa: E402


def _manifest_rows(path: Path) -> list[dict[str, Any]]:
    rows = read_jsonl(path.resolve())
    if not rows:
        raise ValueError("diagnostic manifest is empty")
    return rows


def _quality_ids(rows: list[dict[str, Any]], quality: str) -> set[str]:
    values = {str(row.get("song_id") or "") for row in rows
              if str(row.get("raw_quality") or "") == quality}
    values.discard("")
    if not values:
        raise ValueError(f"manifest has no songs with raw_quality={quality!r}")
    return values


def run(
    baseline_path: Path,
    candidate_path: Path,
    manifest_path: Path,
    output_path: Path,
    *,
    raw_quality: str = "reconstructed",
) -> dict[str, Any]:
    manifest = _manifest_rows(manifest_path)
    song_ids = _quality_ids(manifest, raw_quality)
    baseline = read_jsonl(baseline_path.resolve())
    candidate = read_jsonl(candidate_path.resolve())
    evaluation = evaluate_predictions(
        baseline, candidate, canonical_song_ids=song_ids,
    )
    meta = {}
    for row in manifest:
        song_id = str(row.get("song_id") or "")
        if song_id in song_ids and song_id not in meta:
            meta[song_id] = {
                "artist": str(row.get("artist") or "unknown"),
                "raw_quality": str(row.get("raw_quality") or "unknown"),
                "job_origin": str(row.get("job_origin") or "unknown"),
            }
    # Add the raw-quality partition explicitly: reconstructed rows are marked
    # easy by the training manifest's difficulty policy, so "easy/difficult"
    # would be misleading for this diagnostic.
    deltas: dict[str, float] = {}
    for song_id, item in evaluation["by_song"].items():
        item.update(meta.get(song_id, {}))
        deltas[song_id] = float(item["baseline_wer"] - item["candidate_wer"])
    overall = evaluation["partitions"]["overall"]
    evaluation["raw_quality_partition"] = {
        "raw_quality": raw_quality,
        "songs": len(song_ids),
        "baseline": overall["baseline"],
        "candidate": overall["candidate"],
        "relative_improvement": overall["relative_improvement"],
        "song_delta_bootstrap": song_bootstrap(deltas),
    }
    evaluation["evaluation_policy"] = {
        "mode": "diagnostic_non_gate",
        "raw_quality": raw_quality,
        "songs_from_manifest": sorted(song_ids),
        "canonical_promotion_gate": False,
        "leave_one_song_out_for_promotion": True,
    }
    evaluation["gate"] = {
        "passed": False,
        "status": "diagnostic_only",
        "reason": "reconstructed_or_noncanonical_cohort_not_eligible_for_promotion",
        "additional_family_only": True,
        "runtime_replacement_allowed": False,
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(evaluation, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return evaluation


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--raw-quality", default="reconstructed")
    args = parser.parse_args()
    report = run(args.baseline, args.candidate, args.manifest, args.output,
                 raw_quality=args.raw_quality)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
