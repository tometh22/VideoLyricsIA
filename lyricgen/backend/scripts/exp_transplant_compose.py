#!/usr/bin/env python3
"""EXPERIMENT: CTC engine + chroma-twin TRANSPLANT composition.

For skipped groups that M5's mix-CTC rejected (the crowd sings but no
speech model can READ words in crowd-mush), don't read — MATCH SHAPE:
find the window's acoustic twin (the chorus the artist sang clean,
already CTC-aligned) by beat-synced chroma subsequence-DTW, and warp the
twin's line timings onto the window. M5 reads where legible; the twin
copies shape where not. Uses the gap-transplant machinery already in
staging (pipeline._find_gap_twin/_gap_warp_segments, validated 0.1s vs
Rotor on this very song's hole-210).
"""
from __future__ import annotations

import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import numpy as np  # noqa: E402


def main():
    import librosa
    import ctc_align
    import pipeline as pl
    from pipeline import _fetch_lrclib

    d = pathlib.Path("benchmark/dataset/rotor_nada_fue")
    mix = next(p for p in d.iterdir()
               if p.suffix in (".wav", ".mp3") and p.stem != "stem")
    gt = json.loads((d / "ground_truth.json").read_text())

    lrc = _fetch_lrclib("Coti", "Nada Fue Un Error", None, audio_duration=367)
    lines = [l.strip() for l in lrc["plain"].splitlines() if l.strip()]
    segs_in = [{"text": l, "start": 0.0, "end": 0.0} for l in lines]
    out = ctc_align.retime_segments(str(d / "stem.wav"), segs_in,
                                    job_id="compose", mix_path=str(mix))
    skipped = [i for i, s in enumerate(out) if s.get("ctc_skipped")]
    print(f"tras CTC+M5 siguen salteadas: {skipped}")

    aligned = [s for s in out if not s.get("ctc_skipped")]
    y, sr = librosa.load(str(mix), sr=22050, mono=True)
    ch = librosa.feature.chroma_cqt(y=y, sr=sr, hop_length=512)
    _, bts = librosa.beat.beat_track(y=y, sr=sr, hop_length=512)
    beat_t = librosa.frames_to_time(bts, sr=sr, hop_length=512)
    Csync = librosa.util.sync(ch, bts, aggregate=np.median)
    nb = min(Csync.shape[1], len(beat_t))
    Csync, beat_t = Csync[:, :nb], beat_t[:nb]
    print(f"chroma listo: {nb} beats")

    # line_times shape for recovery_window
    lt = [None if s.get("ctc_skipped") else (s["start"], s["end"], [1])
          for s in out]
    for grp in ctc_align.group_consecutive(skipped):
        win = ctc_align.recovery_window(lt, grp, len(y) / sr, max_s=90.0)
        if not win:
            print(f"grupo {grp}: sin ventana")
            continue
        gs, ge = win
        found = pl._find_gap_twin(aligned, gs, ge, Csync, beat_t,
                                  max_cost=0.20)
        if not found:
            print(f"grupo {grp} ({gs:.1f}-{ge:.1f}s): SIN gemelo (declina)")
            continue
        src, cost = found
        tx = pl._gap_warp_segments(src, gs, ge)
        print(f"\ngrupo {grp} ventana {gs:.1f}-{ge:.1f}s — gemelo cost={cost:.3f} "
              f"({src[0]['start']:.1f}-{src[-1]['end']:.1f}s, {len(src)} líneas):")
        for t in tx:
            cands = [(abs(g['start'] - t['start']), g['start'], g['text'][:30])
                     for g in gt if abs(g['start'] - t['start']) < 12]
            ref = f"| GT cerca: {min(cands)[1]:7.2f} {min(cands)[2]}" if cands else "| GT: nada cerca"
            print(f"  {t['start']:7.2f}-{t['end']:7.2f}  {t['text'][:38]:38s} {ref}")


if __name__ == "__main__":
    main()
