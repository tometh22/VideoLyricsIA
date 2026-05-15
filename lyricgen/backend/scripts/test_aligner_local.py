#!/usr/bin/env python3
"""Test align_lrclib_to_whisper() against the calibration corpus.

For each WAV:
  1. Pull LRCLib synced + plain from the API
  2. Run Whisper turbo with word_timestamps=True
  3. Pretend we only had `plain`: feed plain + Whisper to the aligner
  4. Compare the aligner's output to the synced LRCLib (ground truth)
     line-by-line

The aligner keeps LRCLib's line structure so each output segment has a
1:1 correspondence to a synced LRC line — we can compute per-line error
directly without fuzzy text alignment. This is the cleanest measurement
of timing accuracy we have.

Output: prints a per-song table and a global aggregate.
"""

import json
import os
import sys
import time
from pathlib import Path
from statistics import median, mean, stdev

HERE = Path(__file__).resolve().parent
BACKEND = HERE.parent
sys.path.insert(0, str(BACKEND))
sys.path.insert(0, str(HERE))

os.environ.setdefault("CORS_ORIGINS", "http://localhost:5173")
os.environ.pop("OPENAI_API_KEY", None)


def _load_corpus(path: Path) -> list[dict]:
    with open(path) as f:
        return json.load(f)


def _normalize_text(t: str) -> str:
    import re
    t = (t or "").lower().strip()
    t = re.sub(r"[^\w\sáéíóúñü]", "", t)
    return " ".join(t.split())


def _run_whisper_with_words(audio_path: str) -> dict:
    """Run Whisper turbo and return the raw result with word_timestamps."""
    from pipeline import _get_whisper_model
    model = _get_whisper_model("turbo")
    return model.transcribe(
        audio_path,
        word_timestamps=True,
        condition_on_previous_text=False,
        no_speech_threshold=0.6,
        compression_ratio_threshold=2.4,
    )


def _segments_from_whisper_result(result: dict) -> list[dict]:
    """Mirror pipeline.transcribe() shape: keep words array so the
    aligner has them to work with."""
    out = []
    for seg in result.get("segments", []):
        text = (seg.get("text") or "").strip()
        if not text or len(text) < 3:
            continue
        if seg.get("no_speech_prob", 0) > 0.7:
            continue
        out.append({
            "start": float(seg["start"]),
            "end": float(seg["end"]),
            "text": text,
            "words": seg.get("words") or [],
        })
    return out


def _resolve_lrclib(artist: str, title: str):
    """Return (synced, plain) raw strings from LRCLib, or (None, None).

    _fetch_lrclib's documented return shape is {plain, synced, duration}.
    """
    from pipeline import _fetch_lrclib
    rec = _fetch_lrclib(artist, title, db=None)
    if not rec:
        return None, None
    return rec.get("synced"), rec.get("plain")


def _lrc_to_segments(lrc: str) -> list[dict]:
    from pipeline import _lrc_to_segments as fn
    return fn(lrc) or []


def _compare_line_by_line(aligned: list[dict], ground_truth: list[dict]) -> list[float]:
    """Match aligned[i] to the LRCLib synced line with the same normalized
    text. Returns list of (aligned.start - ground_truth.start) in seconds.

    Greedy match by text, then by closest start time among same-text
    candidates. Skips aligned segments that don't have a clear GT match.
    """
    used = set()
    errors = []
    for a in aligned:
        a_norm = _normalize_text(a["text"])
        candidates = []
        for gi, g in enumerate(ground_truth):
            if gi in used:
                continue
            g_norm = _normalize_text(g["text"])
            if g_norm == a_norm:
                candidates.append((gi, g))
            # also allow if one contains the other (partial overlap)
            elif a_norm and g_norm and (
                a_norm in g_norm or g_norm in a_norm
            ):
                candidates.append((gi, g))
        if not candidates:
            continue
        # Pick the candidate closest in time
        candidates.sort(key=lambda c: abs(c[1]["start"] - a["start"]))
        gi, g = candidates[0]
        used.add(gi)
        errors.append(a["start"] - g["start"])
    return errors


def _stats(errors: list[float]) -> dict:
    if not errors:
        return {"n": 0}
    abs_ms = [abs(e) * 1000 for e in errors]
    signed_ms = [e * 1000 for e in errors]
    return {
        "n": len(errors),
        "mean_abs_ms": mean(abs_ms),
        "median_abs_ms": median(abs_ms),
        "p95_abs_ms": sorted(abs_ms)[int(len(abs_ms) * 0.95) - 1] if len(abs_ms) > 1 else abs_ms[0],
        "std_abs_ms": stdev(abs_ms) if len(abs_ms) > 1 else 0.0,
        "raw_mean_ms": mean(signed_ms),
        "raw_median_ms": median(signed_ms),
    }


def main():
    import argparse
    from lrclib_aligner import align_lrclib_to_whisper

    p = argparse.ArgumentParser()
    p.add_argument("--limit", type=int, default=0,
                   help="Process at most N songs (0=all). Use for smoke test.")
    args = p.parse_args()

    corpus_path = Path.home() / ".calibration" / "corpus.json"
    corpus = _load_corpus(corpus_path)
    if args.limit > 0:
        corpus = corpus[:args.limit]
    print(f"Corpus: {len(corpus)} songs\n")

    all_errors = []
    per_song = []

    for entry in corpus:
        artist, title, wav = entry["artist"], entry["title"], entry["wav_path"]
        if not os.path.exists(wav):
            print(f"SKIP {artist} — {title}: audio not found")
            continue
        print(f"=== {artist} — {title} ===")
        t0 = time.monotonic()
        synced, plain = _resolve_lrclib(artist, title)
        if not synced or not plain:
            print(f"  no LRCLib synced or plain — skip")
            continue
        gt = _lrc_to_segments(synced)
        print(f"  LRCLib synced: {len(gt)} segs")
        print(f"  LRCLib plain: {len(plain.splitlines())} raw lines")

        print(f"  running Whisper... ", end="", flush=True)
        t1 = time.monotonic()
        result = _run_whisper_with_words(wav)
        whisper_segs = _segments_from_whisper_result(result)
        print(f"{len(whisper_segs)} segs ({time.monotonic()-t1:.0f}s)")

        # Run the aligner: LRCLib plain structure + Whisper word timings
        aligned = align_lrclib_to_whisper(plain, whisper_segs)
        print(f"  aligner produced: {len(aligned)} segs")

        # Compare aligned[i] to ground_truth[same text] for timing error
        errors = _compare_line_by_line(aligned, gt)
        s = _stats(errors)
        per_song.append({"artist": artist, "title": title, "stats": s, "raw_errors": errors})
        all_errors.extend(errors)

        if s["n"] > 0:
            print(f"  matched {s['n']}/{len(aligned)} aligner lines to GT")
            print(f"  abs error: mean={s['mean_abs_ms']:.0f}ms  "
                  f"median={s['median_abs_ms']:.0f}ms  p95={s['p95_abs_ms']:.0f}ms")
            print(f"  signed bias: mean={s['raw_mean_ms']:+.0f}ms  median={s['raw_median_ms']:+.0f}ms")
        print(f"  total: {time.monotonic()-t0:.0f}s\n")

    print("=" * 60)
    print("AGGREGATE (all matched lines across all songs)")
    print("=" * 60)
    s = _stats(all_errors)
    if s["n"] == 0:
        print("no matches found")
        return
    print(f"  n: {s['n']}")
    print(f"  mean abs:    {s['mean_abs_ms']:>6.0f} ms")
    print(f"  median abs:  {s['median_abs_ms']:>6.0f} ms")
    print(f"  p95 abs:     {s['p95_abs_ms']:>6.0f} ms")
    print(f"  std abs:     {s['std_abs_ms']:>6.0f} ms")
    print(f"  raw mean:    {s['raw_mean_ms']:>+6.0f} ms  (positive = aligner late)")
    print(f"  raw median:  {s['raw_median_ms']:>+6.0f} ms")
    print()
    print("Gate: median_abs < 50ms AND p95_abs < 200ms in ≥5/7 songs?")
    passing = 0
    for r in per_song:
        s = r["stats"]
        if s["n"] == 0:
            continue
        if s["median_abs_ms"] < 50 and s["p95_abs_ms"] < 200:
            passing += 1
            print(f"  ✓ {r['artist']} — {r['title']}  (median={s['median_abs_ms']:.0f}ms p95={s['p95_abs_ms']:.0f}ms)")
        else:
            print(f"  ✗ {r['artist']} — {r['title']}  (median={s['median_abs_ms']:.0f}ms p95={s['p95_abs_ms']:.0f}ms)")
    print(f"\n{passing}/{len(per_song)} songs pass the gate.")


if __name__ == "__main__":
    main()
