"""WhisperX transcription — word-level timestamps for the NO-LYRICS path.

WHY
---
When we have no known lyrics (lrclib 404, Gemini empty) the audio is the only
source of truth. OpenAI's whisper-1 gives segment-level timing (±500ms) and
interpolates word times. WhisperX (Whisper large-v2 + wav2vec2 phoneme forced
alignment + VAD) pins each word to <100ms and its VAD makes it far less prone to
the single-mega-segment hallucination. This is the engine behind Rotor-grade
"transcribe a hard song with no lyrics and it still lands". Run on Replicate so
there's no torch/GPU on the workers (same plumbing as `forced_align.py`).

CONTRACT
--------
- Behind `WHISPERX_ENABLED` (default off) + `REPLICATE_API_TOKEN`.
- `transcribe_whisperx(audio_path, language=None) -> list[dict] | None` returns
  `[{"start","end","text","words":[{"word","start","end"}]}]` aligned to the
  audio, or **None** on any failure / when disabled / when the result looks
  empty. NEVER raises — the caller falls back to whisper-1.
- `_map_segments` is pure (no network) and unit-testable.
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger("genly.whisperx")

_TRUE = ("1", "true", "yes", "on", "y", "t")

# Replicate whisperX model. Override via env once a version is verified for
# prod. NOTE: pin to a known-good version hash before enabling in production.
_MODEL = os.environ.get(
    "WHISPERX_MODEL",
    # Verified live 2026-05-22 via Replicate API (account tometh22).
    "victor-upmeet/whisperx:655845d6190ef70573c669245f245892cd039df4b880a1e3a65852c09252f5cc",
)


def is_enabled() -> bool:
    """On only when the flag is set AND a Replicate token is present."""
    flag = os.environ.get("WHISPERX_ENABLED", "0").strip().lower() in _TRUE
    return flag and bool(os.environ.get("REPLICATE_API_TOKEN", "").strip())


def _map_segments(output) -> list[dict]:
    """Map whisperX output to our segment shape. Pure + testable.

    whisperX returns {"segments": [{"start","end","text",
    "words":[{"word","start","end","score"}]}], ...}. We keep line-level
    start/end/text and carry per-word stamps (for a future word-level editor).
    Drops segments with no usable text or non-numeric bounds.
    """
    if isinstance(output, dict):
        raw = output.get("segments") or []
    elif isinstance(output, list):
        raw = output
    else:
        return []

    segs: list[dict] = []
    for s in raw:
        if not isinstance(s, dict):
            continue
        text = (s.get("text") or "").strip()
        if not text:
            continue
        try:
            start = float(s.get("start"))
            end = float(s.get("end"))
        except (TypeError, ValueError):
            continue
        if end < start:
            end = start
        words = []
        for w in (s.get("words") or []):
            if not isinstance(w, dict):
                continue
            wt = (w.get("word") or w.get("text") or "").strip()
            try:
                ws = float(w.get("start"))
                we = float(w.get("end"))
            except (TypeError, ValueError):
                continue  # whisperX omits stamps for non-alignable tokens
            if wt:
                # Preserve `score` (whisperX confidence per word, 0-1) so
                # the editor can render low-confidence lines tinted red.
                w_out = {"word": wt, "start": ws, "end": we}
                try:
                    score = w.get("score")
                    if score is not None:
                        w_out["score"] = float(score)
                except (TypeError, ValueError):
                    pass
                words.append(w_out)
        seg = {"start": start, "end": end, "text": text}
        if words:
            seg["words"] = words
        segs.append(seg)
    return segs


def _filter_ghosts(segs: list[dict]) -> list[dict]:
    """Drop suspicious tiny segments: whisperX occasionally tags an
    instrumental flourish or breath as a 1-word segment (real example on
    El Arbol intro: `'Amén'` at 5.15s for 0.18s). Anything <0.5s AND
    <2 words is treated as a ghost. Single-word holds longer than 0.5s
    (e.g., a chanted name) are kept. Pure + testable."""
    out: list[dict] = []
    for s in segs:
        try:
            dur = float(s.get("end", 0)) - float(s.get("start", 0))
        except (TypeError, ValueError):
            dur = 0.0
        words = len((s.get("text") or "").split())
        if dur < 0.5 and words < 2:
            continue
        out.append(s)
    return out


def _split_long_segments(segs: list[dict], *, max_dur: float | None = None,
                          min_split_gap: float = 0.0) -> list[dict]:
    """Split segments longer than `max_dur` at the largest internal
    word-to-word gap, recursively, so subtitles aren't long walls of text.
    Requires per-word stamps (whisperX provides them with `align_output=True`);
    segments without words are left untouched.

    The split point is the BIGGEST gap > `min_split_gap`. When a segment is
    longer than max_dur we split at whatever pause exists, even tiny ones —
    Rotor lines run ~3-5s and whisperX's native segments are 8-25s, so we
    have to be aggressive (incident "El Arbol": an 8.7s line that wasn't
    splitting at the natural phrase boundary between "calles" and "La"
    because min_split_gap=0.3 was too strict; the actual pause whisperX
    measured there was ~0.15s).

    Defaults are env-tunable via `WHISPERX_MAX_LINE_S` (default 6.0) and
    `WHISPERX_MIN_SPLIT_GAP_S` (default 0.0). Pure + testable.
    """
    if max_dur is None:
        try:
            max_dur = float(os.environ.get("WHISPERX_MAX_LINE_S", "6.0"))
        except (TypeError, ValueError):
            max_dur = 6.0
    try:
        min_split_gap = float(os.environ.get("WHISPERX_MIN_SPLIT_GAP_S", min_split_gap))
    except (TypeError, ValueError):
        pass
    if max_dur <= 0:
        return segs

    def _split_once(seg: dict) -> list[dict]:
        words = seg.get("words") or []
        if len(words) < 4:
            return [seg]
        try:
            start = float(seg["start"]); end = float(seg["end"])
        except (TypeError, ValueError, KeyError):
            return [seg]
        if (end - start) <= max_dur:
            return [seg]
        # Long enough to require a split: find biggest gap > min_split_gap.
        best_i, best_gap = -1, -1.0
        for i in range(len(words) - 1):
            try:
                gap = float(words[i + 1]["start"]) - float(words[i]["end"])
            except (TypeError, ValueError, KeyError):
                continue
            if gap > best_gap and gap > min_split_gap:
                best_gap, best_i = gap, i
        if best_i < 0:
            return [seg]   # no usable gap structure (back-to-back words)
        # Split AT the gap: left = words[:best_i+1], right = words[best_i+1:]
        left_words = words[: best_i + 1]
        right_words = words[best_i + 1:]
        left = {
            "start": float(left_words[0]["start"]),
            "end": float(left_words[-1]["end"]),
            "text": " ".join(w.get("word", "").strip() for w in left_words if w.get("word")),
            "words": left_words,
        }
        right = {
            "start": float(right_words[0]["start"]),
            "end": float(right_words[-1]["end"]),
            "text": " ".join(w.get("word", "").strip() for w in right_words if w.get("word")),
            "words": right_words,
        }
        # Recurse on each half (depth-bounded by shrinking duration).
        return _split_once(left) + _split_once(right)

    out: list[dict] = []
    for s in segs:
        out.extend(_split_once(s))
    return out


def _apply_lead_in(segs: list[dict], *, lead_ms: int | None = None) -> list[dict]:
    """Pull each segment's start time earlier by `lead_ms` so the subtitle
    appears slightly before the singer enters that line — the karaoke
    "anticipation" effect. WhisperX timestamps are ±100ms accurate and
    musicians lean a touch behind/ahead of the beat, so without lead-in the
    line often lands a few ms after the voice (which the user perceives as
    "delay").

    Clamps so we never go below 0 or overlap the previous segment's end.
    Per-word stamps inside `segs[i]["words"]` are NOT shifted (they stay
    truthful to the audio; only the line's display window moves).

    Default 120ms, env-tunable via `LYRIC_LEAD_IN_MS`. Pure + testable.
    """
    if lead_ms is None:
        try:
            lead_ms = int(os.environ.get("LYRIC_LEAD_IN_MS", "120"))
        except (TypeError, ValueError):
            lead_ms = 120
    if lead_ms <= 0 or not segs:
        return segs
    lead_s = lead_ms / 1000.0
    out: list[dict] = []
    prev_end: float | None = None       # None until we've seen one segment
    for s in segs:
        try:
            start = float(s.get("start", 0))
        except (TypeError, ValueError):
            out.append(s); continue
        # Floor at prev_end+5ms (no overlap with prior) and at 0 (no negative).
        floor = 0.0 if prev_end is None else max(0.0, prev_end + 0.005)
        new_start = max(start - lead_s, floor)
        new_start = min(new_start, start)   # never push later
        new_seg = dict(s)
        new_seg["start"] = new_start
        out.append(new_seg)
        try:
            prev_end = float(s.get("end", new_start))
        except (TypeError, ValueError):
            prev_end = new_start
    return out


def transcribe_whisperx(audio_path: str, language: str | None = None,
                        lyrics_hint: str | None = None) -> list[dict] | None:
    """Transcribe `audio_path` with whisperX. Returns segments with word
    stamps, or None (disabled / failure / empty). Never raises.

    `lyrics_hint`: optional reference text (lrclib / Genius / Gemini plain
    lyrics). When provided, the first ~800 chars are passed as Whisper's
    `initial_prompt` — biases the model's vocabulary towards the actual
    lyrics so it stops mishearing (e.g. "Legalícenla" instead of
    "Le realizan la"). Costs nothing extra — Whisper-large-v3 already
    accepts the prompt parameter; we just weren't using it.

    INCIDENT 2026-05-25: PROD without this hint produced ["Le realizan
    la"] × 4 in the intro of "Legalícenla" because the audio is
    phonemically ambiguous on a heavy electric guitar mix. With the
    lrclib plain text as prompt, the model expects "Legalícenla" and
    locks onto the right interpretation.
    """
    if not is_enabled():
        return None
    if not audio_path or not os.path.exists(audio_path):
        return None

    try:
        import replicate  # noqa: F401 — used inside `call_with_budget`
    except ImportError:
        logger.warning("[WHISPERX] replicate SDK not installed — falling back")
        return None

    payload: dict = {"align_output": True}
    if language:
        payload["language"] = language

    # Lyrics-aware prompting. The Replicate whisperX model
    # (victor-upmeet/whisperx) accepts `initial_prompt`, forwarded to the
    # underlying faster-whisper.
    #
    # EMPIRICAL FINDING 2026-05-25 (live A/B against Legalícenla on
    # Replicate — see scripts/scratch/test_prompt_variants.py):
    #   - LONG prompt (full lyrics, 800 chars): HURTS. whisperX collapses
    #     to 2 distorted segments because the model parrots prompt
    #     vocabulary across all audio. First seg displaced by 30+ seconds.
    #   - SHORT prompt (~22-120 chars, 2-4 seed lines): WORKS. "Legalícenla.
    #     Oh-oh-oh." (22 chars) produced 8 well-aligned segments with
    #     "Legalícenla" correctly recognised — vs 9 segments with the
    #     wrong "Legalízala" without any hint.
    #
    # Strategy: take the first 2-4 non-empty lines of the reference
    # (typically the chorus / opening — densest in distinctive words),
    # joined with ". " and capped at 120 chars total. Enough vocab to
    # bias without saturating attention.
    if lyrics_hint and lyrics_hint.strip():
        lines = [ln.strip() for ln in lyrics_hint.splitlines() if ln.strip()]
        seed_lines: list[str] = []
        running_chars = 0
        for ln in lines:
            if running_chars + len(ln) > 120 and seed_lines:
                break
            seed_lines.append(ln)
            running_chars += len(ln) + 2          # +2 for ". " joiner
            if len(seed_lines) >= 4:
                break
        prompt_text = ". ".join(seed_lines)[:120]
        payload["initial_prompt"] = prompt_text
        logger.info("[WHISPERX] initial_prompt primed: %s chars (%s seed lines) — %r",
                    len(prompt_text), len(seed_lines), prompt_text[:60])

    # OBSERVABILITY (audit 2026-05-24): whisperX USED to be a single
    # `replicate.run(...)` with no budget cap. When Replicate degraded,
    # the request could hang ~90 min before timing out — same shape as
    # the forced_align bug fixed in PR #281 but on a different code
    # path. Extracted to `replicate_budget.call_with_budget` so all
    # three Replicate consumers (FA, demucs, whisperX) share the same
    # bounded retry + typed-error short-circuit.
    from replicate_budget import call_with_budget, _budget_for

    def _input_factory():
        return {"audio_file": open(audio_path, "rb"), **payload}

    output = call_with_budget(
        _MODEL, _input_factory,
        total_budget_s=_budget_for("whisperx", default_s=480.0),
        backoff=[0, 8, 24],
        call_label="WHISPERX",
    )
    if output is None:
        return None

    segs = _map_segments(output)
    raw_n = len(segs)
    segs = _filter_ghosts(segs)
    segs = _split_long_segments(segs)
    segs = _apply_lead_in(segs)
    if len(segs) < 2:
        logger.warning("[WHISPERX] thin/empty result (%s raw -> %s usable) — falling back",
                       raw_n, len(segs))
        return None
    logger.info("[WHISPERX] transcribed %s segments (%s raw, after ghost-filter + split)", len(segs), raw_n)
    return segs
