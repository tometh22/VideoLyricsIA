"""Acoustic similarity matching for repeated song sections.

Repeated choruses and bridge variations trip up ASR in a specific way: the
model transcribes occurrence A correctly (high forced-alignment word scores)
then hallucinates something plausible-but-wrong for occurrence B (low scores),
because by then its language model has drifted. If A and B sound acoustically
identical, we can detect the match and copy A's text to B.

Algorithm
---------
1. Classify segments as *anchored* (mean word-score ≥ HIGH_CONF) or
   *uncertain* (mean word-score ≤ LOW_CONF). Segments without word stamps
   (forced alignment failed) are skipped.
2. For each uncertain segment compute a mel-spectrogram mean-pool embedding.
3. Compare against every anchor embedding (cosine similarity).
4. If best_sim ≥ threshold AND texts differ → replace the uncertain text.

Why mel mean-pool instead of DTW:
  Same performance, same tempo → time-warping buys nothing. Mean-pool is O(N)
  vs O(N²) for DTW and takes <10 ms per segment on CPU.

Confidence via word scores:
  whisperX forced-alignment gives each word a score in [0, 1]. A hallucinated
  word has low phoneme overlap with the real audio → low score even if the
  surrounding alignment succeeded. E.g., "tan miedo" forced against audio that
  says "Frágil espejo" scores ~0.3, while the correct transcription scores ~0.8.
"""

import logging
import os
from typing import Optional

import numpy as np

logger = logging.getLogger("genly.acoustic_match")

# ── Tuneable constants ──────────────────────────────────────────────────────
_HIGH_CONF: float = float(os.environ.get("ACOUSTIC_MATCH_HIGH_CONF", "0.72"))
_LOW_CONF: float  = float(os.environ.get("ACOUSTIC_MATCH_LOW_CONF",  "0.55"))
_THRESHOLD: float = float(os.environ.get("ACOUSTIC_MATCH_THRESHOLD", "0.82"))
_MIN_DUR:   float = 2.0   # ignore micro-segments — too short to embed reliably
_N_MELS:    int   = 64
_SR:        int   = 16_000


# ── Internal helpers ────────────────────────────────────────────────────────

def _word_score(seg: dict) -> Optional[float]:
    """Mean forced-alignment score across all words, or None if unavailable."""
    words = seg.get("words") or []
    scores = [float(w["score"]) for w in words if w.get("score") is not None]
    return (sum(scores) / len(scores)) if scores else None


def _mel_embedding(audio_path: str, start_s: float, end_s: float) -> "Optional[np.ndarray]":
    """Return mean mel-spectrogram (shape: [N_MELS]) for the given slice."""
    try:
        import librosa  # deferred — not installed in all envs
        dur = end_s - start_s
        if dur < 0.5:
            return None
        y, _ = librosa.load(audio_path, sr=_SR, mono=True,
                             offset=start_s, duration=dur)
        if len(y) < _SR * 0.3:
            return None
        mel = librosa.feature.melspectrogram(y=y, sr=_SR, n_mels=_N_MELS,
                                              n_fft=1024, hop_length=512)
        mel_db = librosa.power_to_db(mel + 1e-9, ref=np.max)
        return mel_db.mean(axis=1)          # (N_MELS,) — time-averaged
    except Exception as exc:
        logger.debug("[ACOUSTIC] embed %.1f–%.1fs failed: %s", start_s, end_s, exc)
        return None


def _cosine(a: "np.ndarray", b: "np.ndarray") -> float:
    na, nb = np.linalg.norm(a), np.linalg.norm(b)
    if na < 1e-9 or nb < 1e-9:
        return 0.0
    return float(np.dot(a, b) / (na * nb))


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
    audio_path: path to the audio file the segments were transcribed from.
    threshold:  cosine similarity floor; overrides ACOUSTIC_MATCH_THRESHOLD.

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
        from post_reconcile import _is_adlib_loop  # no circular import: pr doesn't import us
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

        if ws >= _HIGH_CONF:
            emb = _mel_embedding(audio_path, start, end)
            if emb is not None:
                anchored.append((i, emb, text))
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

        emb_i = _mel_embedding(audio_path, start, end)
        if emb_i is None:
            continue

        best_sim, best_j, best_text = 0.0, -1, ""
        for j, emb_j, text_j in anchored:
            sim = _cosine(emb_i, emb_j)
            if sim > best_sim:
                best_sim, best_j, best_text = sim, j, text_j

        if best_sim < thr or best_j < 0:
            continue

        src_text = best_text
        tgt_text = (seg.get("text") or "").strip()
        if not src_text or src_text == tgt_text:
            continue

        anchor_seg = segments[best_j]
        logger.info(
            "[ACOUSTIC] %.1f–%.1fs (word_score=%.2f) %r → %r  "
            "(cos=%.3f, anchor=%.1f–%.1fs)",
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
