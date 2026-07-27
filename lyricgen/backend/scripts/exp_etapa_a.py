#!/usr/bin/env python3
"""ETAPA A end-to-end: performance text (Gemini) → CTC engine → vs Rotor.

The missing piece the visual test proved: Rotor transcribes THE live
(real structure: every repetition, the crowd's actual words); we were
timing the STUDIO text. Here: Gemini-pro chunked transcript of the live
(93% coverage, validated R&D) feeds the CTC engine (skip-arcs + M5).
Score vs Rotor GT with repeat-aware nearest matching.

Usage: exp_etapa_a.py /tmp/gemini_nada_fue.json rotor_nada_fue
"""
from __future__ import annotations

import json
import pathlib
import re
import statistics
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))


def norm(t: str) -> str:
    return re.sub(r"\W+", "", t.lower())


def main(gem_path: str, slug: str):
    import ctc_align

    d = pathlib.Path("benchmark/dataset") / slug
    gem = json.loads(pathlib.Path(gem_path).read_text())
    glines = gem["segments"] if isinstance(gem, dict) else gem
    texts = []
    for g in glines:
        t = (g.get("text") or "").strip()
        # stage-direction prefixes/lines: "(Público cantando) ...", "(...)"
        t = re.sub(r"^[^)]*\)\s*", "", t) if ")" in t[:25] else t
        t = t.strip()
        if not t or re.fullmatch(r"\(.*\)", t):
            continue
        # pure exclamations / counts ("¡Eh!", "¡Dos, tres, cuatro!")
        if len(re.sub(r"[^a-záéíóúñü]", "", t.lower())) < 6:
            continue
        # seam duplicates: contained in the previous line (or vice versa)
        n = norm(t)
        if texts:
            p = norm(texts[-1])
            if n in p:
                continue
            if p in n:
                texts[-1] = t
                continue
        texts.append(t)
    print(f"libreto del performance: {len(texts)} líneas (Gemini, limpio)")

    segs = [{"text": t, "start": 0.0, "end": 0.0} for t in texts]
    mix = next(p for p in d.iterdir()
               if p.suffix in (".wav", ".mp3") and p.stem != "stem")
    out = ctc_align.retime_segments(str(d / "stem.wav"), segs,
                                    job_id="etapa-a", mix_path=str(mix))
    if out is None:
        print("DECLINÓ — revisar skip-frac/guards")
        return
    gt = json.loads((d / "ground_truth.json").read_text())

    # repeat-aware: each output line matches the TEXT-similar GT line
    # CLOSEST IN TIME (GT repeats lines too)
    offs, shown = [], 0
    sk = sum(1 for s in out if s.get("ctc_skipped"))
    for s in out:
        if s.get("ctc_skipped"):
            continue
        n = norm(s["text"])
        if len(n) < 8:
            continue
        cands = [(abs(g["start"] - s["start"]), g["start"]) for g in gt
                 if len(norm(g["text"])) >= 8
                 and (n[:12] in norm(g["text"]) or norm(g["text"])[:12] in n)]
        if not cands:
            continue
        dmin, gs = min(cands)
        offs.append(dmin)
    offs.sort()
    if not offs:
        print("sin matches")
        return
    med = statistics.median(offs)
    p95 = offs[max(0, int(len(offs) * 0.95) - 1)]
    print(f"\nETAPA A vs Rotor (n={len(offs)} líneas matcheadas, {sk} salteadas):")
    print(f"  mediana {med:.2f}s | p95 {p95:.2f}s | max {max(offs):.2f}s | "
          f"<=0.5s: {sum(1 for o in offs if o <= 0.5)}/{len(offs)} | "
          f">2s: {sum(1 for o in offs if o > 2)}")
    print("\ntimeline (primeras 40):")
    for s in out[:40]:
        tag = " [SALTEADA]" if s.get("ctc_skipped") else (
            " [mix]" if s.get("ctc_recovered") else "")
        print(f"  {s['start']:7.2f}-{s['end']:7.2f}  {s['text'][:52]}{tag}")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else "rotor_nada_fue")
