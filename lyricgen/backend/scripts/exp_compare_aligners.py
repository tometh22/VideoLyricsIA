#!/usr/bin/env python3
"""Compare forced-alignment ENGINES on the KNOWN (ground-truth) lyric text.

Isolates ALIGNMENT quality from TRANSCRIPTION quality: every engine is fed the
exact correct text (Rotor's ground-truth lines) and we measure how tightly each
places the per-line onsets vs Rotor (AOO median/mean/p95 via score_benchmark).

Engines
-------
  cureau_full       forced_align.forced_align_lyrics  (baseline; crashes on the
                    repetitive live stem)
  cureau_chunked    forced_align.forced_align_lyrics_chunked  (VAD/uniform
                    windows, short clips → no crash)
  whisperx          whisperX transcribe → whisperx_reconcile.reconcile against
                    the known text (whisperX's word stamps, our text)

Raw engine outputs are cached under benchmark/dataset/<slug>/_aligner_cache/ so
re-runs (and scoring different engines) don't re-pay Replicate. Use
--force to recompute.

Writes the CHOSEN engine's segments to improvement_output.json so
score_benchmark.py reports it. Default chosen engine = cureau_chunked; override
with --write <engine>.

Usage:
    cd lyricgen/backend            # with env (REPLICATE_API_TOKEN etc.)
    python scripts/exp_compare_aligners.py --only rotor_donde_estan
    python scripts/exp_compare_aligners.py            # both, all engines
    python scripts/exp_compare_aligners.py --write cureau_chunked
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from statistics import mean, median

HERE = Path(__file__).resolve().parent
BACKEND = HERE.parent
sys.path.insert(0, str(BACKEND))
DATASET = BACKEND / "benchmark" / "dataset"

import forced_align  # noqa: E402


def _known_text(gt: list[dict]) -> str:
    return "\n".join((s.get("text") or "").strip() for s in gt if (s.get("text") or "").strip())


def _audio(d: Path) -> Path | None:
    return next((p for p in d.glob("audio.*") if not p.name.endswith(".json")), None)


def _p95(xs):
    s = sorted(xs)
    return s[max(0, int(len(s) * 0.95) - 1)] if s else 0.0


def _score_inline(gt: list[dict], segs: list[dict]) -> dict:
    """Repeat-aware AOO exactly like score_benchmark._aoo (nearest-in-time
    among strong text matches), computed inline so this script is standalone."""
    def jac(a, b):
        sa, sb = set((a or "").lower().split()), set((b or "").lower().split())
        return len(sa & sb) / len(sa | sb) if sa and sb else 0.0
    abs_off, signed = [], []
    for o in segs:
        ot = (o.get("text") or "").strip()
        if not ot:
            continue
        try:
            os_ = float(o["start"])
        except (KeyError, TypeError, ValueError):
            continue
        cand = [(jac(ot, g.get("text") or ""), g) for g in gt]
        strong = [(s, g) for s, g in cand if s >= 0.6]
        if strong:
            m = min(strong, key=lambda t: abs(os_ - float(t[1]["start"])))[1]
        else:
            s, g = max(cand, key=lambda t: t[0]) if cand else (0.0, None)
            m = g if s >= 0.4 else None
        if m is None:
            continue
        d = os_ - float(m["start"])
        abs_off.append(abs(d)); signed.append(d)
    if not abs_off:
        return {"n": 0}
    ms = median(signed)
    deoff = [abs(s - ms) for s in signed]
    return {
        "n": len(abs_off),
        "median": median(abs_off), "mean": mean(abs_off), "p95": _p95(abs_off),
        "median_signed": ms, "deoff_median": median(deoff), "deoff_p95": _p95(deoff),
    }


def _cache(d: Path, name: str):
    cd = d / "_aligner_cache"
    cd.mkdir(exist_ok=True)
    return cd / f"{name}.json"


def run_cureau_full(d: Path, audio: Path, text: str, force: bool) -> list[dict] | None:
    cp = _cache(d, "cureau_full")
    if cp.exists() and not force:
        return json.loads(cp.read_text()).get("segments")
    t0 = time.time()
    try:
        segs = forced_align.forced_align_lyrics(str(audio), text)
    except Exception as e:
        print(f"    cureau_full RAISED: {e}")
        segs = None
    dt = time.time() - t0
    cp.write_text(json.dumps({"segments": segs, "elapsed_s": dt}, ensure_ascii=False, default=str))
    print(f"    cureau_full: {len(segs) if segs else 0} segs in {dt:.0f}s")
    return segs


def run_cureau_chunked(d: Path, audio: Path, text: str, force: bool) -> list[dict] | None:
    cp = _cache(d, "cureau_chunked")
    if cp.exists() and not force:
        return json.loads(cp.read_text()).get("segments")
    # Use stem VAD if a stem exists; else None → uniform windows on the mix.
    vr = None
    try:
        import anchor_align
        vr = anchor_align.vocal_regions(str(audio)) or None
    except Exception:
        vr = None
    t0 = time.time()
    try:
        segs = forced_align.forced_align_lyrics_chunked(str(audio), text, vocal_regions=vr)
    except Exception as e:
        print(f"    cureau_chunked RAISED: {e}")
        segs = None
    dt = time.time() - t0
    cp.write_text(json.dumps({"segments": segs, "elapsed_s": dt}, ensure_ascii=False, default=str))
    print(f"    cureau_chunked: {len(segs) if segs else 0} segs in {dt:.0f}s (no crash)")
    return segs


def run_whisperx(d: Path, audio: Path, text: str, force: bool) -> list[dict] | None:
    cp = _cache(d, "whisperx")
    if cp.exists() and not force:
        return json.loads(cp.read_text()).get("segments")
    import whisperx_transcribe
    import whisperx_reconcile
    t0 = time.time()
    try:
        wx = whisperx_transcribe.transcribe_whisperx(str(audio), language="es", lyrics_hint=text)
        segs = whisperx_reconcile.reconcile(wx, text) if wx else None
    except Exception as e:
        print(f"    whisperx RAISED: {e}")
        segs = None
    dt = time.time() - t0
    cp.write_text(json.dumps({"segments": segs, "elapsed_s": dt}, ensure_ascii=False, default=str))
    print(f"    whisperx: {len(segs) if segs else 0} segs in {dt:.0f}s")
    return segs


ENGINES = {
    "cureau_full": run_cureau_full,
    "cureau_chunked": run_cureau_chunked,
    "whisperx": run_whisperx,
}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default="")
    ap.add_argument("--engines", default="cureau_full,cureau_chunked,whisperx")
    ap.add_argument("--write", default="cureau_chunked",
                    help="engine whose segments go to improvement_output.json")
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()

    engines = [e.strip() for e in args.engines.split(",") if e.strip()]
    dirs = sorted(p for p in DATASET.iterdir() if p.is_dir())
    if args.only:
        dirs = [d for d in dirs if d.name == args.only]

    for d in dirs:
        gt = json.loads((d / "ground_truth.json").read_text())
        text = _known_text(gt)
        audio = _audio(d)
        if not audio:
            print(f"[{d.name}] no audio, skip"); continue
        print(f"\n[{d.name}] {len(gt)} known lines, audio={audio.name}")
        results = {}
        for eng in engines:
            segs = ENGINES[eng](d, audio, text, args.force)
            if segs:
                results[eng] = _score_inline(gt, segs)

        print(f"  {'engine':<16} {'n':>3} {'median':>7} {'mean':>7} {'p95':>7} {'deoff_med':>9} {'deoff_p95':>9}")
        for eng in engines:
            r = results.get(eng)
            if not r or r.get("n", 0) == 0:
                print(f"  {eng:<16}  --   FAILED / empty")
                continue
            print(f"  {eng:<16} {r['n']:>3} {r['median']:>7.2f} {r['mean']:>7.2f} "
                  f"{r['p95']:>7.2f} {r['deoff_median']:>9.2f} {r['deoff_p95']:>9.2f}")

        # Write chosen engine to improvement_output.json
        if args.write in ENGINES:
            cp = _cache(d, args.write)
            if cp.exists():
                segs = json.loads(cp.read_text()).get("segments")
                if segs:
                    out = {"segments": segs, "source": f"align_{args.write}",
                           "meta": {"engine": args.write}}
                    (d / "improvement_output.json").write_text(
                        json.dumps(out, ensure_ascii=False, indent=2, default=str))
                    print(f"  -> wrote {args.write} ({len(segs)} segs) to improvement_output.json")


if __name__ == "__main__":
    main()
