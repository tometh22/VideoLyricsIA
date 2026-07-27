"""VAD-validated lrclib-synced scaffold — the robust primary fallback when
whisperX reconcile aborts.

WHY
---
On guitar/instrumental-heavy songs whisperX reconcile drift-aborts even on a
clean vocal stem (Rata Blanca "La Leyenda": 3/47 lines). The cascade then falls
to fragile paths: `whisper_align` (Whisper-1 on the mix → outro hallucination)
or `forced_align`/Cureau (which CLAMPS un-matchable lines to ONE timestamp — the
"10 lines piled at 3:30" pile-up the operator sees on the post-solo verse).

lrclib `syncedLyrics` is human karaoke timing: correct line ORDER + relative
timing, no hallucination, no pile-up. The only risk is that the synced version
belongs to a DIFFERENT edit (cumbia "Luz de Día": synced runs to 248 s on a
169 s audio; the "Cosas Mías" 35 s-off incident). So we (1) anchor the synced
timeline's global offset to whisperX's first sung word — fixing the version
offset against THIS recording — and (2) VALIDATE the result against where the
voice actually is (energy VAD on the stem) + a duration span gate. Only if the
offset-corrected lines land where there is singing do we trust it; otherwise we
fall through to the existing cascade.

Lab-validated (2026-06-01, ~/genly_timing_lab) across 12 songs spanning rock /
ballad / acoustic-live / english-pop / reggaeton: 12/12 correct accept/reject —
accepts Rata Blanca (47 clean lines, no pile-up), reggaeton with heavy
repetition (Gasolina/Tití, each repeated chorus line in its own slot), ballads
(no quiet-line drop); rejects cumbia (+73 s overshoot) and the Soda live version
(synced mismatched the live arrangement).

CONTRACT
--------
- `vocal_regions(stem_path) -> list[(start, end)]`: energy VAD on the isolated
  vocal stem. Never raises; returns [] on any failure (caller treats as "can't
  validate" → no scaffold).
- `build_synced_scaffold(synced_lrc, wx_segs, audio_dur, *, vocal_regions)
   -> (segments | None, meta)`: returns offset-corrected synced segments when
  they pass validation, else (None, meta) so the caller keeps its fallbacks.
  Pure given the precomputed `vocal_regions`.
"""
from __future__ import annotations

import logging
import os

logger = logging.getLogger("genly.anchor_align")

_TRUE = ("1", "true", "yes", "on", "y", "t")


def _onset_enabled() -> bool:
    """Stage 3.1 (vocal-onset + multi-anchor offset). Default OFF so it ships
    inert; flip ANCHOR_ONSET_ENABLED=1 in staging to validate before prod."""
    return os.environ.get("ANCHOR_ONSET_ENABLED", "0").strip().lower() in _TRUE

# Validation thresholds (tuned on the 12-song lab set; see module docstring).
_MIN_FRAC_IN_VOICE = 0.70      # ≥70% of lines must land where the stem has voice
_VOICE_PAD_S = 1.2            # a line counts as "in voice" within this slack
_MAX_OFFSET_S = 90.0         # sanity cap on the global anchor offset (raised
                             # from 60: live/extended recordings legitimately
                             # need a larger shift — Coti "Nada Fue Un Error
                             # (en vivo)" needs ~+30s but whisperX first-word
                             # anchored it to +75s)
_UNDERCOVER_GAP_S = 30.0     # last line >this before last vocal ⇒ short/foreign
# Offset refinement (2026-06-03): a single global offset anchored to whisperX's
# first sung word can lock onto a WRONG shift that still scores high
# frac_in_voice on a live/dense recording (whisperX misses the early vocals →
# anchors the block too LATE). When we have a VAD signal we re-search the global
# shift and, among the near-best-scoring offsets, keep the EARLIEST line-1 — the
# first verse must land on the first real singing, not a later chorus. Proven on
# Coti "Nada Fue Un Error (en vivo)": +75.5s (whisperX) scored 0.96 but the
# correct +~30s scores 0.98; the search picks the early, higher one.
_OFFSET_SEARCH_STEP_S = 0.5   # scan resolution
_OFFSET_SEARCH_MARGIN = 0.01  # only override the base offset if the search beats
                              # it by this much frac_in_voice (studio songs, where
                              # the legacy anchor is already optimal, are untouched).
                              # Kept small because the wrong-but-plausible live
                              # offset can score within ~0.02 of the correct one.
_OFFSET_NEAR_TOL = 0.03       # offsets within this of the max count as "near-best"


def vocal_regions(
    stem_path: str,
    *,
    frame_s: float = 0.05,
    thr_ratio: float = 0.12,
    merge_gap_s: float = 0.6,
    min_region_s: float = 0.4,
):
    """Energy-based voice-activity windows on the ISOLATED vocal stem.

    The stem has only vocals, so RMS energy above a noise floor == singing.
    Returns a list of (start, end) seconds; [] on any failure (missing librosa,
    unreadable file, silent stem). Never raises."""
    try:
        import numpy as np
        import librosa
    except Exception as e:  # librosa not installed in this context
        logger.info("[ANCHOR] vocal_regions: librosa unavailable (%s)", e)
        return []
    try:
        y, sr = librosa.load(stem_path, sr=16000, mono=True)
        if y.size == 0:
            return []
        hop = max(1, int(frame_s * sr))
        rms = librosa.feature.rms(y=y, frame_length=hop * 2, hop_length=hop)[0]
        if rms.size == 0:
            return []
        thr = max(thr_ratio * float(np.percentile(rms, 95)), 1e-4)
        voiced = rms > thr
        times = librosa.frames_to_time(np.arange(len(rms)), sr=sr, hop_length=hop)
        raw = []
        i = 0
        n = len(voiced)
        while i < n:
            if voiced[i]:
                j = i
                while j < n and voiced[j]:
                    j += 1
                raw.append([float(times[i]), float(times[min(j, n - 1)])])
                i = j
            else:
                i += 1
        merged = []
        for r in raw:
            if merged and r[0] - merged[-1][1] <= merge_gap_s:
                merged[-1][1] = r[1]
            else:
                merged.append(r)
        return [(a, b) for a, b in merged if b - a >= min_region_s]
    except Exception as e:
        logger.warning("[ANCHOR] vocal_regions failed: %s", e)
        return []


def _first_word_time(wx_segs) -> float | None:
    for s in wx_segs or []:
        for w in s.get("words") or []:
            if isinstance(w, dict) and "start" in w:
                try:
                    return float(w["start"])
                except (TypeError, ValueError):
                    continue
    return None


def _last_word_time(wx_segs) -> float | None:
    last = None
    for s in wx_segs or []:
        for w in s.get("words") or []:
            if isinstance(w, dict) and w.get("end") is not None:
                try:
                    last = float(w["end"])
                except (TypeError, ValueError):
                    continue
    return last


def _in_voice(t: float, regions, pad: float = _VOICE_PAD_S) -> bool:
    return any(a - pad <= t <= b + pad for a, b in regions)


# ── Anchor-based alignment (corrects local drift, not just a global offset) ──
# A single global offset (anchored to the first sung word) fixes where the song
# STARTS but not internal drift: the chosen lrclib version's line spacing can be
# ~1-2s off from THIS recording (Rata Blanca: "Cuenta la historia" landed at
# 15.6s vs the real 14.2s, because the offset was anchored to the faint intro
# "Uh, no, no" ad-lib). We instead match DISTINCTIVE canonical words to
# whisperX's actual word detections and fit a robust line synced_t → audio_t,
# so every line (early and late) lands on the voice.

import re as _re
import unicodedata as _ud

_STOPWORDS = frozenset({
    "que", "los", "las", "una", "con", "por", "del", "para", "como", "este",
    "esta", "pero", "sus", "muy", "mas", "más", "ese", "esa", "hay", "fue",
    "son", "han", "sin", "cuando", "donde", "porque", "aunque", "todo", "toda",
    "desde", "hasta", "entre", "sobre", "cada", "más", "ya",
})
_MIN_ANCHOR_LEN = 5
_FIT_MIN_ANCHORS = 3
_FIT_MIN_SPAN_S = 60.0


def _norm_word(w: str) -> str:
    w = _ud.normalize("NFKD", (w or "").lower())
    w = "".join(c for c in w if not _ud.combining(c))
    return _re.sub(r"[^a-z0-9ñ]", "", w)


def _wx_word_stream(wx_segs):
    """[(normalized_word, start_time)] from whisperX segments, in order."""
    out = []
    for s in wx_segs or []:
        for w in s.get("words") or []:
            if isinstance(w, dict) and "start" in w:
                nw = _norm_word(w.get("word"))
                if nw:
                    try:
                        out.append((nw, float(w["start"])))
                    except (TypeError, ValueError):
                        continue
    return out


def _line_first_anchor(text: str):
    """First distinctive (rare, >=5-char, non-stopword) token of a line — a
    reliable acoustic anchor near the line's start."""
    for raw in (text or "").split():
        nw = _norm_word(raw)
        if len(nw) >= _MIN_ANCHOR_LEN and nw not in _STOPWORDS:
            return nw
    return None


def _build_anchors(pairs, wx_stream):
    """Match each line's first distinctive word to whisperX's detection of it,
    monotonically. Returns [(synced_time, audio_time)]."""
    from collections import defaultdict
    idx = defaultdict(list)
    for nw, t in wx_stream:
        idx[nw].append(t)
    anchors = []
    last_audio = -1.0
    for t_syn, text in pairs:
        aw = _line_first_anchor(text)
        if not aw:
            continue
        cand = next((tt for tt in idx.get(aw, ()) if tt > last_audio), None)
        if cand is None:
            continue
        anchors.append((t_syn, cand))
        last_audio = cand
    return anchors


def _fit_alignment(pairs, wx_segs):
    """Robust map synced_t → audio_t as (a, b, n_anchors): audio ≈ a*syn + b.

    Multi-anchor least-squares (drift-correcting) when there are enough
    well-spread matches; median offset for a few; legacy first-word offset when
    none match. a is clamped to a sane stretch range so a bad fit can't warp
    the timeline."""
    if not _onset_enabled():
        # Legacy (#513): single global offset anchored to whisperX's first word.
        first_wx = _first_word_time(wx_segs)
        if first_wx is not None and pairs:
            b = first_wx - pairs[0][0]
            return 1.0, (b if abs(b) <= _MAX_OFFSET_S else 0.0), 0
        return 1.0, 0.0, 0
    import statistics
    anchors = _build_anchors(pairs, _wx_word_stream(wx_segs))
    if anchors:  # trim outliers against the median offset
        offs = [wt - st for st, wt in anchors]
        med = statistics.median(offs)
        anchors = [(st, wt) for (st, wt), o in zip(anchors, offs)
                   if abs(o - med) <= 4.0]
    span = (anchors[-1][0] - anchors[0][0]) if len(anchors) >= 2 else 0.0
    if len(anchors) >= _FIT_MIN_ANCHORS and span >= _FIT_MIN_SPAN_S:
        xs = [st for st, _ in anchors]
        ys = [wt for _, wt in anchors]
        n = len(xs)
        mx, my = sum(xs) / n, sum(ys) / n
        denom = sum((x - mx) ** 2 for x in xs)
        a = (sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / denom) if denom else 1.0
        a = min(1.2, max(0.85, a))
        return a, my - a * mx, len(anchors)
    if anchors:
        b = statistics.median(wt - st for st, wt in anchors)
        return 1.0, (b if abs(b) <= _MAX_OFFSET_S else 0.0), len(anchors)
    # No reliable word matches (whisperX mis-hears guitar-heavy songs — the same
    # reason reconcile aborts). Fall back to a VOCAL-ONSET anchor: align the
    # first sung VERSE to where the voice actually starts, using TIMING (robust)
    # not mis-heard text. Fixes Rata Blanca ("Cuenta" 15.5s → 14.7s, the real
    # onset) without depending on whisperX getting the words right.
    b = _vocal_onset_offset(pairs, wx_segs)
    return 1.0, (b if abs(b) <= _MAX_OFFSET_S else 0.0), 0


def _vocal_onset_offset(pairs, wx_segs):
    """Offset aligning the first VERSE line to the first sustained vocal onset.

    The first whisperX word after the intro gap (>3 s, within the first 40 s) is
    where real singing starts; the first synced line with a distinctive word is
    the first verse (skipping any leading 'oh/uh' ad-lib). Aligning those two
    fixes the start without trusting whisperX's (often wrong) early text. Degrades
    to the plain first-word offset when there is no intro gap / ad-lib."""
    stream = _wx_word_stream(wx_segs)
    if not stream or not pairs:
        return 0.0
    onset = stream[0][1]
    for i in range(1, len(stream)):
        if stream[i][1] - stream[i - 1][1] > 3.0 and stream[i][1] < 40.0:
            onset = stream[i][1]
            break
    verse_t = next((t for t, text in pairs if _line_first_anchor(text)), pairs[0][0])
    return onset - verse_t


def build_synced_scaffold(
    pairs,
    wx_segs,
    audio_dur,
    *,
    vocal_regions,
):
    """Offset-corrected lrclib-synced scaffold, validated against the voice.

    `pairs`: [(start_seconds, text)] parsed from lrclib syncedLyrics, in song
        order (use lrclib_aligner._parse_lrc_to_line_times).
    `wx_segs`: whisperX segments (for the first sung-word anchor + last-word
        coverage check).
    `audio_dur`: duration of THIS recording (for the overshoot span gate).
    `vocal_regions`: precomputed energy-VAD windows on the stem.

    Returns (segments, meta). `segments` is None (with meta.reason) when the
    scaffold cannot be trusted, so the caller keeps its existing fallbacks.
    """
    meta = {"reason": "ok", "offset": 0.0, "frac_in_voice": 0.0}
    if not pairs or len(pairs) < 4:
        meta["reason"] = "too_few_synced_lines"
        return None, meta

    # Align the synced timeline to THIS recording. Multi-anchor linear fit when
    # there are enough distinctive-word matches (corrects local drift); else a
    # single global offset (median, or legacy first-word).
    a, b, n_anchors = _fit_alignment(pairs, wx_segs)
    meta["align"] = {"a": round(a, 4), "b": round(b, 2), "anchors": n_anchors}
    meta["offset"] = round(b, 2)

    # Voice-validated offset re-search. Only for single global offsets (a≈1):
    # the multi-anchor linear fit (a≠1) already corrects drift and must NOT be
    # overridden. The first-word anchor can lock onto a wrong-but-high-scoring
    # shift on live/dense recordings; re-search and keep the EARLIEST line-1
    # among the near-best offsets. Studio songs (legacy anchor already optimal)
    # don't clear the margin, so they're untouched.
    if vocal_regions and abs(a - 1.0) < 1e-6 and len(vocal_regions) >= 2 and pairs:
        def _frac_for(off):
            return sum(1 for (t, _t) in pairs
                       if _in_voice(max(0.0, t + off), vocal_regions)) / len(pairs)
        base_frac = _frac_for(b)
        scan, o = [], -_MAX_OFFSET_S
        while o <= _MAX_OFFSET_S:
            scan.append((round(o, 2), _frac_for(o)))
            o += _OFFSET_SEARCH_STEP_S
        max_frac = max(f for _o, f in scan)
        if max_frac > base_frac + _OFFSET_SEARCH_MARGIN:
            best = min(off for off, f in scan if f >= max_frac - _OFFSET_NEAR_TOL)
            meta["offset_refined"] = {
                "from": round(b, 2), "to": best,
                "base_frac": round(base_frac, 3), "new_frac": round(_frac_for(best), 3),
            }
            b = best
            meta["offset"] = round(b, 2)

    def _map(t):
        return a * t + b

    # Build segments: each line spans up to the next line's start − 50 ms.
    segs = []
    for i, (t, txt) in enumerate(pairs):
        st = round(max(0.0, _map(t)), 2)
        if i + 1 < len(pairs):
            en = round(_map(pairs[i + 1][0]) - 0.05, 2)
        else:
            en = round(st + 3.0, 2)
        if en <= st:
            en = st + 0.5
        segs.append({"start": st, "end": en, "text": txt, "review": True})

    # ── Validation ────────────────────────────────────────────────────────
    # 1) span gate: the scaffold must not overshoot the recording (foreign /
    #    longer edit — cumbia 248 s on 169 s audio).
    try:
        from timing_confidence import span_gate
        sv = span_gate(segs, audio_dur)
        if not sv.ok and sv.reason != "no_duration":
            meta["reason"] = f"span_gate:{sv.reason}"
            return None, meta
    except Exception as e:  # never let a guard import crash the path
        logger.debug("[ANCHOR] span_gate skipped (%s)", e)

    # 2) vocal coverage: ≥70% of lines must land where the stem actually has
    #    singing (catches a synced version whose timeline doesn't match this
    #    recording — the Soda live arrangement, "Cosas Mías" offset).
    if vocal_regions:
        in_voice = sum(1 for s in segs if _in_voice(s["start"], vocal_regions))
        frac = in_voice / len(segs)
        meta["frac_in_voice"] = round(frac, 3)
        if frac < _MIN_FRAC_IN_VOICE:
            meta["reason"] = f"low_voice_coverage:{frac:.2f}"
            return None, meta
        # 3) under-coverage: the scaffold must not END long before the last
        #    sung word (a too-short / foreign-shorter synced version).
        last_wx = _last_word_time(wx_segs)
        if last_wx is not None and segs[-1]["start"] < last_wx - _UNDERCOVER_GAP_S:
            meta["reason"] = (
                f"under_coverage last_line={segs[-1]['start']:.0f}s "
                f"<< last_vocal={last_wx:.0f}s"
            )
            return None, meta
    else:
        # No VAD signal → fall back to span gate only (already passed above).
        meta["frac_in_voice"] = -1.0

    return segs, meta
