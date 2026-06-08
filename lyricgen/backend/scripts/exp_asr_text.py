#!/usr/bin/env python3
"""Fase 1 A/B: which ASR transcribes the DENSE/live audio best (TEXT only)?

Compares, per benchmark song, the raw transcript TEXT of each backend against
Rotor's ground-truth text:
  - whisperX (Replicate)        — current prod ASR
  - gemini_chunked              — Gemini 2.5 over the FULL audio in short
                                  overlapping windows + anti-loop prompt (the
                                  same "REGLAS DURAS" that keep gap-recovery
                                  from looping on long clips) + seam de-dup.

Metrics vs Rotor:
  - WER (text accuracy, lower=better)
  - line coverage = % of Rotor lines whose text appears in the transcript
    (captures whether the dense choruses / outro were captured at all —
    whisperX drops the outro → low coverage).

Usage:
    cd lyricgen/backend
    # env: REPLICATE_API_TOKEN, GOOGLE_APPLICATION_CREDENTIALS (vertex), WHISPERX_ENABLED=1
    python scripts/exp_asr_text.py [--only <slug>]
"""
from __future__ import annotations

import argparse
import io
import json
import sys
import time
import unicodedata
from difflib import SequenceMatcher
from pathlib import Path

HERE = Path(__file__).resolve().parent
BACKEND = HERE.parent
sys.path.insert(0, str(BACKEND))
DATASET = BACKEND / "benchmark" / "dataset"

import numpy as np  # noqa: E402
import librosa  # noqa: E402
import soundfile as sf  # noqa: E402


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    return " ".join(c for c in s.lower().split())


def _wer(ref: str, hyp: str) -> float:
    import jiwer
    r = _norm(ref)
    if not r:
        return 0.0
    return jiwer.wer(r, _norm(hyp))


def _coverage(gt_lines: list[str], hyp_text: str) -> float:
    """Fraction of GT lines whose normalized text is ~present in hyp."""
    h = _norm(hyp_text)
    found = 0
    for ln in gt_lines:
        n = _norm(ln)
        if not n:
            continue
        # substring OR high fuzzy ratio against a sliding window of same length
        if n in h or SequenceMatcher(None, n, h).find_longest_match(0, len(n), 0, len(h)).size >= 0.8 * len(n):
            found += 1
    return found / max(1, len([l for l in gt_lines if _norm(l)]))


# ── whisperX ──────────────────────────────────────────────────────────
def asr_whisperx(audio_path: str, artist: str, song: str) -> str:
    import whisperx_transcribe as wx
    segs = wx.transcribe_whisperx(audio_path, "es", None) or []
    return "\n".join((s.get("text") or "").strip() for s in segs)


# ── Gemini chunked ────────────────────────────────────────────────────
_GEMINI_SYS = (
    "Sos un transcriptor experto de letras en español, nivel Rotor.\n"
    "Te doy un FRAGMENTO de audio de ~{dur:.0f}s de una canción"
    "{who}.\n"
    "Transcribí EXACTAMENTE lo que se canta en ESTE fragmento, una frase por línea.\n\n"
    "REGLAS DURAS:\n"
    "1. SOLO lo que escuchás en estos segundos. NO completes el estribillo entero, "
    "NO repitas en loop una frase que no se repite en el audio.\n"
    "2. SÍ transcribí las repeticiones que realmente se cantan (coros que repiten).\n"
    "3. Ad-libs/gritos/vocalizaciones: transcribilos como se oyen (ej. 'Oh, oh', 'Ah, ah') "
    "o etiquetá (grito)/(instrumental)/(silencio) si no hay letra.\n"
    "4. Mayúscula al inicio de cada línea. SIN números, SIN timestamps, SIN nada más que el texto."
)


def asr_gemini_chunked(audio_path: str, artist: str, song: str,
                       chunk_s: float = 28.0, overlap_s: float = 4.0) -> str:
    import pipeline
    from google import genai
    client = pipeline._get_genai_client()
    if client is None:
        return ""
    sr = 22050
    y, _ = librosa.load(audio_path, sr=sr, mono=True)
    dur = len(y) / sr
    who = f" ({artist} — {song})" if artist else ""
    acc: list[str] = []
    t = 0.0
    while t < dur - 0.5:
        c0, c1 = t, min(dur, t + chunk_s)
        clip = y[int(c0 * sr):int(c1 * sr)]
        buf = io.BytesIO()
        sf.write(buf, clip, sr, format="WAV")
        sysp = _GEMINI_SYS.format(dur=(c1 - c0), who=who)
        try:
            resp = client.models.generate_content(
                model="gemini-2.5-flash",
                contents=[
                    genai.types.Part.from_bytes(data=buf.getvalue(), mime_type="audio/wav"),
                    genai.types.Part.from_text(text="Transcribí este fragmento."),
                ],
                config=genai.types.GenerateContentConfig(
                    system_instruction=sysp, temperature=0.1, max_output_tokens=600,
                    thinking_config=genai.types.ThinkingConfig(thinking_budget=0),
                ),
            )
            lines = [l.strip() for l in (resp.text or "").splitlines() if l.strip()]
        except Exception as e:
            print(f"    gemini chunk {c0:.0f}-{c1:.0f} failed: {e}", flush=True)
            lines = []
        # seam de-dup: drop leading lines of this chunk that match the tail of acc
        for cl in lines:
            cn = _norm(cl)
            if any(SequenceMatcher(None, cn, _norm(p)).ratio() > 0.85 for p in acc[-3:]):
                continue
            acc.append(cl)
        t += chunk_s - overlap_s
    return "\n".join(acc)


BACKENDS = {
    "whisperx": asr_whisperx,
    "gemini_chunked": asr_gemini_chunked,
}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default="")
    ap.add_argument("--backends", default="whisperx,gemini_chunked")
    args = ap.parse_args()
    want = [b.strip() for b in args.backends.split(",") if b.strip()]

    dirs = sorted(p for p in DATASET.iterdir() if p.is_dir())
    if args.only:
        dirs = [d for d in dirs if d.name == args.only]

    rows = []
    for d in dirs:
        gt = json.loads((d / "ground_truth.json").read_text())
        gt_lines = [(s.get("text") or "").strip() for s in gt]
        gt_text = "\n".join(gt_lines)
        meta = json.loads((d / "metadata.json").read_text()) if (d / "metadata.json").exists() else {}
        audio = next((p for p in d.glob("audio.*") if not p.name.endswith(".json")), None)
        if not audio:
            continue
        for b in want:
            print(f"[{d.name}] {b} ...", flush=True)
            t0 = time.time()
            try:
                txt = BACKENDS[b](str(audio), meta.get("artist", ""), meta.get("song_title", ""))
            except Exception as e:
                print(f"  ✗ {b} failed: {e}")
                continue
            dt = time.time() - t0
            w = _wer(gt_text, txt)
            cov = _coverage(gt_lines, txt)
            n_lines = len([l for l in txt.splitlines() if l.strip()])
            rows.append((d.name, b, w, cov, n_lines, len(gt_lines), dt))
            (d / f"asr_{b}.txt").write_text(txt)
            print(f"  ✓ {b}: WER={w:.3f} coverage={cov:.0%} lines={n_lines}/{len(gt_lines)} ({dt:.0f}s)")

    print("\n=== ASR A/B vs Rotor (texto) ===")
    print(f"{'song':22} {'backend':16} {'WER':>6} {'coverage':>9} {'lines':>10} {'sec':>5}")
    for song, b, w, cov, nl, ngt, dt in rows:
        print(f"{song[:22]:22} {b:16} {w:6.3f} {cov:8.0%} {nl:>4}/{ngt:<4} {dt:5.0f}")


if __name__ == "__main__":
    main()
