#!/usr/bin/env python3
"""EXPERIMENT: deterministic crowd-zone fill — Gemini OUT of the chorus loop.

The 7-generation lesson: Gemini's per-run variability on crowd zones is
structural (59/71/101/104 lines across runs; the chorus block present in
one run, absent in the next). No post-processing fixes input that didn't
arrive. Here the zones are detected WITHOUT ASR and filled from KNOWN
anchored text:

  1. After CTC+M5+condense, find COVERAGE GAPS > min_gap between lines.
  2. For each gap, find its acoustic TWIN via beat-synced chroma
     subsequence-DTW against the whole song (pipeline._find_gap_twin —
     the validated transplant machinery). The twin overlaps ANCHORED
     lines (verses/choruses placed at ±0.1s).
  3. Warp the twin's anchored lines onto the gap (_gap_warp_segments) →
     deterministic text + timing, independent of what Gemini emitted.

Gate criteria (user rule: local until RIGHT):
  - zones filled with correct canonical text (no 'eso', no bridge/chorus
    mixups: a bridge gap must pull BRIDGE text via its twin),
  - stable across librettos (run against the cached SMALL libretto that
    reproduced gen-7's holes),
  - no regression on already-good lines.

Usage: exp_zone_fill.py [--fresh]   (--fresh re-runs Gemini; default uses cache)
"""
from __future__ import annotations

import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

CACHE = pathlib.Path("/tmp/libreto_nada_cache.json")
D = pathlib.Path("benchmark/dataset/rotor_nada_fue")


def fill_gaps_by_twin(segments, mix_path, min_gap=4.0, max_cost=0.20):
    """Deterministic gap fill: chroma-twin transplant from anchored lines."""
    import librosa
    import numpy as np
    import pipeline as pl

    y, sr = librosa.load(mix_path, sr=22050, mono=True)
    ch = librosa.feature.chroma_cqt(y=y, sr=sr, hop_length=512)
    _, bts = librosa.beat.beat_track(y=y, sr=sr, hop_length=512)
    beat_t = librosa.frames_to_time(bts, sr=sr, hop_length=512)
    Csync = librosa.util.sync(ch, bts, aggregate=np.median)
    nb = min(Csync.shape[1], len(beat_t))
    Csync, beat_t = Csync[:, :nb], beat_t[:nb]

    anchored = [s for s in segments
                if not s.get("ctc_skipped") and (s["end"] - s["start"]) >= 0.3]
    # iter-3: a long SKIP stub (>6s) hides its span from gap detection
    # ("Nada, nada de esto" 61.6→75.1 in the degraded run) — convert it
    # into a gap candidate, keeping its text as a fill hint.
    segs2 = [s for s in segments
             if not (s.get("ctc_skipped") and (s["end"] - s["start"]) > 6.0)]
    # stem voiced map for gate-1 (singer holding a note → sustain, no fill)
    import ctc_align as _ca
    import torch as _t
    ys, srs = librosa.load(str(D / "stem.wav"), sr=16000, mono=True), 16000
    ys = ys[0] if isinstance(ys, tuple) else ys
    model, dic, blank = _ca._load_model()
    star_id = model.config.vocab_size

    def mix_stem_ratio(a, b):
        """Documented discriminator (R&D runbook): crowd chant = mix has
        2.5-4.7x the stem's energy (demucs erased the crowd); a singer
        holding a note = stem dominates (ratio ~1)."""
        import numpy as _np
        s_ = ys[int(a * 16000):int(b * 16000)]
        m_ = librosa.resample(y[int(a * sr):int(b * sr)],
                              orig_sr=sr, target_sr=16000)
        if not len(s_) or not len(m_):
            return 1.0
        rs = float(_np.sqrt(_np.mean(s_ ** 2))) + 1e-6
        rm = float(_np.sqrt(_np.mean(m_[:len(s_)] ** 2))) + 1e-6
        return rm / rs

    def verify_fill(texts_fill, a, b):
        """Forced-align candidate texts on the gap's MIX emissions; mean
        word score decides chant (fill) vs instrumental (hold)."""
        import torchaudio.functional as AF
        g16 = librosa.resample(y[int(a * sr):int(b * sr)],
                               orig_sr=sr, target_sr=16000)
        em = _ca._emissions(model, _t.tensor(g16).unsqueeze(0), blank, 0.5)
        tg, words = _ca.build_targets(texts_fill, dic, star_id,
                                      word_sep_id=dic.get("|"))
        if not tg or len(tg) >= em.shape[0]:
            return 0.0
        al, sc = AF.forced_align(em.unsqueeze(0),
                                 _t.tensor(tg, dtype=_t.int32).unsqueeze(0),
                                 blank=blank)
        spans = [(s_.start, s_.end, float(s_.score))
                 for s_ in AF.merge_tokens(al[0], sc[0].exp(), blank=blank)]
        lt = _ca.spans_to_lines(spans, words, len(texts_fill), 320 / 16000)
        scs = [w[3] for l in lt if l for w in l[2]]
        return sum(scs) / len(scs) if scs else 0.0

    out = list(segs2)
    inserts = []
    for i in range(len(segs2) - 1):
        gs, ge = segs2[i]["end"], segs2[i + 1]["start"]
        if ge - gs < min_gap:
            continue
        found = pl._find_gap_twin(anchored, gs + 0.2, ge - 0.2, Csync, beat_t,
                                  max_cost=max_cost)
        if not found:
            print(f"   gap {gs:.1f}-{ge:.1f}: sin gemelo (queda vacío)")
            continue
        src, cost = found
        if not src:
            continue
        # gate-2: forced-align VERIFICATION on the mix — chant scores
        # ~0.2+, instrumental/screams score near 0 (measured S2)
        vsc = verify_fill([x.get("text") or "" for x in src],
                          gs + 0.5, ge - 0.5)
        if vsc < 0.20:
            print(f"   gap {gs:.1f}-{ge:.1f}: verificación {vsc:.2f} — "
                  f"no es coreo cantado, se sostiene")
            continue
        warped = pl._gap_warp_segments(src, gs + 0.2, ge - 0.2)
        # monotonic clamp (iter-1: two warped lines overlapped 87.7-91.8)
        prev_end = gs + 0.2
        for w in warped:
            w["start"] = max(w["start"], prev_end)
            w["end"] = max(min(w["end"], ge - 0.2), w["start"] + 0.3)
            prev_end = w["end"]
            w["zone_fill"] = round(cost, 3)
        print(f"   gap {gs:.1f}-{ge:.1f}: gemelo {src[0]['start']:.1f}-"
              f"{src[-1]['end']:.1f} cost={cost:.3f} → {len(warped)} líneas")
        inserts.extend(warped)
    out.extend(inserts)
    out.sort(key=lambda s: s["start"])
    return out


def main():
    import ctc_align
    import performance_text

    mix = str(next(p for p in D.iterdir()
                   if p.suffix in (".wav", ".mp3") and p.stem != "stem"))
    if "--fresh" in sys.argv or not CACHE.exists():
        texts = performance_text.transcribe_performance(
            mix, "Coti", "Nada Fue Un Error")
        CACHE.write_text(json.dumps(texts, ensure_ascii=False))
    texts = json.loads(CACHE.read_text())
    print(f"libreto: {len(texts)} líneas ({'fresco' if '--fresh' in sys.argv else 'cache'})")

    segs = [{"text": t, "start": 0.0, "end": 0.0} for t in texts]
    out = ctc_align.retime_segments(str(D / "stem.wav"), segs, "zonefill", mix, 0.5)
    out = ctc_align.condense_repeated_skips(out)
    print("— gaps y gemelos:")
    out = fill_gaps_by_twin(out, mix)

    gt = json.loads((D / "ground_truth.json").read_text())
    print("\n=== ZONAS CLAVE vs Rotor ===")
    for titulo, lo, hi in [("CORO 1 (R: 65.2/76.3/85.5)", 58, 93),
                           ("Z 114-130 (R: 114.2/120.8/122.8/127.3)", 112, 131),
                           ("BRIDGE 2 (R: 221.9 'Y te digo...')", 214, 232),
                           ("FINAL (R: 247.2/250.9/258.2)", 243, 266)]:
        print(f"--- {titulo}")
        for s in out:
            if lo < s["start"] < hi:
                f = ("ZONE" if s.get("zone_fill") else
                     f"C{s['ctc_condensed']}" if s.get("ctc_condensed") else
                     "SKIP" if s.get("ctc_skipped") else
                     "MIX" if s.get("ctc_recovered") else "")
                print(f"  {s['start']:6.1f}-{s['end']:6.1f} {f:5s} {s['text'][:68]}")


if __name__ == "__main__":
    main()
