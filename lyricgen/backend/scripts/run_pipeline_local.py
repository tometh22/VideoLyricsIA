#!/usr/bin/env python3
"""Run pipeline_runner.transcribe_local() against every job in the dataset.

Writes one of:
    benchmark/dataset/<job_id>/baseline_output.json
    benchmark/dataset/<job_id>/improvement_output.json

…depending on whether Tier 1 env flags are set when this runs:

  unset                 → writes baseline_output.json
  any tier-1 flag set   → writes improvement_output.json

Run both modes to populate both files, then compare with
`score_benchmark.py`.

Usage:
    cd lyricgen/backend
    source venv/bin/activate
    export OPENAI_API_KEY=...
    export GOOGLE_APPLICATION_CREDENTIALS=/path/to/vertex.json

    # Baseline (existing pipeline, no tier-1 changes)
    python scripts/run_pipeline_local.py

    # Then enable Tier 1 and run again
    export ENABLE_TIER1=1
    python scripts/run_pipeline_local.py
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
BACKEND = HERE.parent
sys.path.insert(0, str(BACKEND))
sys.path.insert(0, str(HERE))

from pipeline_runner import transcribe_local, _env_truthy  # noqa: E402

DATASET = HERE.parent / "benchmark" / "dataset"


def _output_filename() -> str:
    """Pick the output filename based on env flags.

    Any single tier-1 flag set ⇒ improvement run. This intentionally
    lets the operator run with just one flag at a time if they want
    to attribute a delta to a single helper.
    """
    if _env_truthy("ENABLE_TIER1") or _env_truthy("VALIDATE_SEGMENTS") or _env_truthy("POLISH_TEXT"):
        return "improvement_output.json"
    return "baseline_output.json"


def main() -> None:
    if not DATASET.exists():
        print(f"[ERR] dataset dir not found: {DATASET}", file=sys.stderr)
        print(f"      Run build_benchmark_dataset.py first.", file=sys.stderr)
        sys.exit(2)

    dirs = sorted(p for p in DATASET.iterdir() if p.is_dir())
    if not dirs:
        print(f"[ERR] no job dirs under {DATASET}", file=sys.stderr)
        sys.exit(2)

    out_name = _output_filename()
    mode_label = "TIER 1 (with improvements)" if out_name == "improvement_output.json" else "BASELINE"
    print(f"Running pipeline in {mode_label} mode against {len(dirs)} job(s)")
    print(f"Output filename: {out_name}")
    print()

    overall_t0 = time.time()
    ok = 0
    failed = 0
    for d in dirs:
        meta_path = d / "metadata.json"
        if not meta_path.exists():
            print(f"  ⚠ {d.name}: no metadata.json, skip")
            failed += 1
            continue
        meta = json.loads(meta_path.read_text())
        audio_candidates = sorted(d.glob("audio.*"))
        audio_candidates = [p for p in audio_candidates if not p.name.endswith(".json")]
        if not audio_candidates:
            print(f"  ⚠ {d.name}: no audio.* file, skip")
            failed += 1
            continue
        audio_path = str(audio_candidates[0])

        print(f"[{d.name}] {meta.get('artist','')} - {meta.get('song_title','')}")
        try:
            result = transcribe_local(
                audio_path=audio_path,
                artist=meta.get("artist", ""),
                song_title=meta.get("song_title", ""),
                language="es",  # benchmark dataset is Spanish
                verbose=True,
            )
        except Exception as e:
            print(f"  ✗ failed: {e}")
            failed += 1
            continue

        (d / out_name).write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        ok += 1
        print()

    elapsed = time.time() - overall_t0
    print(f"\nDone: {ok} ok, {failed} failed, {elapsed:.1f}s total")
    if ok > 0:
        print(f"Score with: python scripts/score_benchmark.py")


if __name__ == "__main__":
    main()
