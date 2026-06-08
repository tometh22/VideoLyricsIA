#!/usr/bin/env python3
"""Gemini-2.5 chunked as the PRIMARY transcription pipeline (text + timing).

Goal: Rotor-grade COMPLETE coverage on dense live audio (repeated choruses,
ad-libs, shouts, long outro) WITHOUT lrclib/toggle, while also producing
per-line TIMING good enough to karaoke.

Design:
  - FORCED FULL COVERAGE: tile the whole audio 0..dur with NO gaps so we
    never skip a chorus/outro (whisper-1 drops the first ~25s chorus).
  - Per-window Gemini call returns each sung line WITH a relative timestamp
    [mm:ss.s] inside the clip. We map rel->absolute (ws + rel).
  - Optional label capture: (grito)/(silencio)/(instrumental) in voiceless
    stretches, Rotor-style.
  - Seam de-dup across the overlap by time-proximity + text similarity.
  - Snap each start to the vocal-stem onset (forced_align.snap_starts_to_
    vocal_onsets) to remove Gemini's coarse timestamp jitter.

Scoring is done IN PROCESS via score_benchmark._wer / _aoo and a coverage
metric. We never touch improvement_output.json (other agents run parallel).

Usage:
    python scripts/exp_gemini_primary.py --slug rotor_nada_fue \
        --model gemini-2.5-flash --chunk 30 --overlap 6 --variant timestamp
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
from statistics import mean, median

HERE = Path(__file__).resolve().parent
BACKEND = HERE.parent
sys.path.insert(0, str(BACKEND))
sys.path.insert(0, str(HERE))

import numpy as np  # noqa: E402
import librosa  # noqa: E402
import soundfile as sf  # noqa: E402

DATASET = BACKEND / "benchmark" / "dataset"


# ── text norm ──────────────────────────────────────────────────────────
def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    return " ".join(c for c in s.lower().split())


def _norm_words(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.lower()
    s = re.sub(r"[^\w\s]", " ", s)
    return " ".join(s.split())


# ── coverage: % of GT lines whose text appears in the hypothesis ─────────
def _coverage(gt_lines: list[str], hyp_text: str) -> float:
    h = _norm(hyp_text)
    found = tot = 0
    for ln in gt_lines:
        n = _norm(ln)
        if not n:
            continue
        tot += 1
        m = SequenceMatcher(None, n, h).find_longest_match(0, len(n), 0, len(h))
        if n in h or m.size >= 0.8 * len(n):
            found += 1
    return found / max(1, tot)


# ── Gemini prompt: transcribe + RELATIVE timestamps per line ────────────
_SYS_TS = (
    "Sos un transcriptor experto de letras en español, nivel Rotor.\n"
    "Te doy un FRAGMENTO de audio de ~{dur:.0f}s de una cancion{who} EN VIVO.\n"
    "Transcribi EXACTAMENTE lo que se canta en ESTE fragmento, una frase por linea,\n"
    "y al inicio de cada linea pone el timestamp RELATIVO (desde el comienzo de ESTE\n"
    "fragmento, no de la cancion) en formato [mm:ss.s].\n\n"
    "FORMATO EXACTO de cada linea: [mm:ss.s] texto de la frase\n"
    "Ejemplo:\n"
    "[00:01.2] Tengo una mala noticia\n"
    "[00:03.0] No fue de casualidad\n\n"
    "REGLAS DURAS:\n"
    "1. SOLO lo que escuchas en estos segundos. NO completes el estribillo entero,\n"
    "   NO repitas en loop una frase que no se repite en el audio.\n"
    "2. SI transcribi TODAS las repeticiones que realmente se cantan (coros que repiten).\n"
    "3. NO cortes una frase a la mitad: si empieza aca y se completa despues,\n"
    "   escribila completa como la ois aca, con el timestamp de cuando EMPIEZA.\n"
    "4. Ad-libs/gritos/vocalizaciones: transcribilos como se oyen (ej. 'Oh, oh', 'Ah, ah',\n"
    "   '¡papa!', '¡no!'). Si hay un GRITO largo sin letra escribi '(grito)'. Si hay\n"
    "   solo musica/aplausos sin voz NO escribas nada. Si hay silencio total escribi '(silencio)'.\n"
    "5. NO inventes palabras: si no entendes algo, transcribi lo mas parecido foneticamente.\n"
    "6. Cada linea DEBE empezar con su [mm:ss.s]. SIN numeracion, SIN comentarios extra."
)

# variant 'plain' (no timestamps): reuse the v2 prompt style
_SYS_PLAIN = (
    "Sos un transcriptor experto de letras en español, nivel Rotor.\n"
    "Te doy un FRAGMENTO de audio de ~{dur:.0f}s de una cancion{who} EN VIVO.\n"
    "Transcribi EXACTAMENTE lo que se canta en ESTE fragmento, una frase por linea.\n\n"
    "REGLAS: 1) SOLO lo de estos segundos, sin loops inventados. 2) SI todas las\n"
    "repeticiones reales. 3) frases completas. 4) ad-libs/gritos como se oyen\n"
    "('Oh, oh','¡papa!','(grito)'), nada si solo hay musica. 5) sin inventar.\n"
    "6) una frase por linea, sin numeros ni timestamps."
)

_TS_RE = re.compile(r"^\s*[\[(]?\s*(\d{1,2}):(\d{2}(?:\.\d{1,2})?)\s*[\])]?\s*(.*)$")


_JUNK = re.compile(r"^[\[\]()0-9:.\s¡!¿?]*$")


def _parse_ts_line(line: str):
    """Return (rel_seconds | None, text). Strips a leading [mm:ss.s] and any
    trailing bracket noise. Returns text='' for junk-only lines (stray '[',
    '[00:', etc. emitted when Gemini wraps a timestamp onto its own line)."""
    raw = line.strip(" -•\t")
    m = _TS_RE.match(raw)
    if m:
        rel = int(m.group(1)) * 60 + float(m.group(2))
        text = m.group(3).strip(" -•\t[]()")
    else:
        rel, text = None, raw.strip(" -•\t[]()")
    if _JUNK.match(text) or not text:
        return rel, ""
    return rel, text


def _gemini_window(client, genai, clip, sr, dur, sysp, model, max_tokens=900):
    buf = io.BytesIO()
    sf.write(buf, clip, sr, format="WAV")
    try:
        resp = client.models.generate_content(
            model=model,
            contents=[
                genai.types.Part.from_bytes(data=buf.getvalue(), mime_type="audio/wav"),
                genai.types.Part.from_text(text="Transcribi este fragmento."),
            ],
            config=genai.types.GenerateContentConfig(
                system_instruction=sysp, temperature=0.0, max_output_tokens=max_tokens,
                thinking_config=genai.types.ThinkingConfig(thinking_budget=0),
            ),
        )
        return [l for l in (resp.text or "").splitlines() if l.strip()]
    except Exception as e:
        print(f"    gemini window failed: {e}", flush=True)
        return []


# ── seam dedup over time+text ───────────────────────────────────────────
def _sim(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    r = SequenceMatcher(None, a, b).ratio()
    if a.startswith(b) or b.startswith(a) or a.endswith(b) or b.endswith(a):
        r = max(r, 0.8)
    return r


def _contained(a: str, b: str) -> bool:
    """True if normalized a is a contiguous substring of b (fragment vs
    fuller line, e.g. 'lo dejaste pasar' in 'lo dejaste pasar no quiero...')."""
    if not a or not b or a == b:
        return False
    return a in b or b in a


def _dedup_segments(segs: list[dict], time_win: float = 4.0) -> list[dict]:
    """Drop a segment that duplicates an earlier one within `time_win`
    seconds (overlap/window-split artifact). Two notions of duplicate:
      - high whole-line similarity (>=0.80), OR
      - one normalized text is contained in the other (fragment split).
    When a fragment and its fuller version collide, keep the FULLER one and
    its (usually earlier/better) timestamp."""
    segs = sorted(segs, key=lambda s: (s["start"], -len(s["text"])))
    out: list[dict] = []
    for s in segs:
        sn = _norm(s["text"])
        merged = False
        for o in out[::-1]:
            if s["start"] - o["start"] > time_win:
                break
            on = _norm(o["text"])
            if _sim(sn, on) >= 0.80 or _contained(sn, on):
                # keep whichever is longer (more complete), earliest start
                if len(sn) > len(on):
                    o["text"] = s["text"]
                    o["start"] = min(o["start"], s["start"])
                merged = True
                break
        if not merged:
            out.append(s)
    out.sort(key=lambda s: s["start"])
    return out


_CHATTER = re.compile(
    r"^(gracias|chau|chao|adi[oó]s|buenas noches|muchas gracias|che)\b", re.I)

_PURE_VOCAL = re.compile(
    r"^[¡!\s]*((o+h+|a+h+|e+h+|u+h+|na+|la+|ja+|je+|wo+|yeah|hey|uh)[\s,.!¡]*)+$",
    re.I)


def _is_pure_vocal(text: str) -> bool:
    return bool(_PURE_VOCAL.match(text.strip()))


def _strip_lead_adlibs(segs: list[dict]) -> list[dict]:
    """Drop leading pure-vocalization ad-lib lines (Oh/Ah/etc.) that occur
    BEFORE the first lexically-rich sung line. On live recordings Gemini
    transcribes intro crowd noise as 'Oh, oh' — Rotor's GT (and the song)
    starts at the first real lyric. Removing these phantom intro lines stops
    them pairing with a far-away real 'Oh, oh' (→ 60-90 s AOO outliers)."""
    first_real = None
    for i, s in enumerate(segs):
        if not _is_pure_vocal(s["text"]) and not _is_label(s["text"]):
            first_real = i
            break
    if first_real is None or first_real == 0:
        return segs
    return segs[first_real:]


def _is_label(text: str) -> bool:
    return bool(re.match(r"^\(?(grito|silencio|instrumental|aplausos?)\)?$", text.strip(), re.I))


# ── main transcription: forced full coverage ────────────────────────────
def transcribe(audio_path: str, artist: str, song: str, *, model: str,
               chunk_s: float, overlap_s: float, variant: str,
               keep_labels: bool, vocal_regions=None, cache_dir=None) -> list[dict]:
    import pipeline
    from google import genai
    client = pipeline._get_genai_client()
    if client is None:
        raise RuntimeError("no genai client")
    sr = 22050
    y, _ = librosa.load(audio_path, sr=sr, mono=True)
    dur = len(y) / sr
    who = f" ({artist} — {song})" if artist else ""
    use_ts = variant == "timestamp"
    sys_tmpl = _SYS_TS if use_ts else _SYS_PLAIN

    cache = None
    if cache_dir:
        cache = Path(cache_dir)
        cache.mkdir(parents=True, exist_ok=True)

    raw_segs: list[dict] = []
    t = 0.0
    step = chunk_s - overlap_s
    while t < dur - 0.5:
        c0, c1 = t, min(dur, t + chunk_s)
        clip = y[int(c0 * sr):int(c1 * sr)]
        sysp = sys_tmpl.format(dur=(c1 - c0), who=who)
        ckey = f"{model}_{variant}_{chunk_s:.0f}_{overlap_s:.0f}_{c0:.1f}_{c1:.1f}.json"
        cf = cache / ckey if cache else None
        if cf and cf.exists():
            lines = json.loads(cf.read_text())
        else:
            lines = _gemini_window(client, genai, clip, sr, c1 - c0, sysp, model)
            if cf:
                cf.write_text(json.dumps(lines, ensure_ascii=False))
        n = len([l for l in lines if l.strip()])
        for i, line in enumerate(lines):
            rel, text = _parse_ts_line(line)
            if not text:
                continue
            if _CHATTER.match(text) and len(text.split()) <= 3:
                continue
            if _is_label(text) and not keep_labels:
                continue
            if use_ts and rel is not None:
                abs_start = c0 + min(rel, c1 - c0)
            else:
                # uniform distribution within the window's span
                abs_start = c0 + (i + 0.5) / max(1, n) * (c1 - c0)
            raw_segs.append({"start": round(abs_start, 3),
                             "end": round(abs_start + 2.0, 3),
                             "text": text})
        t += step

    segs = _dedup_segments(raw_segs)
    segs = _strip_lead_adlibs(segs)
    # set ends to next start (non-overlapping), cap tail
    segs.sort(key=lambda s: s["start"])
    for i in range(len(segs) - 1):
        segs[i]["end"] = round(min(segs[i]["start"] + 6.0,
                                   max(segs[i]["start"] + 0.5, segs[i + 1]["start"] - 0.05)), 3)
    if segs:
        segs[-1]["end"] = round(min(dur, segs[-1]["start"] + 4.0), 3)

    # onset snap
    if vocal_regions:
        import forced_align
        segs = forced_align.snap_starts_to_vocal_onsets(segs, vocal_regions, max_snap_s=2.0)
    return segs


# ── scoring (in process, no file writes to scored bundles) ──────────────
def score(slug: str, segs: list[dict]) -> dict:
    from score_benchmark import _wer, _aoo
    gt = json.loads((DATASET / slug / "ground_truth.json").read_text())
    gt_lines = [(s.get("text") or "").strip() for s in gt]
    wer = _wer(gt, segs)
    aoo_mean, aoo_med, aoo_p95, matched, aoo_p95_deoff = _aoo(gt, segs)
    cov = _coverage(gt_lines, "\n".join(s["text"] for s in segs))
    return {
        "wer": wer, "aoo_mean": aoo_mean, "aoo_median": aoo_med,
        "aoo_p95": aoo_p95, "aoo_p95_deoff": aoo_p95_deoff,
        "matched": matched, "coverage": cov,
        "n_segs": len(segs), "n_gt": len(gt_lines),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--slug", required=True)
    ap.add_argument("--model", default="gemini-2.5-flash")
    ap.add_argument("--chunk", type=float, default=30.0)
    ap.add_argument("--overlap", type=float, default=6.0)
    ap.add_argument("--variant", default="timestamp", choices=["timestamp", "plain"])
    ap.add_argument("--keep-labels", action="store_true")
    ap.add_argument("--no-snap", action="store_true")
    ap.add_argument("--cache", default="")
    ap.add_argument("--save", default="")
    args = ap.parse_args()

    d = DATASET / args.slug
    meta = json.loads((d / "metadata.json").read_text())
    audio = str(d / "audio.wav")
    stem = d / "vocals.wav"

    import anchor_align
    vr = anchor_align.vocal_regions(str(stem)) if stem.exists() and not args.no_snap else None

    t0 = time.time()
    segs = transcribe(audio, meta.get("artist", ""), meta.get("song_title", ""),
                      model=args.model, chunk_s=args.chunk, overlap_s=args.overlap,
                      variant=args.variant, keep_labels=args.keep_labels,
                      vocal_regions=vr, cache_dir=(args.cache or None))
    dt = time.time() - t0
    m = score(args.slug, segs)
    print(f"\n=== {args.slug} | {args.model} | {args.variant} | "
          f"chunk={args.chunk} ov={args.overlap} labels={args.keep_labels} "
          f"snap={not args.no_snap} ===")
    print(f"  WER={m['wer']:.3f}  cov={m['coverage']:.0%}  "
          f"AOO med={m['aoo_median']:.2f} mean={m['aoo_mean']:.2f} "
          f"p95={m['aoo_p95']:.2f} deoff_p95={m['aoo_p95_deoff']:.2f}  "
          f"matched={m['matched']}  segs={m['n_segs']}/{m['n_gt']}  ({dt:.0f}s)")
    if args.save:
        out = {"source": f"gemini_primary:{args.model}:{args.variant}",
               "meta": {"model": args.model, "variant": args.variant,
                        "chunk_s": args.chunk, "overlap_s": args.overlap,
                        "keep_labels": args.keep_labels, "snap": not args.no_snap,
                        "metrics": m},
               "segments": segs}
        Path(args.save).write_text(json.dumps(out, ensure_ascii=False, indent=2))
        print(f"  saved -> {args.save}")


if __name__ == "__main__":
    main()
