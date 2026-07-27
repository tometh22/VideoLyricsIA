#!/usr/bin/env python3
"""EXPERIMENT M5: recover crowd-sung lines from the MIX, window-confined.

The crowd's voice IS in the mix — demucs erases it from the stem, which
is why the engine skips those lines (no evidence). Global mix alignment
fails (instruments hallucinate emissions — negative result documented).
But a SKIPPED line comes with a confined window: the span between its
anchored neighbours. Within 10-30s, with star edges and a strong prior,
aligning the line's text against MIX emissions is a much easier problem.

Test: rotor_nada_fue — the engine (stem, skip-arcs) skips the crowd
chorus lines; recover each skipped cluster from the mix window and score
the recovered onsets against Rotor's GT.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
DATASET = Path(__file__).resolve().parent.parent / "benchmark" / "dataset"

SLUG = sys.argv[1] if len(sys.argv) > 1 else "rotor_nada_fue"


def main():
    import torch
    import torchaudio
    import torchaudio.functional as AF
    import ctc_align
    from pipeline import _fetch_lrclib

    d = DATASET / SLUG
    audio = next(p for p in d.iterdir()
                 if p.suffix in (".wav", ".mp3") and p.stem != "stem")
    gt = json.loads((d / "ground_truth.json").read_text())

    lrc = _fetch_lrclib("Coti", "Nada Fue Un Error", None, audio_duration=367)
    lines = [l.strip() for l in lrc["plain"].splitlines() if l.strip()]
    segs = [{"text": l, "start": 0.0, "end": 0.0} for l in lines]
    out = ctc_align.retime_segments(str(d / "stem.wav"), segs, job_id="m5-stem")
    skipped = [i for i, s in enumerate(out) if s.get("ctc_skipped")]
    print(f"stem pass: {len(skipped)} salteadas: {skipped}")

    # group consecutive skipped lines; window = between anchored neighbours
    groups, cur = [], [skipped[0]]
    for i in skipped[1:]:
        if i == cur[-1] + 1:
            cur.append(i)
        else:
            groups.append(cur)
            cur = [i]
    groups.append(cur)

    model, dictionary, blank_id = ctc_align._load_model()
    star_id = model.config.vocab_size
    wav, sr = torchaudio.load(str(audio))
    wav = wav.mean(0, keepdim=True)
    if sr != ctc_align.SR:
        wav = torchaudio.functional.resample(wav, sr, ctc_align.SR)
    SR, FRAME = ctc_align.SR, ctc_align.FRAME

    def norm(t):
        return re.sub(r"\W+", "", t.lower())

    gtn = [(g["start"], norm(g["text"])) for g in gt]
    for grp in groups:
        lo = max(0.0, (out[grp[0] - 1]["end"] if grp[0] > 0 else 0.0) - 1.0)
        hi_idx = grp[-1] + 1
        hi = (out[hi_idx]["start"] + 1.0) if hi_idx < len(out) else wav.shape[1] / SR
        if hi - lo < 2.0 or hi - lo > 60.0:
            print(f"grupo {grp}: ventana {lo:.1f}-{hi:.1f} fuera de rango — skip")
            continue
        chunk = wav[:, int(lo * SR):int(hi * SR)]
        em = ctc_align._emissions(model, chunk, blank_id, ctc_align._star_delta())
        texts = [out[i]["text"] for i in grp]
        targets, words = ctc_align.build_targets(texts, dictionary, star_id,
                                                 word_sep_id=dictionary.get("|"))
        if len(targets) >= em.shape[0]:
            print(f"grupo {grp}: más tokens que frames — skip")
            continue
        aligned, scores = AF.forced_align(
            em.unsqueeze(0), torch.tensor(targets, dtype=torch.int32).unsqueeze(0),
            blank=blank_id)
        spans_t = AF.merge_tokens(aligned[0], scores[0].exp(), blank=blank_id)
        spans = [(sp.start, sp.end, float(sp.score)) for sp in spans_t]
        lt = ctc_align.spans_to_lines(spans, words, len(texts), FRAME / SR)
        print(f"\ngrupo {grp} ventana {lo:.1f}-{hi:.1f}s (mix):")
        for k, i in enumerate(grp):
            if lt[k] is None:
                print(f"  {i:3} sin alineo")
                continue
            s, e, ws = lt[k]
            s += lo
            e += lo
            msc = sum(w[3] for w in ws) / len(ws) if ws else 0
            n = norm(texts[k])
            cands = [(abs(gs - s), gs) for gs, gn in gtn
                     if len(n) > 14 and (n[:14] in gn or gn[:14] in n)
                     and abs(gs - s) < 30]
            ref = f"rotor {min(cands)[1]:7.2f} (d={min(cands)[0]:5.2f})" if cands else "rotor ?"
            print(f"  {i:3} {s:7.2f}-{e:7.2f} score {msc:.2f}  {ref}  | {texts[k][:40]}")


if __name__ == "__main__":
    main()
