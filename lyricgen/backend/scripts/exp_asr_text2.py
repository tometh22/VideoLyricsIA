#!/usr/bin/env python3
"""Fase 1 (v2): best ASR for DENSE/live audio — hardened Gemini-chunked.

Improvements over scripts/exp_asr_text.py:
  1. HARD seam de-dup: instead of fuzzy-vs-last-3, we align the OVERLAP
     region between consecutive chunks. Each chunk knows it shares the
     last `overlap_s` of audio with the previous one, so we look for the
     longest line-suffix of `acc` that matches a line-prefix of the new
     chunk and splice there (drop the duplicated overlap lines). Falls
     back to per-line fuzzy dedup against a small tail window.
  2. Tunable chunk_s / overlap_s / model via CLI; sweep helper.
  3. whisper-1 (OpenAI) backend for comparison.
  4. Metrics: WER total + WER letter-only (excludes Rotor ad-lib/label
     lines like "Ah, ah", "Oh, oh", "(grito)", "(silencio)", "¡papá!").
  5. Writes asr_best.txt per song (best backend by letter-only WER).

Usage:
    cd lyricgen/backend
    python scripts/exp_asr_text2.py --backends whisperx,gemini_chunked,whisper1
    python scripts/exp_asr_text2.py --backends gemini_chunked --chunk 24 --overlap 6
    python scripts/exp_asr_text2.py --sweep   # grid over chunk/overlap for gemini
"""
from __future__ import annotations

import argparse
import io
import json
import re
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


# ── text normalization & metrics ──────────────────────────────────────
def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    return " ".join(c for c in s.lower().split())


def _norm_words(s: str) -> str:
    """Normalize for WER: strip punctuation, lowercase, no accents."""
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.lower()
    s = re.sub(r"[^\w\s]", " ", s)
    return " ".join(s.split())


def _wer(ref: str, hyp: str) -> float:
    import jiwer
    r = _norm_words(ref)
    if not r:
        return 0.0
    return jiwer.wer(r, _norm_words(hyp))


# ── ad-lib / label detection (for letter-only WER) ────────────────────
# A Rotor GT line counts as "ad-lib/label" if, after removing parenthetical
# asides and pure-vocalization tokens, almost nothing real remains.
_LABELS = re.compile(r"\((grito|silencio|instrumental|aplausos?|risas?)\)", re.I)
_VOCAL_TOKEN = re.compile(
    r"^(a+h+|o+h+|e+h+|u+h+|u+|ah|oh|eh|uh|na+|la+|ja+|je+|wo+|woo+|yeah|hey)$",
    re.I,
)
_SHOUT = re.compile(r"^[¡!]?\s*(pap[aá]|hijo|no|mam[aá])\s*[!¡]?$", re.I)


def is_adlib_line(text: str) -> bool:
    raw = (text or "").strip()
    if not raw:
        return True
    # pure label
    if _LABELS.search(raw) and not re.sub(_LABELS, "", raw).strip():
        return True
    if raw == "(grito)" or raw == "(silencio)":
        return True
    # shout lines (¡papá!, ¡hijo!, ¡no!)
    if _SHOUT.match(raw):
        return True
    # strip parentheticals, then check what's left
    stripped = re.sub(r"\([^)]*\)", " ", raw)
    stripped = re.sub(r"[¡!¿?,.\-]", " ", stripped)
    toks = [t for t in stripped.lower().split() if t]
    if not toks:
        return True
    real = [t for t in toks if not _VOCAL_TOKEN.match(t)]
    # if >=70% of tokens are vocalizations, treat as ad-lib line
    return len(real) <= max(0, int(0.3 * len(toks))) - (1 if len(toks) <= 2 else 0) or len(real) == 0


def letter_only_ref(gt_lines: list[str]) -> tuple[str, int, int]:
    """Return (joined letter-only ref text, n_letter_lines, n_total)."""
    kept = [ln for ln in gt_lines if not is_adlib_line(ln)]
    return "\n".join(kept), len(kept), len(gt_lines)


# ── coverage ──────────────────────────────────────────────────────────
def _coverage(gt_lines: list[str], hyp_text: str) -> float:
    h = _norm(hyp_text)
    found = 0
    tot = 0
    for ln in gt_lines:
        n = _norm(ln)
        if not n:
            continue
        tot += 1
        m = SequenceMatcher(None, n, h).find_longest_match(0, len(n), 0, len(h))
        if n in h or m.size >= 0.8 * len(n):
            found += 1
    return found / max(1, tot)


# ── whisperX ──────────────────────────────────────────────────────────
def asr_whisperx(audio_path: str, artist: str, song: str, **kw) -> str:
    import whisperx_transcribe as wx
    segs = wx.transcribe_whisperx(audio_path, "es", None) or []
    return "\n".join((s.get("text") or "").strip() for s in segs)


# ── whisper-1 (OpenAI) ────────────────────────────────────────────────
def asr_whisper1(audio_path: str, artist: str, song: str, **kw) -> str:
    import os
    if not os.environ.get("OPENAI_API_KEY"):
        print("    whisper1: OPENAI_API_KEY not set", flush=True)
        return ""
    from openai import OpenAI
    client = OpenAI()
    # whisper-1 caps upload at 25MB; our WAVs exceed it. Re-encode to a
    # 16kHz mono mp3 (plenty for speech) into a temp file.
    import tempfile
    y, _ = librosa.load(audio_path, sr=16000, mono=True)
    tmp = tempfile.NamedTemporaryFile(suffix=".mp3", delete=False)
    tmp.close()
    try:
        sf.write(tmp.name, y, 16000, format="MP3")
    except Exception:
        # soundfile may lack mp3 encoder; fall back to ogg/opus
        sf.write(tmp.name, y, 16000, format="OGG", subtype="OPUS")
    with open(tmp.name, "rb") as f:
        resp = client.audio.transcriptions.create(
            model="whisper-1", file=f, language="es",
            response_format="verbose_json",
            timestamp_granularities=["segment"], temperature=0.0,
        )
    segs = resp.segments or []
    raw = [(s.text or "").strip() for s in segs] if segs else [(resp.text or "").strip()]
    # strip well-known Whisper credit/translation hallucinations
    hall = re.compile(
        r"(amara\.org|subt[ií]tulos|subtitulado|traducido por|"
        r"www\.|\.com|gracias por ver|suscr[ií]b)", re.I)
    return "\n".join(l for l in raw if l and not hall.search(l))


# ── Gemini chunked (hardened seam merge) ──────────────────────────────
_GEMINI_SYS = (
    "Sos un transcriptor experto de letras en español, nivel Rotor.\n"
    "Te doy un FRAGMENTO de audio de ~{dur:.0f}s de una canción{who}.\n"
    "Transcribí EXACTAMENTE lo que se canta en ESTE fragmento, una frase por línea.\n\n"
    "REGLAS DURAS:\n"
    "1. SOLO lo que escuchás en estos segundos. NO completes el estribillo entero, "
    "NO repitas en loop una frase que no se repite en el audio.\n"
    "2. SÍ transcribí las repeticiones que realmente se cantan (coros que repiten).\n"
    "3. NO cortes una frase a la mitad: si una frase empieza en este fragmento pero "
    "se completa después, igual escribila completa como la oís acá.\n"
    "4. Ad-libs/gritos/vocalizaciones: transcribilos como se oyen (ej. 'Oh, oh', 'Ah, ah'). "
    "Si NO hay voz cantada (solo música/silencio/aplausos) NO escribas nada para esa parte.\n"
    "5. NO inventes palabras: si no entendés algo, transcribí lo más parecido fonéticamente, "
    "pero no agregues frases que no están.\n"
    "6. Mayúscula al inicio de cada línea. SIN números, SIN timestamps, SIN comentarios, "
    "SIN 'gracias'/'chau'/nombres del cantante hablando. Solo la letra cantada."
)


def _gemini_chunk_lines(client, genai, clip, sr, dur_chunk, sysp) -> list[str]:
    buf = io.BytesIO()
    sf.write(buf, clip, sr, format="WAV")
    try:
        resp = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=[
                genai.types.Part.from_bytes(data=buf.getvalue(), mime_type="audio/wav"),
                genai.types.Part.from_text(text="Transcribí este fragmento."),
            ],
            config=genai.types.GenerateContentConfig(
                system_instruction=sysp, temperature=0.0, max_output_tokens=700,
                thinking_config=genai.types.ThinkingConfig(thinking_budget=0),
            ),
        )
        return [l.strip(" -•\t") for l in (resp.text or "").splitlines() if l.strip(" -•\t")]
    except Exception as e:
        print(f"    gemini chunk failed: {e}", flush=True)
        return []


def _seam_merge(acc: list[str], new: list[str], tail_window: int = 8) -> list[str]:
    """Splice `new` onto `acc`, dropping lines in `new` that duplicate the
    overlap region already in `acc`.

    Strategy: find the longest k such that the LAST k lines of acc match
    (fuzzy) the FIRST k lines of new. Drop those k from new. If no run
    match, fall back to dropping leading new-lines that fuzzy-match any of
    the last `tail_window` acc lines.
    """
    if not acc:
        return list(new)
    if not new:
        return acc

    an = [_norm(x) for x in acc]
    nn = [_norm(x) for x in new]

    def sim(a: str, b: str) -> float:
        if not a or not b:
            return 0.0
        r = SequenceMatcher(None, a, b).ratio()
        # treat a partial line as matching its fuller version (chunk split)
        if a.startswith(b) or b.startswith(a) or a.endswith(b) or b.endswith(a):
            r = max(r, 0.8)
        return r

    # try longest aligned run between tail of acc and head of new
    best_k = 0
    max_k = min(len(acc), len(new), tail_window)
    for k in range(max_k, 0, -1):
        tail = an[-k:]
        head = nn[:k]
        scores = [sim(t, h) for t, h in zip(tail, head)]
        if scores and sum(s >= 0.72 for s in scores) >= max(1, int(0.7 * k)) \
                and (sum(scores) / k) >= 0.7:
            best_k = k
            break
    if best_k:
        return acc + new[best_k:]

    # fallback: per-line, drop leading new lines matching recent tail
    out = list(acc)
    tail_set = an[-tail_window:]
    i = 0
    while i < len(new):
        if any(sim(nn[i], t) > 0.85 for t in tail_set):
            i += 1
            continue
        break
    # also dedup consecutive exact repeats at the join
    rest = new[i:]
    if rest and sim(nn[i] if i < len(nn) else "", an[-1]) > 0.9:
        rest = rest[1:]
    return out + rest


def _absorb_fragments(lines: list[str]) -> list[str]:
    """Merge a short fragment line into an adjacent line when it is a
    prefix/suffix of it (chunk-boundary split, e.g. 'Lo dejaste' +
    'Lo dejaste pasar', or 'Entre el' + 'Entre el juego y el azar').

    A line is a "fragment" if it has <=3 words. We drop it when an
    immediate neighbour (prev or next) starts-with or contains it as a
    normalized prefix.
    """
    norm = [_norm(x) for x in lines]
    drop = [False] * len(lines)
    for i, n in enumerate(norm):
        if drop[i] or not n:
            continue
        if len(n.split()) > 3:
            continue
        for j in (i - 1, i + 1):
            if 0 <= j < len(norm) and not drop[j] and norm[j] and norm[j] != n:
                nj = norm[j]
                # fragment is prefix or suffix of the fuller neighbour
                if len(nj) > len(n) and (nj.startswith(n) or nj.endswith(n)):
                    drop[i] = True
                    break
    return [ln for i, ln in enumerate(lines) if not drop[i]]


def _post_clean(lines: list[str]) -> list[str]:
    """Drop obvious chatter, collapse immediate exact dups, absorb
    chunk-boundary fragments."""
    chatter = re.compile(
        r"^(gracias|chau|chao|adi[oó]s|buenas noches|muchas gracias|"
        r"\(instrumental\)|\(silencio\)|\(aplausos?\)|che)\b",
        re.I,
    )
    out: list[str] = []
    prev = ""
    for ln in lines:
        s = ln.strip()
        if not s:
            continue
        if chatter.match(s) and len(s.split()) <= 3:
            continue
        if _norm(s) == prev:
            continue
        out.append(s)
        prev = _norm(s)
    return _absorb_fragments(out)


def asr_gemini_chunked(audio_path: str, artist: str, song: str,
                       chunk_s: float = 28.0, overlap_s: float = 4.0,
                       **kw) -> str:
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
        sysp = _GEMINI_SYS.format(dur=(c1 - c0), who=who)
        lines = _gemini_chunk_lines(client, genai, clip, sr, c1 - c0, sysp)
        acc = _seam_merge(acc, lines)
        t += chunk_s - overlap_s
    return "\n".join(_post_clean(acc))


BACKENDS = {
    "whisperx": asr_whisperx,
    "whisper1": asr_whisper1,
    "gemini_chunked": asr_gemini_chunked,
}


def evaluate(gt_lines: list[str], txt: str) -> dict:
    gt_text = "\n".join(gt_lines)
    let_ref, n_let, n_tot = letter_only_ref(gt_lines)
    return {
        "wer": _wer(gt_text, txt),
        "wer_letter": _wer(let_ref, txt),
        "cov": _coverage(gt_lines, txt),
        "n_lines": len([l for l in txt.splitlines() if l.strip()]),
        "n_gt": n_tot,
        "n_let": n_let,
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default="")
    ap.add_argument("--backends", default="whisperx,gemini_chunked,whisper1")
    ap.add_argument("--chunk", type=float, default=28.0)
    ap.add_argument("--overlap", type=float, default=4.0)
    ap.add_argument("--sweep", action="store_true",
                    help="grid over (chunk,overlap) for gemini_chunked only")
    ap.add_argument("--write-best", action="store_true",
                    help="write asr_best.txt (best by letter WER)")
    args = ap.parse_args()

    dirs = sorted(p for p in DATASET.iterdir() if p.is_dir())
    if args.only:
        dirs = [d for d in dirs if d.name == args.only]

    if args.sweep:
        grid = [(28, 4), (24, 6), (20, 5), (32, 6), (20, 8), (24, 4)]
        print(f"{'song':22} {'chunk/ov':>9} {'WER':>6} {'WERlet':>7} {'cov':>5} {'lines':>9}")
        for d in dirs:
            gt = json.loads((d / "ground_truth.json").read_text())
            gt_lines = [(s.get("text") or "").strip() for s in gt]
            meta = json.loads((d / "metadata.json").read_text())
            audio = next(p for p in d.glob("audio.*") if not p.name.endswith(".json"))
            for ch, ov in grid:
                txt = asr_gemini_chunked(str(audio), meta.get("artist", ""),
                                         meta.get("song_title", ""),
                                         chunk_s=ch, overlap_s=ov)
                m = evaluate(gt_lines, txt)
                print(f"{d.name[:22]:22} {ch:4.0f}/{ov:<4.0f} {m['wer']:6.3f} "
                      f"{m['wer_letter']:7.3f} {m['cov']:4.0%} "
                      f"{m['n_lines']:>4}/{m['n_gt']:<4}", flush=True)
        return

    want = [b.strip() for b in args.backends.split(",") if b.strip()]
    rows = []
    for d in dirs:
        gt = json.loads((d / "ground_truth.json").read_text())
        gt_lines = [(s.get("text") or "").strip() for s in gt]
        meta = json.loads((d / "metadata.json").read_text()) if (d / "metadata.json").exists() else {}
        audio = next((p for p in d.glob("audio.*") if not p.name.endswith(".json")), None)
        if not audio:
            continue
        best = None  # (wer_letter, backend, txt)
        for b in want:
            print(f"[{d.name}] {b} ...", flush=True)
            t0 = time.time()
            try:
                txt = BACKENDS[b](str(audio), meta.get("artist", ""),
                                  meta.get("song_title", ""),
                                  chunk_s=args.chunk, overlap_s=args.overlap)
            except Exception as e:
                print(f"  x {b} failed: {type(e).__name__}: {e}")
                continue
            dt = time.time() - t0
            m = evaluate(gt_lines, txt)
            rows.append((d.name, b, m, dt))
            (d / f"asr_{b}.txt").write_text(txt)
            print(f"  ok {b}: WER={m['wer']:.3f} WERlet={m['wer_letter']:.3f} "
                  f"cov={m['cov']:.0%} lines={m['n_lines']}/{m['n_gt']} ({dt:.0f}s)")
            if best is None or m["wer_letter"] < best[0]:
                best = (m["wer_letter"], b, txt)
        if args.write_best and best is not None:
            (d / "asr_best.txt").write_text(best[2])
            print(f"  >> asr_best.txt <- {best[1]} (WERlet={best[0]:.3f})")

    print("\n=== ASR vs Rotor ===")
    print(f"{'song':22} {'backend':15} {'WER':>6} {'WERlet':>7} {'cov':>5} {'lines':>11} {'sec':>5}")
    for song, b, m, dt in rows:
        print(f"{song[:22]:22} {b:15} {m['wer']:6.3f} {m['wer_letter']:7.3f} "
              f"{m['cov']:4.0%} {m['n_lines']:>4}/{m['n_gt']:<4}(let{m['n_let']}) {dt:5.0f}")


if __name__ == "__main__":
    main()
