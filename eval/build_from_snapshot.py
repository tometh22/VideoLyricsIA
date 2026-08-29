#!/usr/bin/env python3
"""Build the canonical layout from the already audited 65-song snapshot."""

from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path

from eval.canonical import (
    RAW_QUALITIES, RAW_QUALITY_MAP, canonical_sha256, derive_edits, read_json,
    segments_to_lines, segments_to_words, write_json,
)


def build(snapshot: Path, output: Path) -> dict:
    if output.exists() and any(output.iterdir()):
        raise RuntimeError(f"output must be empty: {output}")
    manifest = read_json(snapshot / "manifest.json")
    output.mkdir(parents=True, exist_ok=True)
    cases = []
    counts = {quality: 0 for quality in sorted(RAW_QUALITIES)}
    origins = {"staging": 0, "production": 0}
    for item in manifest["cases"]:
        job_id = item["job_id"]
        source = snapshot / "cases" / job_id
        metadata = read_json(source / "metadata.json")
        approved = read_json(source / "human_gold.json")
        raw_path = source / "machine_original.json"
        raw = read_json(raw_path) if raw_path.is_file() else None
        legacy_quality = metadata["historical_machine_baseline"]["quality"]
        raw_quality = RAW_QUALITY_MAP[legacy_quality]
        origin = str(metadata["source_environment"])
        counts[raw_quality] += 1
        origins[origin] += 1
        case = output / job_id
        case.mkdir()
        write_json(case / "approved.json", approved)
        lines = segments_to_lines(approved)
        write_json(case / "lines.json", lines)
        words = segments_to_words(approved)
        if words:
            write_json(case / "words.json", words)
        if raw is not None:
            write_json(case / "raw_pipeline_output.json", {
                "schema_version": 1,
                "job_id": job_id,
                "historical": True,
                "raw_quality": raw_quality,
                "pipeline": metadata.get("pipeline") or {},
                "segments": raw,
            })
            write_json(case / "edits.json", derive_edits(raw, approved))
        else:
            write_json(case / "edits.json", [])
        song = metadata.get("song") or {}
        meta = {
            "schema_version": 1,
            "song_id": job_id,
            "artist": song.get("artist"),
            "title": song.get("title"),
            "isrc": None,
            "language": {"value": None, "derived": True, "status": "pending_audio_export"},
            "duration_s": None,
            "duration_derived": True,
            "approved_at": (metadata.get("delivery") or {}).get("approved_at"),
            "approved_by": (metadata.get("delivery") or {}).get("approved_by_label"),
            "source_url": f"https://umg.genly.pro/",
            "job_origin": origin,
            "raw_quality": raw_quality,
            "has_raw": raw is not None,
            "raw_historical": raw is not None,
            "audio": {
                "available_in_r2": bool((metadata.get("audio") or {}).get("available")),
                "expected_sha256": (metadata.get("audio") or {}).get("stored_sha256"),
                "downloaded": False,
                "verified": False,
            },
            "approved_sha256": canonical_sha256(approved),
            "provenance": {
                "snapshot": str(snapshot),
                "historical_baseline": metadata.get("historical_machine_baseline"),
            },
        }
        write_json(case / "meta.json", meta)
        cases.append({
            "song_id": job_id,
            "path": job_id,
            "raw_quality": raw_quality,
            "has_raw": raw is not None,
            "job_origin": origin,
        })
    result = {
        "schema_version": 1,
        "songs": len(cases),
        "raw_quality_counts": counts,
        "job_origin_counts": origins,
        "historical_raw_exact_plus_reconstructed": counts["exact"] + counts["reconstructed"],
        "historical_raw_all": len([case for case in cases if case["has_raw"]]),
        "cases": cases,
    }
    write_json(output / "manifest.json", result)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=Path("eval/golden"))
    args = parser.parse_args()
    try:
        report = build(args.snapshot.resolve(), args.output.resolve())
    except (OSError, ValueError, KeyError, json.JSONDecodeError, RuntimeError) as exc:
        parser.error(str(exc))
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
