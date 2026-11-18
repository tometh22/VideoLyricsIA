"""Local Whisper forced alignment — the cascade's last acoustic witness.

WHY (the audit trail)
---------------------
Incident 2026-09-15, staging job ``18dc85ecd8d6`` ("Navidad de Aimogasta",
campaña ``ba3318bdfffe``): the operator pasted the official 40-line lyric
over an 18-line machine transcript and got ``No se pudo re-sincronizar``.
Every engine in ``_maybe_anchor_align`` declined:

  | engine                       | verdict                                |
  |------------------------------|----------------------------------------|
  | local CTC (wav2vec2) on stem | median word score 0.29 < 0.30          |
  | local CTC on mix / L channel | 0.21 / 0.23                            |
  | hosted ``forced_align``      | declined                               |
  | Whisper-1 word stamps + DP   | 17/40 anchored, 23/40 interpolated      |

Replaying the declined CTC candidate against an independent aligner showed
the 0.30 floor was RIGHT: 23 of its 40 lines were off by more than 0.5 s and
lines 14-19 landed 13-18 s late. So the fix is not a looser threshold — it is
one more *independent* way to look at the audio.

This module is that witness, and it differs from every stage above it:

* It is **forced** alignment — the decoder is constrained to the operator's
  text. ``lyrics_whisper_align`` transcribes freely and then DP-matches what
  came back, which is exactly what collapses on mushy, guitar-heavy folk
  material (17 of 40 lines anchored there).
* Its acoustic backbone is Whisper, not wav2vec2, so a song the CTC model
  cannot read is not automatically lost.
* It **interpolates nothing**: every line gets real word stamps, so the
  ``ANCHOR_MAX_INTERPOLATED_FRAC`` rule the caller applies is satisfied by
  construction rather than by padding.
* It runs locally on CPU (~4 s for a 2:15 song with ``base``) and costs
  nothing per call.

Measured on that job (``base``/``small``/``medium`` agree within ~0.2 s on
most lines): 40/40 lines, 0 crammed, monotonic, median word probability 0.45.
Two negative controls on the SAME audio are rejected by the guards the
caller already runs — a different song's lyric (Color Esperanza) lands
52.6 % crammed lines, non-monotonic, median probability 0.005; the same
lyric duplicated to 80 lines lands 44.3 % crammed, non-monotonic, 0.028.
That order-of-magnitude gap is what ``ANCHOR_LOCAL_ALIGN_MIN_MED_PROB``
guards; the structural checks in the caller catch both cases on their own.

CONTRACT
--------
``local_forced_align(audio_path, lines, *, language, job_id) -> list[dict] | None``

Returns one segment per input line, in order, shaped like every other
engine's output (``{"start", "end", "text", "words": [{"word", "start",
"end", "score"}]}``), or ``None`` when the aligner is unavailable, returns a
different number of lines, or reads as pure noise. The caller stays
responsible for the shared safety verdict (``_safe_alignment`` + the crammed
guard): declining here is never a substitute for it.
"""

from __future__ import annotations

import logging
import os
import statistics
import time

logger = logging.getLogger("uvicorn.error")

_TRUE = {"1", "true", "yes", "on"}

# Whisper ships SHA-256 checksums for its own weights, so the identity to
# pin here is the model NAME: an env var must not be able to point the
# aligner at an arbitrary local checkpoint.
_ALLOWED_MODELS = frozenset({"tiny", "base", "small", "medium"})

_MODEL = None        # lazy singleton: (name, model)


def is_enabled() -> bool:
    return os.environ.get(
        "ANCHOR_LOCAL_ALIGN_ENABLED", "1"
    ).strip().lower() in _TRUE


def model_name() -> str:
    name = os.environ.get("ANCHOR_LOCAL_ALIGN_MODEL", "base").strip() or "base"
    if name not in _ALLOWED_MODELS:
        raise RuntimeError(
            "ANCHOR_LOCAL_ALIGN_MODEL is not an approved model identity"
        )
    return name


def min_median_prob() -> float:
    """Floor under which the alignment is noise, not timing.

    Deliberately far below the healthy case (0.45 measured) and above both
    negative controls (0.005 and 0.028): this is defence in depth, not the
    primary verdict.
    """
    try:
        return float(os.environ.get("ANCHOR_LOCAL_ALIGN_MIN_MED_PROB", "0.05"))
    except (TypeError, ValueError):
        return 0.05


def _load_model():
    """Lazy singleton. Raises if stable-ts/whisper are unavailable —
    callers catch and decline."""
    global _MODEL
    name = model_name()
    if _MODEL is not None and _MODEL[0] == name:
        return _MODEL[1]
    import stable_whisper

    t0 = time.time()
    model = stable_whisper.load_model(name, device="cpu")
    logger.info("[LOCAL-ALIGN] whisper %s loaded in %.1fs", name, time.time() - t0)
    _MODEL = (name, model)
    return model


# The languages the product transcribes (mirrors
# ``transcription_language.SUPPORTED_LANGUAGES``). Audio language ID is only
# allowed to choose among these: Whisper LID on a stem or a guitar intro has
# been seen to answer Nynorsk / Welsh for Spanish songs (3-sep, 9-sep), and a
# forced alignment must never be steered by that.
_FALLBACK_LANGUAGES = ("es", "en", "pt", "fr", "it", "de")

# Seconds of audio per LID window (Whisper's fixed 30 s context) and where in
# the song to take them: a single window at 0 s is often an instrumental
# intro, so average three windows spread across the body of the song.
_LID_WINDOW_POSITIONS = (0.25, 0.5, 0.75)


def _supported_languages() -> tuple[str, ...]:
    try:
        from transcription_language import SUPPORTED_LANGUAGES
    except Exception:  # noqa: BLE001 — the module must stay importable alone
        return _FALLBACK_LANGUAGES
    return tuple(sorted(SUPPORTED_LANGUAGES)) or _FALLBACK_LANGUAGES


def _mel_windows(model, audio_path: str):
    """Batched log-mel windows (n, n_mels, 3000) for Whisper LID, or ``None``."""
    import torch
    import whisper

    audio = whisper.load_audio(audio_path)
    total = len(audio)
    if total == 0:
        return None
    window = whisper.audio.N_SAMPLES
    if total <= window:
        starts = [0]
    else:
        starts = sorted({int((total - window) * f) for f in _LID_WINDOW_POSITIONS})
    mels = [
        whisper.log_mel_spectrogram(
            whisper.pad_or_trim(audio[start:start + window]),
            model.dims.n_mels,
        )
        for start in starts
    ]
    return torch.stack(mels).to(model.device)


def detect_language(model, audio_path: str, *, job_id: str = "") -> str | None:
    """Audio language ID restricted to the product's languages.

    stable-ts refuses to align without a language (``TypeError: expected
    argument for language``) and the lexical detector in
    ``transcription_language`` abstains on most real lyrics — it returns
    nothing for a clean three-line Spanish verse. Staging job daa625bed6f1
    (IKV "DJ Droga", 3-oct) declined on stem and mix for exactly that reason.
    Never raises: ``None`` means "no usable language".
    """
    try:
        mel = _mel_windows(model, audio_path)
        if mel is None:
            return None
        _tokens, probs = model.detect_language(mel)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "[LOCAL-ALIGN] language detection failed: %s: %s (job=%s)",
            type(exc).__name__, str(exc)[:200], job_id,
        )
        return None
    if isinstance(probs, dict):
        probs = [probs]
    allowed = _supported_languages()
    totals = {code: 0.0 for code in allowed}
    for window in probs or []:
        for code in allowed:
            try:
                totals[code] += float(window.get(code, 0.0))
            except (TypeError, ValueError, AttributeError):
                continue
    best = max(allowed, key=lambda code: totals[code])
    if totals[best] <= 0.0:
        return None
    logger.info(
        "[LOCAL-ALIGN] language from audio: %s (mean p=%.2f over %d window(s), job=%s)",
        best, totals[best] / max(len(probs or []), 1), len(probs or []), job_id,
    )
    return best


def detect_audio_language(audio_path: str, *, job_id: str = "") -> str | None:
    """``detect_language`` with the shared model. Callers should pass the
    untouched MIX: LID on a Demucs stem is what answered Nynorsk/English for
    Spanish songs (3-sep). Never raises."""
    if not audio_path or not os.path.exists(audio_path) or not is_enabled():
        return None
    try:
        model = _load_model()
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "[LOCAL-ALIGN] language detection unavailable: %s: %s (job=%s)",
            type(exc).__name__, str(exc)[:200], job_id,
        )
        return None
    return detect_language(model, audio_path, job_id=job_id)


def _segments_of(result) -> list[dict]:
    try:
        data = result.to_dict()
    except AttributeError:
        return []
    return [s for s in (data or {}).get("segments") or [] if isinstance(s, dict)]


def local_forced_align(
    audio_path: str,
    lines: list[str],
    *,
    language: str | None = None,
    job_id: str = "",
) -> list[dict] | None:
    """Force-align ``lines`` onto ``audio_path``; ``None`` means decline."""
    clean = [str(line).strip() for line in (lines or []) if str(line).strip()]
    if not clean or not audio_path or not os.path.exists(audio_path):
        return None
    if not is_enabled():
        return None

    t0 = time.time()
    try:
        model = _load_model()
        # stable-ts raises ``TypeError: expected argument for language`` on a
        # multilingual model when ``language`` is empty, and the caller's
        # lexical detector abstains on most lyrics — so never hand it None.
        language = (str(language).strip().lower() if language else "") or None
        if not language:
            language = detect_language(model, audio_path, job_id=job_id)
        if not language:
            logger.warning(
                "[LOCAL-ALIGN] decline: no language for the aligner (job=%s)",
                job_id,
            )
            return None
        # ``original_split`` keeps the operator's line breaks as the segment
        # boundaries — without it stable-ts re-splits on punctuation and the
        # caller's line-count check rejects the result.
        result = model.align(
            audio_path,
            "\n".join(clean),
            language=language,
            original_split=True,
            verbose=None,
            stream=False,
        )
    except Exception as exc:  # noqa: BLE001 — every failure is a decline
        # The message matters: "TypeError" alone hid a missing language for
        # weeks (job daa625bed6f1). Lyric text never reaches this message.
        logger.warning(
            "[LOCAL-ALIGN] decline on error: %s: %s (job=%s)",
            type(exc).__name__, str(exc)[:200], job_id,
        )
        return None

    raw = _segments_of(result)
    if len(raw) != len(clean):
        logger.warning(
            "[LOCAL-ALIGN] decline: %d segments for %d lines (job=%s)",
            len(raw), len(clean), job_id,
        )
        return None

    segments: list[dict] = []
    probs: list[float] = []
    for line, seg in zip(clean, raw):
        try:
            start = float(seg.get("start"))
            end = float(seg.get("end"))
        except (TypeError, ValueError):
            logger.warning("[LOCAL-ALIGN] decline: unusable span (job=%s)", job_id)
            return None
        words = []
        for word in seg.get("words") or []:
            if not isinstance(word, dict):
                continue
            try:
                prob = float(word.get("probability"))
                w_start = float(word.get("start"))
                w_end = float(word.get("end"))
            except (TypeError, ValueError):
                continue
            probs.append(prob)
            words.append({
                "word": str(word.get("word") or "").strip(),
                "start": round(w_start, 3),
                "end": round(w_end, 3),
                # The shared vocabulary of the cascade: `_apply` flags a line
                # for review when the median of these is below
                # ANCHOR_REVIEW_MIN_SCORE, which is precisely what a
                # low-confidence forced alignment deserves.
                "score": prob,
            })
        segment = {"start": start, "end": end, "text": line}
        if words:
            segment["words"] = words
        segments.append(segment)

    if not probs:
        logger.warning("[LOCAL-ALIGN] decline: no word stamps (job=%s)", job_id)
        return None
    median_prob = statistics.median(probs)
    floor = min_median_prob()
    if median_prob < floor:
        # Whisper will happily force ANY text onto ANY audio; near-zero word
        # probabilities across the whole song mean it sang none of it.
        logger.warning(
            "[LOCAL-ALIGN] decline: median word prob %.3f < %.3f (job=%s)",
            median_prob, floor, job_id,
        )
        return None

    logger.info(
        "[LOCAL-ALIGN] aligned %d lines in %.1fs (median prob %.2f, job=%s)",
        len(segments), time.time() - t0, median_prob, job_id,
    )
    return segments


__all__ = [
    "local_forced_align",
    "detect_language",
    "detect_audio_language",
    "is_enabled",
    "model_name",
    "min_median_prob",
]
