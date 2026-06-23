"""Acoustic similarity matching for repeated song sections.

Repeated choruses and bridge variations trip up ASR: occurrence A transcribes
correctly (high forced-alignment word scores), then occurrence B hallucinates
(low scores) because the LM has drifted. If A and B share the same vocal
melody, we can detect the match and copy A's text to B.

Why MFCC+Δ+Δ² with DTW (not mel mean-pool + cosine):
  Mean-pool collapses the time axis — two phrases with the same notes in a
  different order look identical. Worse, the backing track dominates the energy
  budget, making unrelated sections look acoustically "similar" (cos > 0.99).
  MFCC+delta features capture the *shape* of the vocal melody (formant
  trajectory + rate-of-change), and DTW aligns the two sequences allowing for
  slight tempo differences between repetitions. The vocal stem (not the mix)
  must be passed so the backing-track energy is mostly absent.

Algorithm
---------
1. Classify segments as *anchored* (mean word-score ≥ HIGH_CONF) or
   *uncertain* (mean word-score ≤ LOW_CONF). Segments without word stamps or
   that are adlib loops are skipped.
2. For each anchor, compute an MFCC+Δ+Δ² sequence from the vocal stem.
3. For each uncertain segment, compute its sequence and DTW-compare against
   every anchor.
4. If best_dtw_similarity ≥ threshold AND texts differ → replace text.

Confidence via word scores:
  whisperX forced-alignment gives each word a score in [0, 1]. A hallucinated
  word has low phoneme overlap with the real audio → low score even if the
  surrounding alignment succeeded.
"""

import logging
import os
from typing import Optional

import numpy as np

logger = logging.getLogger("genly.acoustic_match")

# ── Tuneable constants ──────────────────────────────────────────────────────
_HIGH_CONF: float = float(os.environ.get("ACOUSTIC_MATCH_HIGH_CONF", "0.72"))
_LOW_CONF:  float = float(os.environ.get("ACOUSTIC_MATCH_LOW_CONF",  "0.65"))
_THRESHOLD: float = float(os.environ.get("ACOUSTIC_MATCH_THRESHOLD", "0.20"))
_MIN_DUR:   float = 2.0    # ignore micro-segments
_N_MFCC:    int   = 13
_SR:        int   = 16_000
_MAX_DTW_FRAMES: int = 200  # subsample longer seqs; DTW is O(T²)


# ── Internal helpers ────────────────────────────────────────────────────────

def _word_score(seg: dict) -> Optional[float]:
    """Mean forced-alignment score across all words, or None if unavailable."""
    words = seg.get("words") or []
    scores = [float(w["score"]) for w in words if w.get("score") is not None]
    return (sum(scores) / len(scores)) if scores else None


def _mfcc_sequence(audio_path: str, start_s: float, end_s: float) -> "Optional[np.ndarray]":
    """Return MFCC+Δ+Δ² sequence (N_MFCC*3, T) z-normalized per coefficient.

    Captures vocal melody contour without being dominated by energy level.
    Subsamples to _MAX_DTW_FRAMES if the segment is long.
    """
    try:
        import librosa
        dur = end_s - start_s
        if dur < 0.5:
            return None
        y, _ = librosa.load(audio_path, sr=_SR, mono=True,
                             offset=start_s, duration=dur)
        if len(y) < _SR * 0.3:
            return None
        mfcc = librosa.feature.mfcc(y=y, sr=_SR, n_mfcc=_N_MFCC,
                                     n_fft=1024, hop_length=512)
        d1 = librosa.feature.delta(mfcc)
        d2 = librosa.feature.delta(mfcc, order=2)
        seq = np.vstack([mfcc, d1, d2])  # (N_MFCC*3, T)
        # z-normalize per coefficient so energy level doesn't dominate
        mu = seq.mean(axis=1, keepdims=True)
        sd = seq.std(axis=1, keepdims=True) + 1e-9
        seq = (seq - mu) / sd
        # Subsample if T too large
        T = seq.shape[1]
        if T > _MAX_DTW_FRAMES:
            stride = (T + _MAX_DTW_FRAMES - 1) // _MAX_DTW_FRAMES
            seq = seq[:, ::stride]
        return seq
    except Exception as exc:
        logger.debug("[ACOUSTIC] mfcc %.1f–%.1fs failed: %s", start_s, end_s, exc)
        return None


def _dtw_similarity(seq_a: "np.ndarray", seq_b: "np.ndarray") -> float:
    """DTW similarity in [0, 1]. 1 = identical trajectory, ~0 = unrelated.

    Normalizes the accumulated DTW cost by the longer sequence length so
    short vs. long comparisons are on the same scale.
    """
    try:
        import librosa
        D, _ = librosa.sequence.dtw(seq_a, seq_b, metric="euclidean")
        dist = float(D[-1, -1]) / max(seq_a.shape[1], seq_b.shape[1])
        return 1.0 / (1.0 + dist)
    except Exception as exc:
        logger.debug("[ACOUSTIC] dtw failed: %s", exc)
        return 0.0


# ── Public API ──────────────────────────────────────────────────────────────

def correct_by_acoustic_similarity(
    segments: list,
    audio_path: str,
    *,
    threshold: Optional[float] = None,
) -> list:
    """Replace uncertain transcriptions with text from acoustically similar
    high-confidence segments in the same song.

    Parameters
    ----------
    segments:   whisperX segments after ghost-filter (have word scores).
    audio_path: path to the VOCAL STEM (not the mix) — background energy must
                be minimal for MFCC features to be discriminative.
    threshold:  DTW similarity floor (0–1); overrides ACOUSTIC_MATCH_THRESHOLD.

    Returns a new list; originals are not mutated.  Never raises — any
    failure returns the original list unchanged.
    """
    if os.environ.get("ACOUSTIC_MATCH_ENABLED", "1").strip().lower() in (
        "0", "false", "off", "no"
    ):
        return segments

    if not segments or not audio_path or not os.path.exists(audio_path):
        return segments

    thr = threshold if threshold is not None else _THRESHOLD

    # ── Step 1: classify segments ──────────────────────────────────────────
    try:
        from post_reconcile import _is_adlib_loop
    except ImportError:
        _is_adlib_loop = lambda _t: False           # noqa: E731

    anchored = []   # list of (idx, np.ndarray, str)
    uncertain = []  # list of (idx, float)

    for i, seg in enumerate(segments):
        text = (seg.get("text") or "").strip()
        if not text or _is_adlib_loop(text):
            continue
        try:
            start, end = float(seg["start"]), float(seg["end"])
        except (KeyError, TypeError, ValueError):
            continue
        if end - start < _MIN_DUR:
            continue

        ws = _word_score(seg)
        if ws is None:
            continue  # no word stamps → can't assess confidence

        # Temporary diagnostic: log every candidate's word score
        logger.info("[ACOUSTIC-WS] %.1f–%.1fs ws=%.3f %r",
                    start, end, ws, text[:40])

        if ws >= _HIGH_CONF:
            seq = _mfcc_sequence(audio_path, start, end)
            if seq is not None:
                anchored.append((i, seq, text))
        elif ws <= _LOW_CONF:
            uncertain.append((i, ws))

    if not anchored or not uncertain:
        return segments

    # ── Step 2: match & correct ────────────────────────────────────────────
    out = list(segments)
    n_corrected = 0

    for i, ws in uncertain:
        seg = segments[i]
        try:
            start, end = float(seg["start"]), float(seg["end"])
        except (KeyError, TypeError, ValueError):
            continue

        seq_i = _mfcc_sequence(audio_path, start, end)
        if seq_i is None:
            continue

        best_sim, best_j, best_text = 0.0, -1, ""
        for j, seq_j, text_j in anchored:
            sim = _dtw_similarity(seq_i, seq_j)
            if sim > best_sim:
                best_sim, best_j, best_text = sim, j, text_j

        if best_sim < thr or best_j < 0:
            logger.debug(
                "[ACOUSTIC] %.1f–%.1fs (ws=%.2f) best_sim=%.3f < %.2f → skip",
                start, end, ws, best_sim, thr,
            )
            continue

        src_text = best_text
        tgt_text = (seg.get("text") or "").strip()
        if not src_text or src_text == tgt_text:
            continue

        anchor_seg = segments[best_j]
        logger.info(
            "[ACOUSTIC] %.1f–%.1fs (word_score=%.2f) %r → %r  "
            "(dtw_sim=%.3f, anchor=%.1f–%.1fs)",
            start, end, ws,
            tgt_text[:50], src_text[:50],
            best_sim,
            float(anchor_seg["start"]), float(anchor_seg["end"]),
        )
        out[i] = {**seg, "text": src_text, "acoustic_corrected": True}
        n_corrected += 1

    if n_corrected:
        logger.info("[ACOUSTIC] %d/%d uncertain segments corrected",
                    n_corrected, len(uncertain))
    else:
        logger.debug("[ACOUSTIC] 0 corrections (%d anchors, %d uncertain)",
                     len(anchored), len(uncertain))
    return out
