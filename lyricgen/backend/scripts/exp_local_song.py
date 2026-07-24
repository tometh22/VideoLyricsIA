#!/usr/bin/env python3
"""Realistic local test of the CTC engine on a user-provided song.

Unlike the Rotor benchmark (perfect text), this reproduces the PROD flow:
  text  = lrclib plain lyrics (imperfect: studio version, may have ghost
          lines / missing repetitions vs what's actually sung)
  audio = vocal stem via demucs (R2 cache hit if seen before)
  timing = ctc_align.retime_segments (the production module)

Prints the timeline with per-line scores + gap diagnostics, and when
lrclib has synced lyrics, the delta vs lrclib's own timestamps as a
rough reference (valid for studio; expected to diverge wildly on lives).

Usage:
  CTC_ALIGN_ENABLED=1 python scripts/exp_local_song.py "/path/song.wav" "Artista" "Título"
"""
from __future__ import annotations

import os
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def synced_to_times(synced: str) -> dict[str, float]:
    """LRC '[mm:ss.xx] line' → {normalized_line: first_seen_seconds}."""
    out = {}
    for m in re.finditer(r"\[(\d+):(\d+(?:\.\d+)?)\]\s*(.+)", synced or ""):
        t = int(m.group(1)) * 60 + float(m.group(2))
        key = re.sub(r"\W+", "", m.group(3).lower())
        out.setdefault(key, t)
    return out


def main(audio: str, artist: str, title: str):
    from pipeline import _fetch_lrclib, _audio_duration
    import vocal_sep
    import ctc_align

    print(f"\n=== {artist} — {title} ===")
    dur = _audio_duration(audio)
    print(f"audio: {Path(audio).name}  ({dur:.0f}s)")

    lrc = _fetch_lrclib(artist, title, None, audio_duration=dur)
    if not lrc or not (lrc.get("plain") or "").strip():
        print("lrclib: SIN RESULTADO — en prod esto iría al path whisper/gemini")
        return
    lines = [ln.strip() for ln in lrc["plain"].splitlines() if ln.strip()]
    ldur = lrc.get("duration")
    print(f"lrclib: {len(lines)} líneas, duración registrada {ldur or '?'}s "
          f"(delta vs audio: {abs(dur - ldur):.0f}s)" if ldur else
          f"lrclib: {len(lines)} líneas")

    t0 = time.time()
    stem = vocal_sep.separate_vocals(audio)
    print(f"stem: {'OK' if stem else 'FALLÓ'} en {time.time()-t0:.0f}s")
    if not stem:
        return

    segs = [{"text": ln, "start": 0.0, "end": 0.0} for ln in lines]
    t1 = time.time()
    out = ctc_align.retime_segments(stem, segs, job_id="local-demo")
    print(f"ctc_align: {time.time()-t1:.1f}s")
    if not out:
        print("→ DECLINÓ (en prod: se mantiene el timing de la cascada)")
        return

    ref = synced_to_times(lrc.get("synced") or "")
    print(f"\n{'#':>3} {'start':>8} {'end':>8} {'score':>6} {'vs-lrc':>7}  texto")
    prev_end = 0.0
    for i, s in enumerate(out):
        words = s.get("words") or []
        score = sum(w["score"] for w in words) / len(words) if words else None
        key = re.sub(r"\W+", "", s["text"].lower())
        d = f"{s['start'] - ref[key]:+6.1f}s" if key in ref else "      -"
        gap = s["start"] - prev_end
        flag = ""
        if score is not None and score < 0.20:
            flag += "  ⚠ score bajo"
        if (s["end"] - s["start"]) < 0.3:
            flag += "  ⚠ colapsada"
        if gap > 25:
            flag += f"  (gap {gap:.0f}s)"
        print(f"{i:>3} {s['start']:>8.2f} {s['end']:>8.2f} "
              f"{score if score is not None else 0:>6.2f} {d:>7}  {s['text'][:52]}{flag}")
        prev_end = s["end"]

    scored = [(sum(w['score'] for w in (s.get('words') or [])) / max(len(s.get('words') or []), 1))
              for s in out if s.get('words')]
    if scored:
        srt = sorted(scored)
        print(f"\nresumen: {len(out)} líneas | score mediano {srt[len(srt)//2]:.2f} | "
              f"{sum(1 for x in scored if x < 0.2)} líneas con score <0.20")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2], sys.argv[3])
