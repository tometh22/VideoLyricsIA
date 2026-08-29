#!/usr/bin/env python3
"""Replay the exact deployed timing selector against pre-human gold.

The runtime module is loaded from a pinned git object.  This prevents the eval
branch from silently testing a reimplementation that differs from staging.
Only vocal stems produced by the deployed ``mdx_extra`` path are accepted.
"""

from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import re
import subprocess
import sys
import types
from pathlib import Path
from typing import Any

from eval.bootstrap import song_bootstrap_ci
from eval.canonical import read_json, write_json


RUNTIME_PATH = "lyricgen/backend/timing_review_suggestions.py"


def _runtime_module(ref: str) -> tuple[types.ModuleType, str, str]:
    source = subprocess.check_output(
        ["git", "show", f"{ref}:{RUNTIME_PATH}"], text=True,
    )
    commit = subprocess.check_output(
        ["git", "rev-parse", ref], text=True,
    ).strip()
    source_sha256 = hashlib.sha256(source.encode("utf-8")).hexdigest()
    name = "_pinned_timing_review_suggestions"
    module = types.ModuleType(name)
    module.__file__ = f"git:{commit}:{RUNTIME_PATH}"
    sys.modules[name] = module
    exec(compile(source, module.__file__, "exec"), module.__dict__)
    return module, commit, source_sha256


def _normalized(text: Any) -> str:
    return " ".join(re.findall(r"\w+", str(text or "").casefold()))


def _identity(left: Any, right: Any) -> float:
    return difflib.SequenceMatcher(None, _normalized(left), _normalized(right)).ratio()


def _metric(sample: list[dict[str, Any]], numerator: str, denominator: str) -> float:
    return sum(int(row[numerator]) for row in sample) / max(
        1, sum(int(row[denominator]) for row in sample)
    )


def run(
    golden: Path, stems: Path, output: Path, runtime_ref: str,
    tolerance_s: float = 0.15,
) -> dict[str, Any]:
    runtime, commit, source_sha256 = _runtime_module(runtime_ref)
    manifest = read_json(golden / "manifest.json")
    stem_manifest_path = stems / "manifest.json"
    stem_manifest = read_json(stem_manifest_path)
    stem_records = {row["song_id"]: row for row in stem_manifest["cases"]}
    rows: list[dict[str, Any]] = []
    missing = []
    abstention_reasons: dict[str, int] = {}
    proposal_rows = []
    for item in manifest["cases"]:
        case = golden / item["path"]
        meta = read_json(case / "meta.json")
        if meta.get("raw_quality") not in {"exact", "reconstructed"}:
            continue
        song_id = item["song_id"]
        stem_record = stem_records.get(song_id) or {}
        stem = stems / song_id / "vocals.wav"
        if stem_record.get("status") != "downloaded" or not stem.is_file():
            missing.append(song_id)
            continue
        raw = read_json(case / "raw_pipeline_output.json")["segments"]
        approved = read_json(case / "approved.json")
        track = runtime.load_acoustic_track(stem)
        candidates, diagnostics = runtime.build_timing_review_candidates(raw, track)
        for reason, count in diagnostics.get("abstention_reasons", {}).items():
            abstention_reasons[reason] = abstention_reasons.get(reason, 0) + int(count)
        by_index = {}
        for candidate in candidates:
            match = re.match(r"t4-(\d+)-", str(candidate.get("id") or ""))
            if match:
                by_index[int(match.group(1))] = candidate
        song_row = {
            "song_id": song_id,
            "corrections": 0,
            "corrections_recalled": 0,
            "proposals": len(candidates),
            "proposals_correct": 0,
            "identity_matched_lines": 0,
        }
        for index, raw_line in enumerate(raw):
            if index >= len(approved) or _identity(raw_line.get("text"), approved[index].get("text")) < 0.85:
                continue
            song_row["identity_matched_lines"] += 1
            raw_end = float(raw_line.get("end") or 0.0)
            approved_end = float(approved[index].get("end") or 0.0)
            corrected = abs(approved_end - raw_end) > tolerance_s
            candidate = by_index.get(index)
            if corrected:
                song_row["corrections"] += 1
            if candidate is not None:
                proposed_end = float(candidate["proposed_end"])
                correct = corrected and abs(proposed_end - approved_end) <= tolerance_s
                song_row["proposals_correct"] += int(correct)
                song_row["corrections_recalled"] += int(correct)
                proposal_rows.append({
                    "song_id": song_id,
                    "line_idx": index,
                    "current_end": raw_end,
                    "approved_end": approved_end,
                    "proposed_end": proposed_end,
                    "correct": correct,
                    "diagnosis": ";".join(candidate.get("reasons") or []),
                    "confidence": candidate.get("confidence"),
                    "impact_ms": candidate.get("impact_ms"),
                })
        rows.append(song_row)

    recall = _metric(rows, "corrections_recalled", "corrections")
    precision = _metric(rows, "proposals_correct", "proposals")
    recall_ci = song_bootstrap_ci(
        rows, lambda sample: _metric(sample, "corrections_recalled", "corrections"),
    )
    precision_ci = song_bootstrap_ci(
        rows, lambda sample: _metric(sample, "proposals_correct", "proposals"),
    )
    report = {
        "schema_version": 1,
        "mode": "exact_deployed_timing_selector_replay",
        "runtime": {
            "git_commit": commit,
            "source_path": RUNTIME_PATH,
            "source_sha256": source_sha256,
            "stem_model": "mdx_extra",
        },
        "gold_leakage": False,
        "gold_usage": "scoring only after proposals are frozen",
        "eligible_songs": sum(
            (read_json(golden / item["path"] / "meta.json").get("raw_quality") in {"exact", "reconstructed"})
            for item in manifest["cases"]
        ),
        "replayed_songs": len(rows),
        "missing_runtime_stems": len(missing),
        "missing_song_ids": missing,
        "result_scope": "exploratory_incomplete" if len(rows) < 10 else "historical_replay",
        "timing": {
            "corrections": sum(row["corrections"] for row in rows),
            "proposals": sum(row["proposals"] for row in rows),
            "correct_proposals": sum(row["proposals_correct"] for row in rows),
            "recall": recall,
            "recall_song_bootstrap_ci": recall_ci,
            "precision": precision,
            "precision_song_bootstrap_ci": precision_ci,
            "gate": {
                "minimum_recall": 0.20,
                "minimum_precision": 0.70,
                "status": "INSUFFICIENT_SONGS" if len(rows) < 10 else (
                    "GO_CANDIDATE" if recall >= 0.20 and precision >= 0.70 else "NO_GO"
                ),
            },
        },
        "text_and_vocalization": {
            "status": "NOT_REPLAYABLE_FROM_PRESERVED_ARTIFACTS",
            "reason": "historical corpus does not preserve the independent candidate-family inputs required by the deployed consensus selector",
            "substitution_forbidden": "approved lyrics are not supplied to candidate generation",
        },
        "abstention_reasons": abstention_reasons,
        "per_song": rows,
    }
    output.mkdir(parents=True, exist_ok=True)
    write_json(output / "report.json", report)
    write_json(output / "proposals.json", proposal_rows)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--golden", type=Path, default=Path("eval/golden"))
    parser.add_argument("--stems", type=Path, default=Path("eval/cache/full_stems"))
    parser.add_argument("--output", type=Path, default=Path("eval/runs/runtime_suggestions_replay"))
    parser.add_argument("--runtime-ref", default="c9bdc358")
    args = parser.parse_args()
    report = run(args.golden.resolve(), args.stems.resolve(), args.output.resolve(), args.runtime_ref)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
