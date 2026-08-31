#!/usr/bin/env python3
"""Evaluate Phase-2 prerequisites and the repetition go/no-go without leakage."""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
from collections import Counter
from pathlib import Path
from typing import Any

from eval.bootstrap import song_bootstrap_ci
from eval.canonical import read_json, write_json
from eval.metrics import normalize_text
from eval.raw_cohort import RAW_TRUSTED


def _repeat_rows(golden: Path, word_errors: Path) -> list[dict[str, Any]]:
    errors = Counter()
    for row in csv.DictReader(word_errors.open(encoding="utf-8")):
        if row["repeat_context"] in {"repeated_line", "unique_line"}:
            errors[(row["song_id"], row["repeat_context"])] += 1
    manifest = read_json(golden / "manifest.json")
    result = []
    for item in manifest["cases"]:
        case = golden / item["path"]
        meta = read_json(case / "meta.json")
        if meta.get("raw_quality") not in RAW_TRUSTED:
            continue
        lines = read_json(case / "lines.json")
        counts = Counter(normalize_text(line.get("text") or "") for line in lines)
        denominators = Counter()
        for line in lines:
            if line.get("kind", "main") != "main":
                continue
            context = "repeated_line" if counts[normalize_text(line.get("text") or "")] > 1 else "unique_line"
            lexical = re.sub(r"\([^)]*\)", " ", str(line.get("text") or ""))
            denominators[context] += len(normalize_text(lexical).split())
        result.append({
            "song_id": item["song_id"],
            "repeated_errors": errors[(item["song_id"], "repeated_line")],
            "unique_errors": errors[(item["song_id"], "unique_line")],
            "repeated_words": denominators["repeated_line"],
            "unique_words": denominators["unique_line"],
        })
    return result


def _rate(sample: list[dict[str, Any]], context: str) -> float:
    return sum(row[f"{context}_errors"] for row in sample) / max(
        1, sum(row[f"{context}_words"] for row in sample)
    )


def run(
    golden: Path, word_errors: Path, stem_manifest: Path, output: Path,
    t7_report: Path | None = None,
) -> dict[str, Any]:
    rows = _repeat_rows(golden, word_errors)
    repeated = _rate(rows, "repeated")
    unique = _rate(rows, "unique")
    uplift = repeated / max(unique, 1e-9) - 1.0
    stems = read_json(stem_manifest)
    available_stems = sum(row.get("status") == "downloaded" for row in stems["cases"])
    substitutions = sum(
        row["type"] == "substitution"
        for row in csv.DictReader(word_errors.open(encoding="utf-8"))
    )
    t7 = read_json(t7_report) if t7_report is not None and t7_report.is_file() else None
    report = {
        "schema_version": 1,
        "gold_leakage": False,
        "repetition_prerequisite": {
            "repeated_error_rate": repeated,
            "repeated_song_bootstrap_ci": song_bootstrap_ci(rows, lambda sample: _rate(sample, "repeated")),
            "unique_error_rate": unique,
            "unique_song_bootstrap_ci": song_bootstrap_ci(rows, lambda sample: _rate(sample, "unique")),
            "relative_uplift": uplift,
            "material_uplift_threshold": 0.20,
            "decision": "IMPLEMENT_VOTING" if uplift >= 0.20 else "NOT_MATERIAL_SKIP",
        },
        "modules": [
            {
                "module": "2.1 phonetic_rescoring",
                "bucket": substitutions,
                "status": "BLOCKED_MISSING_PREHUMAN_NBEST_AND_IPA_POSTERIORS",
                "reason": "gold contains the winning approved token for scoring, but not the pre-human n-best or frame posteriors needed to generate and rank without leakage",
            },
            {
                "module": "2.2 repeated_section_voting",
                "bucket": sum(row["repeated_errors"] for row in rows),
                "status": "SKIPPED_BY_PREREQUISITE" if uplift < 0.20 else "READY_TO_IMPLEMENT",
                "reason": "per-word repeated-line rate is not materially above unique-line rate" if uplift < 0.20 else "material uplift confirmed",
            },
            {
                "module": "2.3 gemini_full_song_coherence",
                "bucket": sum(row["repeated_errors"] + row["unique_errors"] for row in rows),
                "status": "BLOCKED_CREDENTIAL_AND_CLIENT_AUDIO_EGRESS" if not (os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")) else "CREDENTIAL_PRESENT_POLICY_STILL_REQUIRED",
            },
            {
                "module": "2.4 whisper_n5_auto_consistency",
                "eligible_songs": 41,
                "runtime_stems_available": available_stems,
                "status": "READY" if available_stems == 41 else "WAITING_EXACT_RUNTIME_STEMS",
            },
            {
                "module": "T7 synthetic_corruption_verifier",
                "status": "BLOCKED_UMG_TRAINING_AUTHORIZATION",
                "prepared_samples": t7.get("samples") if t7 else 0,
                "prepared_songs": t7.get("songs") if t7 else 0,
                "reason": "preparation is local, but client-data training remains contrary to the published policy until authorized",
            },
        ],
        "gate": {"minimum_recall": 0.20, "minimum_precision": 0.70, "unit": "song bootstrap"},
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    write_json(output, report)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--golden", type=Path, default=Path("eval/golden"))
    parser.add_argument("--word-errors", type=Path, default=Path("eval/runs/prod_raw/autopsy/word_errors.csv"))
    parser.add_argument("--stem-manifest", type=Path, default=Path("eval/cache/full_stems/manifest.json"))
    parser.add_argument("--output", type=Path, default=Path("eval/runs/phase2_status/report.json"))
    parser.add_argument("--t7-report", type=Path, default=Path("eval/runs/t7_corruptions/report.json"))
    args = parser.parse_args()
    print(json.dumps(run(
        args.golden.resolve(), args.word_errors.resolve(), args.stem_manifest.resolve(),
        args.output.resolve(), args.t7_report.resolve(),
    ), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
