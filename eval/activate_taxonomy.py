#!/usr/bin/env python3
"""Evaluate the human taxonomy certificate without publishing or mutating it."""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path

from eval.canonical import write_json
from eval.classify_errors import CATEGORIES


TRUE_VALUES = {"1", "true", "yes", "si", "sí", "y"}
FALSE_VALUES = {"0", "false", "no", "n"}


def run(validation_csv: Path, output: Path, required: int = 30) -> dict:
    rows = list(csv.DictReader(validation_csv.open(encoding="utf-8")))
    judged = []
    invalid = []
    for row in rows:
        human_category = str(row.get("human_category") or "").strip()
        explicit = str(row.get("human_agrees") or "").strip().casefold()
        if human_category:
            if human_category not in CATEGORIES:
                invalid.append(row.get("custom_id"))
                continue
            agrees = human_category == row.get("llm_category")
            judged.append(agrees)
        elif explicit in TRUE_VALUES | FALSE_VALUES:
            judged.append(explicit in TRUE_VALUES)
    agreement = sum(judged) / max(1, len(judged))
    ready = len(judged) >= required and not invalid
    report = {
        "schema_version": 1,
        "rows": len(rows),
        "judged": len(judged),
        "required": required,
        "invalid_human_categories": invalid,
        "agreement": agreement if judged else None,
        "gate": {
            "required_agreement": 0.85,
            "status": (
                "INCOMPLETE" if not ready else
                "APPROVED" if agreement >= 0.85 else
                "REVISE_PROMPT_AND_NEW_SAMPLE"
            ),
        },
        "side_effects": False,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    write_json(output, report)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--validation-csv", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=Path("eval/runs/taxonomy_activation/report.json"))
    args = parser.parse_args()
    print(json.dumps(run(args.validation_csv, args.output), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
