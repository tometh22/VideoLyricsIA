#!/usr/bin/env python3
"""Evaluate a LoRA-v1 inference replay on the canonical 23-song cohort."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from lora_v1 import (  # noqa: E402
    CANONICAL_COHORT_SIZE, data_improvement_curve, evaluate_predictions, read_jsonl,
)


def _canonical_ids(path: Path | None) -> set[str] | None:
    if path is None:
        return None
    payload = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(payload, list):
        return {str(item) for item in payload}
    if isinstance(payload, dict):
        cohort = payload.get("canonical_eval_cohort")
        if isinstance(cohort, dict):
            values = cohort.get("song_ids")
        else:
            values = payload.get("song_ids") or payload.get("songs") or payload.get("cases")
        if isinstance(values, list):
            ids = {str(item.get("song_id") if isinstance(item, dict) else item) for item in values}
            return {item for item in ids if item and item != "None"}
    raise ValueError("canonical cohort file must be a list or object with song_ids")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--canonical-cohort", type=Path, required=True)
    parser.add_argument(
        "--additional-exact-cohort", type=Path,
        help="optional song-id file for new raw_quality=exact songs (v2)",
    )
    parser.add_argument("--training-report", type=Path,
                        help="optional run_report.json to bind the adapter artifact")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    canonical = _canonical_ids(args.canonical_cohort)
    if canonical is None or len(canonical) != CANONICAL_COHORT_SIZE:
        raise ValueError(f"canonical cohort must contain exactly {CANONICAL_COHORT_SIZE} songs")
    additional_exact = _canonical_ids(args.additional_exact_cohort) or set()
    evaluation_song_ids = canonical | additional_exact
    baseline = read_jsonl(args.baseline.resolve())
    candidate = read_jsonl(args.candidate.resolve())
    evaluation = evaluate_predictions(
        baseline, candidate, canonical_song_ids=evaluation_song_ids,
    )
    evaluation["data_improvement_curve"] = data_improvement_curve(
        baseline, candidate, canonical_song_ids=evaluation_song_ids,
    )
    evaluation["evaluation_policy"] = {
        "cohort": (
            "canonical_exact_23_plus_new_exact"
            if additional_exact else "canonical_exact_23"
        ),
        "canonical_song_count": len(canonical),
        "additional_exact_song_count": len(additional_exact),
        "song_bootstrap_ci": True,
        "easy_difficult_split": True, "global_wer_only": False,
        "additional_family_only": True, "replacement_after_consecutive_evals": 2,
    }
    # A complete, held-out replay is the gate for *adding* the adapter as a
    # consensus witness.  It is deliberately not a superiority gate: the
    # base Whisper remains authoritative until two consecutive evaluations
    # prove sustained improvement.
    complete_replay = evaluation.get("songs") == len(evaluation_song_ids)
    evaluation["pipeline_validated"] = complete_replay
    evaluation["evaluation_passed"] = complete_replay
    evaluation["gate"] = {
        "passed": complete_replay,
        "reason": "complete_canonical_replay" if complete_replay else "incomplete_canonical_replay",
        "additional_family_only": True,
        "runtime_replacement_allowed": False,
    }
    if args.training_report:
        training_report = json.loads(args.training_report.resolve().read_text(encoding="utf-8"))
        if not isinstance(training_report, dict):
            raise ValueError("training report must be a JSON object")
        for key in ("base_model", "adapter_path", "adapter_sha256"):
            if training_report.get(key) is not None:
                evaluation[key] = training_report[key]
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(evaluation, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(evaluation, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
