#!/usr/bin/env python3
"""Run pipeline.transcribe() on one audio file and dump a benchmark bundle.

The SAME runner exercises whatever transcribe() is on the current checkout, so
you can A/B configs by toggling env vars (no code edits):

    VAD_CHUNK_ENABLED=0      # pure single-pass (no fallback)
    TRANSCRIBE_VAD_FIRST=1   # legacy VAD-first
    (default)                # single-pass first + VAD collapse-fallback

Output bundle ({source, segments, meta}) is consumed by score_benchmark.py as
baseline_output.json / improvement_output.json.

Usage (from lyricgen/backend, with the venv active and OPENAI_API_KEY set):
    python scripts/bench_runner.py <audio> <out.json> [source-label]
"""
import json
import os
import sys
import time

import pipeline

if __name__ == "__main__":
    audio, out_path = sys.argv[1], sys.argv[2]
    source = sys.argv[3] if len(sys.argv) > 3 else "?"

    t0 = time.time()
    segs = pipeline.transcribe(audio, language="es", job_id=None, return_words=False)
    elapsed = round(time.time() - t0, 1)

    bundle = {
        "source": source,
        "segments": segs,
        "meta": {
            "audio": os.path.basename(audio),
            "n_segments": len(segs),
            "elapsed_s": elapsed,
            "vad_chunk_enabled": os.environ.get("VAD_CHUNK_ENABLED", "1"),
            "vad_first": os.environ.get("TRANSCRIBE_VAD_FIRST", "0"),
        },
    }
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(bundle, f, ensure_ascii=False, indent=2)
    print(f"[{source}] {len(segs)} segments in {elapsed}s -> {out_path}")
