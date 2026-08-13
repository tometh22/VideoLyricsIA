#!/usr/bin/env python3
"""Calibrate Whisper timestamps against LRCLib ground truth.

Usage (from anywhere; script self-paths the imports):
    python lyricgen/backend/scripts/calibrate_whisper.py \
        --corpus ~/.calibration/corpus.json \
        --report ~/.calibration/report.md \
        --strategies S0,S1,S2

For each (song × strategy) the script:
  1. Loads the audio
  2. Resolves ground-truth segments from LRCLib (synced)
  3. Runs Whisper local-turbo to produce candidate segments
  4. Applies the strategy to shift/snap timestamps
  5. Aligns candidate to ground truth by fuzzy text match
  6. Computes per-line errors: candidate.start - groundtruth.start

Aggregates: mean / median / p50 / p95 of |error| per strategy, both
per-song and overall. Output: Markdown report + CSV with raw errors.

NO network calls except LRCLib /api/get (https://lrclib.net). NO R2,
NO production DB. Designed to run on operator's laptop.
"""

import argparse
import csv
import json
import os
import sys
import time
from pathlib import Path
from statistics import median, mean, stdev

# Make `from pipeline import ...` and `from storage import ...` work
# when this script is run from any cwd.
HERE = Path(__file__).resolve().parent
BACKEND = HERE.parent
sys.path.insert(0, str(BACKEND))

# Override config to skip CORS validation when importing pipeline
os.environ.setdefault("CORS_ORIGINS", "http://localhost:5173")
# Force local-Whisper path (the pipeline routes to OpenAI when OPENAI_API_KEY
# is set; we want local timings for calibration consistency).
os.environ.pop("OPENAI_API_KEY", None)


def _import_pipeline():
    """Lazy import — keeps the script's --help fast even when whisper isn't installed yet."""
    from pipeline import (
        _fetch_lrclib,
        _parse_lrclib_record,
        _lrc_to_segments,
        _get_whisper_model,
    )
    return _fetch_lrclib, _parse_lrclib_record, _lrc_to_segments, _get_whisper_model


# ── Text alignment ──────────────────────────────────────────────────────


def _normalize_text(t: str) -> str:
    """Lowercase, strip punctuation, collapse whitespace. Aggressive normalization
    so 'Mujer Amante,' aligns with 'mujer amante'."""
    import re
    t = (t or "").lower().strip()
    t = re.sub(r"[^\w\s]", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t


def _word_set(t: str) -> set:
    return set(_normalize_text(t).split())


def _jaccard(a: str, b: str) -> float:
    """Word-level Jaccard similarity — robust to mistranscribed individual words."""
    sa, sb = _word_set(a), _word_set(b)
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


def _align(whisper_segs: list[dict], lrclib_segs: list[dict],
           min_similarity: float = 0.5) -> list[tuple]:
    """Greedy best-match alignment.

    For each Whisper segment, find the lrclib segment with the highest
    Jaccard text similarity, subject to:
      - similarity >= min_similarity (else mark unmatched)
      - lrclib.start within ±20s of whisper.start (avoid wild matches
        like alignment to a repeated chorus far away in the song)
      - each lrclib seg can match at most one whisper seg

    Returns list of (whisper_idx, lrclib_idx_or_None, similarity).
    """
    used = set()
    out = []
    for wi, w in enumerate(whisper_segs):
        best_li = None
        best_sim = 0.0
        for li, l in enumerate(lrclib_segs):
            if li in used:
                continue
            if abs(l["start"] - w["start"]) > 20.0:
                continue
            sim = _jaccard(w["text"], l["text"])
            if sim > best_sim:
                best_sim = sim
                best_li = li
        if best_li is not None and best_sim >= min_similarity:
            used.add(best_li)
            out.append((wi, best_li, best_sim))
        else:
            out.append((wi, None, best_sim))
    return out


# ── Strategies ──────────────────────────────────────────────────────────


def _strategy_S0(whisper_segs: list[dict], audio_path: str) -> list[dict]:
    """Baseline — current pipeline output (Whisper with words[0].start)."""
    return [dict(s) for s in whisper_segs]


def _strategy_S1(whisper_segs: list[dict], audio_path: str,
                 shift_s: float = 0.0) -> list[dict]:
    """Uniform shift. `shift_s` is computed AFTER S0 by the caller and
    passed back here. Negative shift moves starts earlier."""
    return [
        {**s, "start": max(0.0, s["start"] + shift_s)}
        for s in whisper_segs
    ]


def _strategy_S2(whisper_segs: list[dict], audio_path: str,
                 window_s: float = 0.2) -> list[dict]:
    """Snap each segment.start to the nearest acoustic onset in the
    vocal frequency band (200–2500 Hz). librosa.onset.onset_detect on
    a band-passed copy of the audio.

    Only snaps if an onset exists within `window_s` of the original
    start; otherwise leaves the timestamp untouched.
    """
    import librosa
    import numpy as np

    y, sr = librosa.load(audio_path, sr=22050, mono=True)
    # Band-pass to vocal range. We use a simple FFT mask — fast and good
    # enough for onset detection. librosa.effects.preemphasis would also
    # work but is less targeted.
    n_fft = 2048
    hop = 512
    stft = librosa.stft(y, n_fft=n_fft, hop_length=hop)
    freqs = librosa.fft_frequencies(sr=sr, n_fft=n_fft)
    mask = (freqs >= 200) & (freqs <= 2500)
    vocal_stft = stft * mask[:, None]
    vocal_y = librosa.istft(vocal_stft, hop_length=hop, length=len(y))

    onset_frames = librosa.onset.onset_detect(
        y=vocal_y, sr=sr, hop_length=hop,
        backtrack=True,  # snap to the nearest preceding low-energy point
        units="frames",
    )
    onset_times = librosa.frames_to_time(onset_frames, sr=sr, hop_length=hop)

    out = []
    for s in whisper_segs:
        original_start = s["start"]
        # Find nearest onset within window
        if len(onset_times) == 0:
            out.append(dict(s))
            continue
        dists = np.abs(onset_times - original_start)
        nearest_idx = int(np.argmin(dists))
        if dists[nearest_idx] <= window_s:
            new_start = float(onset_times[nearest_idx])
            out.append({**s, "start": new_start})
        else:
            out.append(dict(s))
    return out


# ── Whisper runner ──────────────────────────────────────────────────────


def _run_whisper_local(audio_path: str, model_name: str = "turbo") -> list[dict]:
    """Mirror the pipeline's local-whisper path (`pipeline.py:1486-1531`)
    but standalone for the harness. Returns the same shape:
    [{start, end, text}, ...] with `words[0].start` substituted when
    available — the exact behaviour the production pipeline ships.
    """
    _fetch_lrclib, _parse_lrclib_record, _lrc_to_segments, _get_whisper_model = _import_pipeline()
    model = _get_whisper_model(model_name)
    result = model.transcribe(
        audio_path,
        word_timestamps=True,
        condition_on_previous_text=False,
        no_speech_threshold=0.6,
        compression_ratio_threshold=2.4,
    )
    segments = []
    for seg in result.get("segments", []):
        text = (seg.get("text") or "").strip()
        if not text or len(text) < 3:
            continue
        if seg.get("no_speech_prob", 0) > 0.7:
            continue
        words = seg.get("words", [])
        if words:
            start = words[0]["start"]
            end = words[-1]["end"]
        else:
            start = seg["start"]
            end = seg["end"]
        segments.append({"start": float(start), "end": float(end), "text": text})
    return segments


# ── LRCLib resolver ─────────────────────────────────────────────────────


def _resolve_lrclib(artist: str, title: str) -> list[dict] | None:
    """Use the pipeline's _fetch_lrclib + _lrc_to_segments to get
    ground-truth segments. Returns None if no synced LRC available."""
    _fetch_lrclib, _parse_lrclib_record, _lrc_to_segments, _ = _import_pipeline()
    rec = _fetch_lrclib(artist, title, db=None)
    if not rec:
        return None
    parsed = _parse_lrclib_record(rec) if isinstance(rec, dict) and "synced" not in rec else rec
    synced = (parsed or rec).get("synced")
    if not synced:
        return None
    segs = _lrc_to_segments(synced)
    return segs or None


# ── Per-song run ────────────────────────────────────────────────────────


def _run_song(entry: dict, strategies: list[str]) -> dict:
    """Returns:
        {
          "artist": ..., "title": ..., "wav_path": ...,
          "groundtruth_count": N, "whisper_count": M, "aligned_count": K,
          "errors_by_strategy": {"S0": [...], "S1": [...], ...},
        }
    """
    artist = entry["artist"]
    title = entry["title"]
    wav_path = entry["wav_path"]
    if not os.path.exists(wav_path):
        return {"error": f"audio not found: {wav_path}", **entry}

    print(f"\n=== {artist} — {title} ===")
    print(f"  audio: {wav_path}")

    print("  [1/3] resolving LRCLib...")
    t0 = time.monotonic()
    gt = _resolve_lrclib(artist, title)
    if not gt:
        return {"error": "LRCLib synced not available", **entry}
    print(f"        {len(gt)} ground-truth segments ({time.monotonic()-t0:.1f}s)")

    print("  [2/3] running Whisper turbo...")
    t0 = time.monotonic()
    whisper_segs = _run_whisper_local(wav_path, model_name="turbo")
    print(f"        {len(whisper_segs)} whisper segments ({time.monotonic()-t0:.1f}s)")

    # Compute S0 baseline first; S1 derives its shift from S0 errors.
    print("  [3/3] applying strategies + aligning...")
    results = {}
    s0 = _strategy_S0(whisper_segs, wav_path)
    s0_align = _align(s0, gt)
    s0_errors = [
        s0[wi]["start"] - gt[li]["start"]
        for (wi, li, _) in s0_align if li is not None
    ]
    results["S0"] = s0_errors

    if "S1" in strategies and s0_errors:
        shift = -median(s0_errors)
        s1 = _strategy_S1(whisper_segs, wav_path, shift_s=shift)
        s1_align = _align(s1, gt)
        s1_errors = [
            s1[wi]["start"] - gt[li]["start"]
            for (wi, li, _) in s1_align if li is not None
        ]
        results["S1"] = s1_errors
        results["_S1_shift_s"] = shift

    if "S2" in strategies:
        try:
            s2 = _strategy_S2(whisper_segs, wav_path)
            s2_align = _align(s2, gt)
            s2_errors = [
                s2[wi]["start"] - gt[li]["start"]
                for (wi, li, _) in s2_align if li is not None
            ]
            results["S2"] = s2_errors
        except Exception as e:
            print(f"        S2 failed: {e}")

    return {
        "artist": artist,
        "title": title,
        "wav_path": wav_path,
        "groundtruth_count": len(gt),
        "whisper_count": len(whisper_segs),
        "aligned_count": len(s0_errors),
        "errors_by_strategy": results,
    }


# ── Aggregation + report ────────────────────────────────────────────────


def _stats(errors: list[float]) -> dict:
    """Stats on absolute errors in ms."""
    if not errors:
        return {"n": 0}
    abs_errs = [abs(e) * 1000 for e in errors]
    return {
        "n": len(errors),
        "mean_abs_ms": mean(abs_errs),
        "median_abs_ms": median(abs_errs),
        "p95_abs_ms": sorted(abs_errs)[int(len(abs_errs) * 0.95) - 1] if len(abs_errs) > 1 else abs_errs[0],
        "std_abs_ms": stdev(abs_errs) if len(abs_errs) > 1 else 0.0,
        "raw_mean_ms": mean([e * 1000 for e in errors]),
        "raw_median_ms": median([e * 1000 for e in errors]),
    }


def _build_report(per_song: list[dict], strategies: list[str]) -> str:
    """Markdown report. Per-strategy table + per-song breakdown."""
    lines = ["# Whisper calibration report\n"]
    lines.append(f"Corpus: {len(per_song)} songs\n")

    # Per-song summary
    lines.append("\n## Per-song aligned counts\n")
    lines.append("| Artist | Title | GT segs | Whisper segs | Aligned |")
    lines.append("|---|---|---:|---:|---:|")
    for r in per_song:
        if "error" in r:
            lines.append(f"| {r['artist']} | {r['title']} | — | — | ERROR: {r['error']} |")
            continue
        lines.append(
            f"| {r['artist']} | {r['title']} | "
            f"{r['groundtruth_count']} | {r['whisper_count']} | {r['aligned_count']} |"
        )

    # Aggregate per strategy
    lines.append("\n## Strategy comparison (absolute errors, ms)\n")
    lines.append("| Strategy | n | mean_abs | median_abs | p95_abs | std_abs | raw_mean (signed) | raw_median (signed) |")
    lines.append("|---|---:|---:|---:|---:|---:|---:|---:|")
    for sid in strategies:
        all_errs = []
        for r in per_song:
            errs = r.get("errors_by_strategy", {}).get(sid, [])
            all_errs.extend(errs)
        s = _stats(all_errs)
        if s["n"] == 0:
            lines.append(f"| {sid} | 0 | — | — | — | — | — | — |")
            continue
        lines.append(
            f"| **{sid}** | {s['n']} | "
            f"{s['mean_abs_ms']:.1f} | {s['median_abs_ms']:.1f} | "
            f"{s['p95_abs_ms']:.1f} | {s['std_abs_ms']:.1f} | "
            f"{s['raw_mean_ms']:+.1f} | {s['raw_median_ms']:+.1f} |"
        )

    # Per-song per-strategy detail
    lines.append("\n## Per-song detail\n")
    for r in per_song:
        if "error" in r:
            continue
        lines.append(f"\n### {r['artist']} — {r['title']}\n")
        lines.append("| Strategy | n | mean_abs (ms) | median_abs (ms) | p95_abs (ms) |")
        lines.append("|---|---:|---:|---:|---:|")
        for sid in strategies:
            errs = r["errors_by_strategy"].get(sid, [])
            s = _stats(errs)
            if s["n"] == 0:
                lines.append(f"| {sid} | 0 | — | — | — |")
            else:
                lines.append(
                    f"| {sid} | {s['n']} | "
                    f"{s['mean_abs_ms']:.1f} | {s['median_abs_ms']:.1f} | {s['p95_abs_ms']:.1f} |"
                )
        if "_S1_shift_s" in r["errors_by_strategy"]:
            lines.append(f"\nS1 shift applied: **{r['errors_by_strategy']['_S1_shift_s']*1000:+.1f} ms**")

    return "\n".join(lines)


# ── Main ────────────────────────────────────────────────────────────────


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--corpus", default=str(Path.home() / ".calibration" / "corpus.json"))
    p.add_argument("--report", default=str(Path.home() / ".calibration" / "report.md"))
    p.add_argument("--csv", default=str(Path.home() / ".calibration" / "errors.csv"))
    p.add_argument("--strategies", default="S0,S1,S2",
                   help="Comma-separated strategy IDs (S0,S1,S2). S3/S5 (large-v3) require model download.")
    p.add_argument("--limit", type=int, default=0,
                   help="Process at most N songs (0 = all). Useful for first dry-run.")
    args = p.parse_args()

    strategies = [s.strip() for s in args.strategies.split(",") if s.strip()]
    print(f"Strategies: {strategies}")

    with open(args.corpus) as f:
        corpus = json.load(f)
    if args.limit > 0:
        corpus = corpus[:args.limit]
    print(f"Corpus: {len(corpus)} songs")

    per_song = []
    for entry in corpus:
        try:
            r = _run_song(entry, strategies)
        except Exception as e:
            import traceback
            traceback.print_exc()
            r = {"error": str(e), **entry}
        per_song.append(r)

    # Write report
    report = _build_report(per_song, strategies)
    os.makedirs(os.path.dirname(args.report), exist_ok=True)
    with open(args.report, "w") as f:
        f.write(report)
    print(f"\nReport written: {args.report}")

    # Write CSV with raw errors for further analysis
    with open(args.csv, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["artist", "title", "strategy", "error_s"])
        for r in per_song:
            if "error" in r:
                continue
            for sid in strategies:
                for e in r["errors_by_strategy"].get(sid, []):
                    w.writerow([r["artist"], r["title"], sid, f"{e:.4f}"])
    print(f"CSV written: {args.csv}")


if __name__ == "__main__":
    main()
