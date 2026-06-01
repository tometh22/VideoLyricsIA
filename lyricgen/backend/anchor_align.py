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

logger = logging.getLogger("genly.anchor_align")

# Validation thresholds (tuned on the 12-song lab set; see module docstring).
_MIN_FRAC_IN_VOICE = 0.70      # ≥70% of lines must land where the stem has voice
_VOICE_PAD_S = 1.2            # a line counts as "in voice" within this slack
_MAX_OFFSET_S = 60.0         # sanity cap on the global anchor offset
_UNDERCOVER_GAP_S = 30.0     # last line >this before last vocal ⇒ short/foreign


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

    first_wx = _first_word_time(wx_segs)
    offset = 0.0
    if first_wx is not None:
        cand = first_wx - pairs[0][0]
        offset = cand if abs(cand) <= _MAX_OFFSET_S else 0.0
        if abs(cand) > _MAX_OFFSET_S:
            logger.info("[ANCHOR] offset %.1fs out of range — using 0", cand)
    meta["offset"] = offset

    # Build segments: each line spans up to the next line's start − 50 ms.
    segs = []
    for i, (t, txt) in enumerate(pairs):
        st = round(max(0.0, t + offset), 2)
        if i + 1 < len(pairs):
            en = round(pairs[i + 1][0] + offset - 0.05, 2)
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
