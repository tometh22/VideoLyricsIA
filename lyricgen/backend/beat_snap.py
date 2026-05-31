"""Beat-snap — Rotor's "rhythm engine".

WHY
---
WhisperX / forced-align timestamps are word-level accurate (±100ms) but they
sit wherever the singer enters, which isn't always on the beat — singers
lean a touch ahead or behind for feel. The result reads as "almost right
but slightly off the music". Rotor's secret sauce is detecting the beat
grid (BPM via tempo tracking) and snapping each subtitle's start to the
nearest strong beat. Subtitles land in time with the music, not just with
the voice. That's the difference between "auto-generated" and "premium".

CONTRACT
--------
- Behind `BEAT_SNAP_ENABLED` (default off).
- Snap window default 80ms via `BEAT_SNAP_WINDOW_MS`. (Was 200ms before
  2026-05-31; dropped after the Agus.Cafisi report where 200ms drift
  was audible on "Nada fue un error" / "Luz de día (cumbia)".)
- Segments with reliable forced-align word stamps (`words[].score >= 0.5`)
  are NOT snapped — their start is already aligned to the singer's
  actual onset and snapping would drift it AWAY from the audio.
- `apply(audio_path, segs)` returns segments with starts shifted to the
  closest beat within ±window, or the originals on any failure. Never raises.
- `detect_beats` and `snap_segments` are pure and unit-testable.
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger("genly.beat_snap")

_TRUE = ("1", "true", "yes", "on", "y", "t")


def is_enabled() -> bool:
    return os.environ.get("BEAT_SNAP_ENABLED", "0").strip().lower() in _TRUE


def detect_beats(audio_path: str) -> tuple[float, list[float]] | None:
    """Detect BPM and beat times (seconds) via librosa.beat_track. Returns
    `(bpm, beats)` or None on failure. Beat-track at 22 kHz mono is ~3-5s
    for a 6-min song on CPU."""
    try:
        import librosa
        y, sr = librosa.load(audio_path, sr=22050, mono=True)
        tempo, beat_frames = librosa.beat.beat_track(y=y, sr=sr)
        beats = librosa.frames_to_time(beat_frames, sr=sr).tolist()
        # librosa may return tempo as a 1-element ndarray.
        try:
            bpm = float(tempo[0]) if hasattr(tempo, "__len__") else float(tempo)
        except (TypeError, ValueError):
            bpm = 0.0
        return bpm, [float(t) for t in beats]
    except Exception as e:  # librosa not installed, file unreadable, anything
        logger.warning("[BEAT_SNAP] detect failed (%s) — skipping snap", e)
        return None


# 2026-05-31 (Agus): default window dropped from 200 → 80 ms after
# the operator reported "Nada fue un error" and "Luz de día (cumbia)"
# showing the chorus a perceptible amount LATE vs the audio. The 200ms
# window was producing audible drift because:
#   - whisperx forced-align word stamps are per-millisecond reliable
#   - snapping the segment.start away from the first word's actual time
#     by up to 200ms creates a visible "subtitle lags the vocal" effect
# 80ms is below the typical karaoke "feel" threshold (~100ms) while
# still polishing the wobble from whisper-1 word-less segments.
_DEFAULT_WINDOW_MS = 80

# Threshold for "this word's stamp is trustworthy enough to skip the
# beat snap". whisperx forced-align populates per-word `score` ∈ [0,1];
# above 0.5 means the aligner found the audio frames that match the word,
# so the segment.start (= words[0].start) is already correct and snapping
# would PUSH it away from the truth.
_RELIABLE_WORD_SCORE = 0.5


def _has_reliable_words(segment: dict) -> bool:
    """True if the segment carries forced-align word stamps with score.

    When this is True we DO NOT snap — moving start away from the first
    word's actual onset is a downgrade, not a polish.
    """
    words = segment.get("words")
    if not words or not isinstance(words, list):
        return False
    for w in words:
        if not isinstance(w, dict):
            continue
        try:
            score = float(w.get("score", 0.0))
        except (TypeError, ValueError):
            continue
        if score >= _RELIABLE_WORD_SCORE:
            return True
    return False


def snap_segments(segs: list[dict], beats: list[float], *,
                   window_ms: int = _DEFAULT_WINDOW_MS,
                   skip_if_reliable_words: bool = True) -> list[dict]:
    """Snap each segment.start to the nearest beat within ±window_ms.

    Preserves monotonicity (snapped start >= prev_end + 5ms) and never
    pushes start past the segment's own end. Words inside `segs[i]["words"]`
    are NOT shifted — they stay truthful to the audio; only the display
    window's start moves. Pure + testable.

    `skip_if_reliable_words` (default True) leaves segments alone when
    they already carry forced-align word stamps with `score >= 0.5`.
    Those stamps are ground truth for the vocal onset and beat-snapping
    them only pushes the subtitle away from the singer's actual entry.
    Pass False to force snapping of every segment regardless.
    """
    if not beats or not segs or window_ms <= 0:
        return segs
    window_s = window_ms / 1000.0
    import bisect
    beats_sorted = sorted(beats)
    out: list[dict] = []
    prev_end = 0.0
    for s in segs:
        try:
            start = float(s.get("start", 0))
            end = float(s.get("end", start))
        except (TypeError, ValueError):
            out.append(s)
            continue
        if skip_if_reliable_words and _has_reliable_words(s):
            out.append(s)
            prev_end = end
            continue
        # Nearest beat — check both neighbours.
        i = bisect.bisect_left(beats_sorted, start)
        candidates = []
        if i > 0:
            candidates.append(beats_sorted[i - 1])
        if i < len(beats_sorted):
            candidates.append(beats_sorted[i])
        if not candidates:
            out.append(s)
            prev_end = end
            continue
        nearest = min(candidates, key=lambda b: abs(b - start))
        if abs(nearest - start) > window_s:
            out.append(s)
            prev_end = end
            continue
        # Clamp to no-overlap / not-past-end.
        new_start = max(nearest, prev_end + 0.005)
        if new_start >= end:
            out.append(s)
            prev_end = end
            continue
        new_seg = dict(s)
        new_seg["start"] = new_start
        out.append(new_seg)
        prev_end = end
    return out


def apply(audio_path: str, segs: list[dict], *,
          window_ms: int | None = None) -> list[dict]:
    """Detect beats and snap, behind BEAT_SNAP_ENABLED. One-shot helper for
    the pipeline; returns originals on any failure."""
    if not is_enabled() or not segs or not audio_path:
        return segs
    if window_ms is None:
        try:
            window_ms = int(os.environ.get("BEAT_SNAP_WINDOW_MS", str(_DEFAULT_WINDOW_MS)))
        except (TypeError, ValueError):
            window_ms = _DEFAULT_WINDOW_MS
    result = detect_beats(audio_path)
    if result is None:
        return segs
    bpm, beats = result
    snapped = snap_segments(segs, beats, window_ms=window_ms)
    moved = sum(1 for a, b in zip(segs, snapped)
                if float(a.get("start", 0)) != float(b.get("start", 0)))
    logger.info("[BEAT_SNAP] %.1f bpm, %d beats — %d/%d starts snapped (window=%dms)",
                bpm, len(beats), moved, len(segs), window_ms)
    return snapped
