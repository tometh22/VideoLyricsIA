#!/usr/bin/env python3
"""Freeze a content-addressed, portal-verified historical baseline."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

from eval.canonical import canonical_sha256, read_json, write_json
from eval.metrics import NORMALIZATION_VERSION


def freeze(
    golden: Path, score_path: Path, autopsy_41_path: Path,
    autopsy_57_path: Path, language_path: Path, output: Path,
) -> dict[str, Any]:
    manifest = read_json(golden / "manifest.json")
    if manifest.get("status") != "complete":
        raise RuntimeError("golden extraction is not complete")
    portal = manifest.get("portal_verification_sample") or []
    if len(portal) != 5 or not all(row.get("verified") for row in portal):
        raise RuntimeError("five-case live portal gate has not passed")
    language = read_json(language_path)
    if language.get("songs") != manifest.get("songs"):
        raise RuntimeError("language identification does not cover the full golden set")
    if language.get("unresolved_count"):
        raise RuntimeError("language identification still has unresolved songs")
    score = read_json(score_path)
    if score.get("normalization_version") != NORMALIZATION_VERSION:
        raise RuntimeError("score normalization does not match the harness")
    autopsy_41 = read_json(autopsy_41_path)
    autopsy_57 = read_json(autopsy_57_path)
    if autopsy_41.get("songs") != 41 or autopsy_57.get("songs") != 57:
        raise RuntimeError("autopsy cohorts are not 41 and 57 songs")
    audio_identity = []
    approved_identity = []
    for item in manifest["cases"]:
        meta = read_json(golden / item["path"] / "meta.json")
        audio_identity.append({"song_id": item["song_id"], "sha256": meta["audio"]["sha256"]})
        approved_identity.append({"song_id": item["song_id"], "sha256": meta["approved_sha256"]})
    baseline = {
        "schema_version": 1,
        "baseline_id": "baseline-2026-08-29",
        "certified": True,
        "normalization_version": NORMALIZATION_VERSION,
        "identity": {
            "manifest_sha256": canonical_sha256(manifest),
            "audio_set_sha256": canonical_sha256(sorted(audio_identity, key=lambda row: row["song_id"])),
            "approved_set_sha256": canonical_sha256(sorted(approved_identity, key=lambda row: row["song_id"])),
        },
        "extraction": {
            "songs": manifest["songs"],
            "raw_quality_counts": manifest["raw_quality_counts"],
            "job_origin_counts": manifest["job_origin_counts"],
            "legacy_raw_text_retention": manifest["legacy_raw_text_retention"],
            "portal_verification": portal,
            "language_id": {
                "songs": language["songs"],
                "agreement_count": language["agreement_count"],
                "disagreement_count": language["disagreement_count"],
                "unresolved_count": language["unresolved_count"],
                "whisper_model": language["whisper_model"],
                "llm_model": language["llm_model"],
            },
        },
        "cohorts": {
            "exact_plus_reconstructed_41": {
                "score": score["cohorts"]["raw_exact_plus_reconstructed"],
                "autopsy": autopsy_41,
            },
            "including_estimated_57": {
                "score": score["cohorts"]["raw_all_57"],
                "autopsy": autopsy_57,
            },
        },
        "exclusions": {
            "no_raw_count": manifest["raw_quality_counts"].get("none", 0),
            "comparative_metrics": "excluded",
        },
    }
    write_json(output, baseline)
    return baseline


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--golden", type=Path, default=Path("eval/golden"))
    parser.add_argument("--score", type=Path, default=Path("eval/runs/prod_raw/summary.json"))
    parser.add_argument("--autopsy-41", type=Path, required=True)
    parser.add_argument("--autopsy-57", type=Path, required=True)
    parser.add_argument("--language", type=Path, default=Path("eval/reports/language_id.json"))
    parser.add_argument("--output", type=Path, default=Path("eval/baselines/baseline-2026-08-29.json"))
    args = parser.parse_args()
    result = freeze(
        args.golden.resolve(), args.score.resolve(), args.autopsy_41.resolve(),
        args.autopsy_57.resolve(), args.language.resolve(), args.output.resolve(),
    )
    print(f"frozen {result['baseline_id']} ({result['identity']['manifest_sha256'][:12]})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
