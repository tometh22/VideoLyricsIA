#!/usr/bin/env python3
"""Experiment: given the CORRECT lyric text (Rotor's ground-truth text), how
tight can our forced-aligner place line onsets vs Rotor's timing?

This isolates ALIGNMENT quality from TRANSCRIPTION quality. If aligning known-
correct text hits Rotor-grade (median AOO < 0.2s), then the whole problem
reduces to "get the text right" (Fase 1 ASR). If not, the aligner needs work
(Fase 2). Writes improvement_output.json (source=forced_align_known) so
score_benchmark.py picks it up.

Usage:
    cd lyricgen/backend
    export FORCED_ALIGNER_ENABLED=1   # + REPLICATE_API_TOKEN
    python scripts/exp_align_known_text.py --only rotor_donde_estan
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
BACKEND = HERE.parent
sys.path.insert(0, str(BACKEND))
DATASET = BACKEND / "benchmark" / "dataset"

import forced_align  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default="", help="only this dataset slug")
    args = ap.parse_args()

    dirs = sorted(p for p in DATASET.iterdir() if p.is_dir())
    if args.only:
        dirs = [d for d in dirs if d.name == args.only]

    for d in dirs:
        gt = json.loads((d / "ground_truth.json").read_text())
        # Use Rotor's text as the known-correct transcript (one line each).
        text = "\n".join((s.get("text") or "").strip() for s in gt if (s.get("text") or "").strip())
        audio = next((p for p in d.glob("audio.*") if not p.name.endswith(".json")), None)
        if not audio:
            print(f"  {d.name}: no audio, skip"); continue
        print(f"[{d.name}] forced-aligning {len(gt)} known lines to {audio.name} ...", flush=True)
        t0 = time.time()
        segs = forced_align.forced_align_lyrics(str(audio), text)
        dt = time.time() - t0
        if not segs:
            print(f"  ✗ forced_align returned None/empty after {dt:.0f}s (likely cureau crash on repetitive stem)")
            continue
        out = {"segments": segs, "source": "forced_align_known", "meta": {"elapsed_s": dt}}
        (d / "improvement_output.json").write_text(json.dumps(out, ensure_ascii=False, indent=2, default=str))
        print(f"  ✓ {len(segs)} segs in {dt:.0f}s → improvement_output.json")


if __name__ == "__main__":
    main()
