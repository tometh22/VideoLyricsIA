#!/usr/bin/env python3
"""Debug harness for chunked forced-align: caches EACH window's raw cureau
wordstamps to disk so the SHARED assembly (forced_align._assemble_windows)
can be iterated offline (free) after a single paid alignment pass.

Pass 1 (paid):  python scripts/exp_chunk_debug.py --only <slug> --stem --align
Pass 2+ (free): python scripts/exp_chunk_debug.py --only <slug> --stem
                (re-runs windowing + assembly on the cached words)
"""
from __future__ import annotations
import argparse, json, os, sys
from pathlib import Path
from statistics import mean, median

HERE = Path(__file__).resolve().parent
BACKEND = HERE.parent
sys.path.insert(0, str(BACKEND))
DATASET = BACKEND / "benchmark" / "dataset"
import forced_align as fa  # noqa: E402


def _p95(xs):
    s = sorted(xs); return s[max(0, int(len(s)*0.95)-1)] if s else 0.0


def score(gt, segs):
    def jac(a, b):
        sa, sb = set((a or "").lower().split()), set((b or "").lower().split())
        return len(sa & sb)/len(sa | sb) if sa and sb else 0.0
    ao, sg = [], []
    for o in segs:
        ot = (o.get("text") or "").strip()
        if not ot: continue
        try: os_ = float(o["start"])
        except Exception: continue
        cand = [(jac(ot, g.get("text") or ""), g) for g in gt]
        strong = [(s, g) for s, g in cand if s >= 0.6]
        if strong: m = min(strong, key=lambda t: abs(os_-float(t[1]["start"])))[1]
        else:
            s, g = max(cand, key=lambda t: t[0]) if cand else (0.0, None)
            m = g if s >= 0.4 else None
        if m is None: continue
        d = os_-float(m["start"]); ao.append(abs(d)); sg.append(d)
    if not ao: return {"n": 0}
    ms = median(sg); de = [abs(s-ms) for s in sg]
    return {"n": len(ao), "median": round(median(ao), 3), "mean": round(mean(ao), 3),
            "p95": round(_p95(ao), 3), "median_signed": round(ms, 3),
            "deoff_median": round(median(de), 3), "deoff_p95": round(_p95(de), 3)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default="")
    ap.add_argument("--align", action="store_true", help="run cureau per window (paid)")
    ap.add_argument("--stem", action="store_true", help="align the demucs vocal stem (vocals.wav)")
    ap.add_argument("--target", type=float, default=30.0)
    ap.add_argument("--overlap", type=float, default=3.0)
    args = ap.parse_args()

    dirs = sorted(p for p in DATASET.iterdir() if p.is_dir())
    if args.only: dirs = [d for d in dirs if d.name == args.only]
    tag = "stem" if args.stem else "mix"

    for d in dirs:
        gt = json.loads((d/"ground_truth.json").read_text())
        lyric_lines = [(s.get("text") or "").strip() for s in gt if (s.get("text") or "").strip()]
        audio = (d/"vocals.wav") if args.stem else next(
            (p for p in d.glob("audio.*") if not p.name.endswith(".json")), None)
        if not audio or not audio.exists():
            print(f"[{d.name}] no audio ({audio}), skip"); continue
        dur = fa._audio_duration_s(str(audio))
        # window planning uses VAD on the SAME audio we align
        try:
            import anchor_align
            vr = anchor_align.vocal_regions(str(audio)) or None
        except Exception:
            vr = None
        windows = fa._plan_windows(dur, vr, target_s=args.target, overlap_s=args.overlap)
        line_map = fa._assign_lines_to_windows(lyric_lines, windows, dur, vocal_regions=vr)
        cdir = d/f"_chunk_cache_{tag}"; cdir.mkdir(exist_ok=True)
        print(f"\n[{d.name}/{tag}] dur={dur:.0f}s windows={len(windows)} lines={len(lyric_lines)}")

        per_window_words = []
        ok = 0
        for wi, ((ws, we), idxs) in enumerate(zip(windows, line_map)):
            wf = cdir/f"w{wi:02d}.json"
            words = None
            if args.align and idxs:
                clip = fa._slice_clip(str(audio), ws, we-ws)
                if clip:
                    try:
                        words = fa._cureau_wordstamps(
                            clip, "\n".join(lyric_lines[i] for i in idxs),
                            total_budget_s=120.0)
                    finally:
                        try: os.unlink(clip)
                        except OSError: pass
                wf.write_text(json.dumps({"ws": ws, "we": we, "idxs": idxs,
                                          "words": words}, ensure_ascii=False, default=str))
            if wf.exists():
                cached = json.loads(wf.read_text())
                words = cached.get("words")
            per_window_words.append(words)
            if words: ok += 1
            print(f"  w{wi:02d} {ws:6.1f}-{we:6.1f} lines={idxs[0] if idxs else '-'}..{idxs[-1] if idxs else '-'} "
                  f"-> {len(words) if words else 0} words")

        final, covered = fa._assemble_windows(windows, line_map, per_window_words,
                                              lyric_lines, dur)
        print(f"  ok_windows={ok}/{len(windows)} covered={covered}/{len(lyric_lines)}")
        if final:
            print("  SCORE:", score(gt, final))
            out = {"segments": final, "source": f"align_cureau_chunked_{tag}",
                   "meta": {"engine": "cureau_chunked", "audio": tag,
                            "ok_windows": ok, "covered": covered}}
            (d/"improvement_output.json").write_text(
                json.dumps(out, ensure_ascii=False, indent=2, default=str))
            print(f"  -> wrote improvement_output.json")


if __name__ == "__main__":
    main()
