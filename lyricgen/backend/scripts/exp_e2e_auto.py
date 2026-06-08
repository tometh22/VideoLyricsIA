#!/usr/bin/env python3
"""End-to-end AUTOMATIC pipeline measurement (no ground-truth text) vs Rotor.

Combines the two phase winners:
  - ASR winner:  whisper-1 (OpenAI), verbose_json with WORD + SEGMENT stamps.
  - Alignment:   forced_align.forced_align_lyrics_chunked + snap_starts_to_vocal_onsets
                 (and, for comparison, whisper-1's NATIVE word stamps re-segmented).

Pipelines measured (all fully automatic — text comes from ASR, never from GT):

  A  whisper-1 text  -> LLM re-segment (Rotor-style lines, audio-grounded)
                     -> forced_align_lyrics_chunked (cureau) + snap   [cureau path]
  B  whisper-1 WORD stamps -> LLM re-segment (re-times from native words) + snap
                                                                       [no cureau]
  Eg (vivo ensemble) whisper-1 skeleton + gemini_chunked text reconciled
                     -> forced_align_lyrics_chunked + snap

Each result is scored with scripts/score_benchmark.py's metric (WER + AOO
median/mean/p95, repeat-aware). The best per-theme result is written to that
theme's improvement_output.json.

Usage:
    python scripts/exp_e2e_auto.py --only rotor_donde_estan --pipelines A,B
    python scripts/exp_e2e_auto.py            # both themes, A+B (+ensemble on vivo)
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
import time
import unicodedata
from pathlib import Path
from statistics import mean, median

HERE = Path(__file__).resolve().parent
BACKEND = HERE.parent
sys.path.insert(0, str(BACKEND))
DATASET = BACKEND / "benchmark" / "dataset"

import librosa  # noqa: E402
import soundfile as sf  # noqa: E402

import forced_align  # noqa: E402
import anchor_align  # noqa: E402
import pipeline  # noqa: E402


# ── scoring (mirror of scripts/score_benchmark.py, repeat-aware) ──────────
def _norm_words(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.lower()
    s = re.sub(r"[^\w\s]", " ", s)
    return " ".join(s.split())


def wer(ref_segs, hyp_segs) -> float:
    import jiwer
    ref = _norm_words(" ".join((s.get("text") or "") for s in ref_segs))
    hyp = _norm_words(" ".join((s.get("text") or "") for s in hyp_segs))
    if not ref:
        return 0.0
    return jiwer.wer(ref, hyp)


def _jaccard(a: str, b: str) -> float:
    sa, sb = set((a or "").lower().split()), set((b or "").lower().split())
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


def aoo(ground, output):
    """Returns (mean, median, p95, matched, p95_deoffset). Same logic as
    score_benchmark.py: repeat-aware nearest-in-time matching."""
    abs_off, signed = [], []
    for out_seg in output:
        out_text = (out_seg.get("text") or "").strip()
        if not out_text:
            continue
        try:
            out_start = float(out_seg["start"])
        except (KeyError, TypeError, ValueError):
            continue
        cands = [(_jaccard(out_text, gt.get("text") or ""), gt) for gt in ground]
        strong = [(sc, gt) for sc, gt in cands if sc >= 0.6]
        if strong:
            bm = min(strong, key=lambda t: abs(out_start - float(t[1]["start"])))[1]
        else:
            sc, gt = max(cands, key=lambda t: t[0]) if cands else (0.0, None)
            bm = gt if sc >= 0.4 else None
        if bm is None:
            continue
        d = out_start - float(bm["start"])
        abs_off.append(abs(d))
        signed.append(d)
    if not abs_off:
        return (0.0, 0.0, 0.0, 0, 0.0)

    def _p95(xs):
        s = sorted(xs)
        return s[max(0, int(len(s) * 0.95) - 1)]
    med_signed = median(signed)
    deoff = [abs(s - med_signed) for s in signed]
    return (mean(abs_off), median(abs_off), _p95(abs_off), len(abs_off), _p95(deoff))


def score(ground, segs) -> dict:
    w = wer(ground, segs)
    a_mean, a_med, a_p95, matched, a_p95d = aoo(ground, segs)
    return {"wer": w, "aoo_mean": a_mean, "aoo_median": a_med, "aoo_p95": a_p95,
            "aoo_p95_deoff": a_p95d, "matched": matched, "n": len(segs)}


def fmt(s: dict) -> str:
    return (f"WER={s['wer']:.3f} AOO med={s['aoo_median']:.2f} mean={s['aoo_mean']:.2f} "
            f"p95={s['aoo_p95']:.2f} deoff95={s['aoo_p95_deoff']:.2f} "
            f"matched={s['matched']}/{s['n']}")


# ── whisper-1 with WORD + SEGMENT stamps (cached) ─────────────────────────
def whisper1_verbose(audio_path: str, cache: Path) -> dict:
    """Return {'text':..., 'words':[{word,start,end}], 'segments':[...]}.
    Caches the parsed response so re-runs are free."""
    if cache.exists():
        return json.loads(cache.read_text())
    from openai import OpenAI
    client = OpenAI()
    y, _ = librosa.load(audio_path, sr=16000, mono=True)
    tmp = tempfile.NamedTemporaryFile(suffix=".mp3", delete=False)
    tmp.close()
    try:
        sf.write(tmp.name, y, 16000, format="MP3")
    except Exception:
        sf.write(tmp.name, y, 16000, format="OGG", subtype="OPUS")
    with open(tmp.name, "rb") as f:
        resp = client.audio.transcriptions.create(
            model="whisper-1", file=f, language="es",
            response_format="verbose_json",
            timestamp_granularities=["word", "segment"], temperature=0.0,
        )
    os.unlink(tmp.name)
    hall = re.compile(r"(amara\.org|subt[ií]tulos|subtitulado|traducido por|"
                      r"www\.|\.com|gracias por ver|suscr[ií]b|marie arias)", re.I)
    # time spans of hallucinated segments — drop any words inside them, so the
    # credit/translation hallucinations don't leak into the word stream that
    # feeds the LLM segmenter / forced aligner.
    hall_spans = []
    segs = []
    for s in (resp.segments or []):
        t = (s.text or "").strip()
        if t and hall.search(t):
            hall_spans.append((float(s.start), float(s.end)))
        elif t:
            segs.append({"start": float(s.start), "end": float(s.end), "text": t})

    def _in_hall(st):
        return any(a - 0.2 <= st <= b + 0.2 for a, b in hall_spans)
    words = []
    for w in (resp.words or []):
        wd = (getattr(w, "word", "") or "").strip()
        st = float(w.start)
        if wd and not _in_hall(st):
            words.append({"word": wd, "start": st, "end": float(w.end)})
    clean_text = " ".join(s["text"] for s in segs).strip()
    out = {"text": clean_text, "words": words, "segments": segs}
    cache.write_text(json.dumps(out, ensure_ascii=False))
    return out


# ── plain-text line splitter (Rotor-style ~4-9 word phrases) ──────────────
def split_lines(text: str) -> list[str]:
    """Split coarse whisper text into Rotor-style phrase lines on sentence /
    clause punctuation, then by word budget. Text-only (no GT)."""
    text = re.sub(r"\s+", " ", (text or "").strip())
    # break before ¿ ¡ and after . ? ! ; and at commas; keep openers attached
    parts = re.split(r"(?<=[.?!;])\s+|\s*(?=[¿¡])|,\s*", text)
    out = []
    for p in parts:
        p = p.strip(" ,")
        if not p:
            continue
        words = p.split()
        # further split overlong clauses into ~7-word chunks
        if len(words) > 11:
            for k in range(0, len(words), 7):
                seg = " ".join(words[k:k + 7]).strip()
                if seg:
                    out.append(seg[0].upper() + seg[1:])
        else:
            out.append(p[0].upper() + p[1:])
    return out


# ── Pipeline B: whisper-1 native words -> LLM re-segment + snap ────────────
def pipeline_B(audio_path, w1, artist, song, vregions):
    """Use whisper-1 word stamps directly. Re-segment into Rotor lines with
    the audio-grounded LLM segmenter (re-times each line from its native
    whisper words), then snap line starts to vocal onsets. No cureau."""
    if not w1["words"]:
        return None
    one_seg = [{"start": w1["words"][0]["start"],
                "end": w1["words"][-1]["end"],
                "text": w1["text"], "words": w1["words"]}]
    segs = pipeline._llm_segment_words(one_seg, audio_path=audio_path,
                                       artist=artist, song=song)
    if not segs or segs is one_seg:
        # LLM segmenter declined — fall back to punctuation splitter mapped
        # back onto the native words by sequential consumption.
        lines = split_lines(w1["text"])
        segs = forced_align.wordstamps_to_segments(w1["words"], lines)
    if vregions:
        segs = forced_align.snap_starts_to_vocal_onsets(segs, vregions, max_snap_s=2.0)
    return segs


# ── Pipeline A: whisper-1 text -> lines -> chunked cureau align + snap ─────
def pipeline_A(audio_path, lines, vregions):
    text = "\n".join(lines)
    segs = forced_align.forced_align_lyrics_chunked(
        audio_path, text, vocal_regions=vregions)
    return segs  # chunked already snaps internally when vregions present


# ── vivo ensemble: whisper-1 skeleton + gemini_chunked recovery ───────────
def _norm(s):
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    return " ".join(re.sub(r"[^\w\s]", " ", s.lower()).split())


def ensemble_text(w1_lines, gem_lines):
    """Reconcile whisper-1 (precise skeleton) with gemini_chunked (recovers
    repetitions/outro). Strategy: keep the whisper-1 line sequence as the
    backbone; append any TRAILING gemini lines whose normalized text is not
    already covered by the whisper backbone tail (outro repetitions whisper
    cut off). Conservative — only adds, never reorders."""
    backbone = list(w1_lines)
    bset = {_norm(l) for l in backbone}
    # find gemini lines after the last shared anchor that whisper lacks
    add = []
    for gl in gem_lines:
        n = _norm(gl)
        if not n:
            continue
        if n not in bset:
            add.append(gl)
    # only append outro-style additions (repetitions of existing choruses)
    tail_add = [a for a in add if any(_norm(a) and _norm(a) in _norm(b) or
                                      _norm(b) in _norm(a) for b in backbone)]
    return backbone + tail_add


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default="")
    ap.add_argument("--pipelines", default="A,B")
    ap.add_argument("--write-best", action="store_true", default=True)
    args = ap.parse_args()

    dirs = sorted(p for p in DATASET.iterdir() if p.is_dir())
    if args.only:
        dirs = [d for d in dirs if d.name == args.only]
    want = [p.strip() for p in args.pipelines.split(",") if p.strip()]

    UPPER = {"rotor_donde_estan": 0.11, "rotor_nada_fue": 0.47}
    BASELINE_MED = 1.8

    for d in dirs:
        gt = json.loads((d / "ground_truth.json").read_text())
        meta = json.loads((d / "metadata.json").read_text()) if (d / "metadata.json").exists() else {}
        artist, song = meta.get("artist", ""), meta.get("song_title", "")
        audio = str(d / "audio.wav")
        stem = d / "vocals.wav"
        vregions = anchor_align.vocal_regions(str(stem)) if stem.exists() else []
        print(f"\n##### {d.name}  ({len(gt)} GT lines, vocal_regions={len(vregions)})")

        w1 = whisper1_verbose(audio, d / "_w1_verbose.json")
        print(f"  whisper-1: {len(w1['words'])} words, {len(w1['segments'])} segs")
        w1_lines = split_lines(w1["text"])
        print(f"  split_lines -> {len(w1_lines)} lines")

        results = {}  # name -> (segs, score, source_label)

        if "B" in want:
            t0 = time.time()
            segsB = pipeline_B(audio, w1, artist, song, vregions)
            if segsB:
                s = score(gt, segsB)
                results["B"] = (segsB, s, "e2e_auto_whisper1_words_llmseg_snap")
                print(f"  [B native-words] {fmt(s)} ({time.time()-t0:.0f}s)")

        if "C" in want:
            # C: B's clean LLM-segmented line TEXTS -> chunked cureau align.
            # (cureau onsets on clean Rotor-style text, vs B's native-word onsets)
            t0 = time.time()
            if "B" in results:
                c_lines = [s["text"] for s in results["B"][0] if (s.get("text") or "").strip()]
            else:
                bsegs = pipeline_B(audio, w1, artist, song, vregions) or []
                c_lines = [s["text"] for s in bsegs if (s.get("text") or "").strip()]
            segsC = pipeline_A(audio, c_lines, vregions)
            if segsC:
                s = score(gt, segsC)
                results["C"] = (segsC, s, "e2e_auto_whisper1_llmseg_text_chunked_align_snap")
                print(f"  [C llmseg-text->cureau] {fmt(s)} ({time.time()-t0:.0f}s)")
            else:
                print("  [C llmseg-text->cureau] returned None")

        if "A" in want:
            t0 = time.time()
            segsA = pipeline_A(audio, w1_lines, vregions)
            if segsA:
                s = score(gt, segsA)
                results["A"] = (segsA, s, "e2e_auto_whisper1_chunked_align_snap")
                print(f"  [A chunked-cureau] {fmt(s)} ({time.time()-t0:.0f}s)")
            else:
                print("  [A chunked-cureau] returned None")

        if "E" in want and (d / "asr_gemini_chunked.txt").exists():
            t0 = time.time()
            gem_lines = [l.strip() for l in (d / "asr_gemini_chunked.txt").read_text().splitlines() if l.strip()]
            ens_lines = ensemble_text(w1_lines, gem_lines)
            print(f"  ensemble text -> {len(ens_lines)} lines (w1 {len(w1_lines)} + gem add {len(ens_lines)-len(w1_lines)})")
            segsE = pipeline_A(audio, ens_lines, vregions)
            if segsE:
                s = score(gt, segsE)
                results["E"] = (segsE, s, "e2e_auto_ensemble_w1_gemini_chunked_snap")
                print(f"  [E ensemble] {fmt(s)} ({time.time()-t0:.0f}s)")

        if not results:
            print("  !! no pipeline produced output")
            continue

        # best = lowest AOO median, tie-break by WER
        best_name = min(results, key=lambda k: (results[k][1]["aoo_median"],
                                                results[k][1]["wer"]))
        segs, s, src = results[best_name]
        print(f"  ==> BEST: pipeline {best_name}  {fmt(s)}")
        print(f"      vs baseline median {BASELINE_MED}s | upper-bound (correct text) "
              f"{UPPER.get(d.name, '?')}s")

        if args.write_best:
            bundle = {"segments": segs, "source": src,
                      "meta": {"pipeline": best_name, "artist": artist, "song": song,
                               "score": s}}
            (d / "improvement_output.json").write_text(
                json.dumps(bundle, ensure_ascii=False, indent=2))
            print(f"      wrote improvement_output.json (source={src})")


if __name__ == "__main__":
    main()
