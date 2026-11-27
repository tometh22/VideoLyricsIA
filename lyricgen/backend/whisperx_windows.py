"""Audio-owned, bounded language detection for multilingual music.

WhisperX chooses one language from the first 30 seconds. Never let an intro
make that choice for an entire song. Silence-separated windows are decoded
independently, without a lyric prompt or a language inherited from a neighbor.
"""
from __future__ import annotations

from copy import deepcopy
from itertools import groupby
import logging
import math
import os
import re
import subprocess
import tempfile
import time

logger = logging.getLogger("genly.whisperx.windows")
POLICY = "vocal-windows-v1"
MAX_WINDOW_S = 26.0
MIN_LANGUAGE_PROBABILITY = 0.60
OUTCOME_SCHEMA = "vocal-window-outcome-v1"
# Provider rejections (Replicate 429 while the account is rate-limited) and
# transient network errors are retried on the SAME provider with a wait,
# bounded by attempts and by the shared deadline. They are not silence and
# must not be routed to the whisper-1 language fallback on the first error.
MAX_RETRY_WAIT_S = 30.0
_TRANSIENT_MARKERS = (
    "429", "throttl", "rate limit", "too many requests",
    "timed out", "timeout", "temporarily unavailable", "connection reset",
    "connection aborted", "connection refused", "broken pipe", "reset by peer",
    "remote end closed", "server disconnected", "readerror", "connecterror",
    "remoteprotocolerror", "status: 502", "status: 503", "status: 504",
)


def max_window_attempts() -> int:
    try:
        return max(1, min(10, int(os.environ.get("WHISPERX_WINDOW_MAX_ATTEMPTS", "5"))))
    except (TypeError, ValueError):
        return 5


def _error_code(err) -> str:
    text = f"{type(err).__name__} {err}".lower()
    if "429" in text or "throttl" in text or "rate limit" in text:
        return "rate_limited"
    if any(marker in text for marker in _TRANSIENT_MARKERS):
        return "transient_network"
    return "provider_error"


def _retry_wait_s(err, attempt: int) -> float | None:
    """Seconds to wait before retrying `err`, or None when it is not transient.

    Honours the provider's own hint ("rate limit resets in ~2s"); otherwise a
    short exponential backoff. Always capped by MAX_RETRY_WAIT_S."""
    if err is None or _error_code(err) == "provider_error":
        return None
    hint = re.search(r"resets in ~?\s*(\d+(?:\.\d+)?)\s*s", str(err), re.IGNORECASE)
    wait = float(hint.group(1)) + 1.0 if hint else 2.0 * (2 ** max(0, attempt - 1))
    return min(MAX_RETRY_WAIT_S, max(0.5, wait))
_CHARACTER_LOOP = re.compile(r"([^\W\d_])\1{29,}", re.IGNORECASE)


def _pathological_repetition(row: dict) -> bool:
    """Catch decoder loops that lack word timing, preserving timed refrains."""
    text = str(row.get("text") or "")
    if _CHARACTER_LOOP.search(text):
        return True
    if row.get("words"):
        return False
    try:
        duration = float(row["end"]) - float(row["start"])
    except (KeyError, TypeError, ValueError):
        return False
    if not math.isfinite(duration) or duration <= 0:
        return False
    tokens = re.findall(r"[^\W_]+", text.casefold())
    return any(count >= 20 and count / duration > 4.0
               for count in (sum(1 for _ in group) for _, group in groupby(tokens)))


def plan_windows(regions, duration: float) -> list[tuple[float, float]]:
    """Group voice islands at pauses, preserving quiet/short utterances.

    Padding never overlaps another window. Long continuous vocals are split
    at the last available pause before the limit, or at the limit itself.
    """
    groups = []
    for start, end in sorted(regions):
        if not all(math.isfinite(v) for v in (start, end)):
            continue
        start, end = max(0.0, start), min(duration, end)
        if end <= start:
            continue
        if groups and start - groups[-1][1] <= 2.5 and end - groups[-1][0] <= MAX_WINDOW_S - 0.8:
            groups[-1] = (groups[-1][0], max(end, groups[-1][1]))
        else:
            groups.append((start, end))
    windows = []
    for i, (start, end) in enumerate(groups):
        left = max(0.0, start - 0.4)
        right = min(duration, end + 0.4)
        if i:
            left = max(left, (groups[i - 1][1] + start) / 2)
        if i + 1 < len(groups):
            right = min(right, (end + groups[i + 1][0]) / 2)
        while right - left > MAX_WINDOW_S:
            windows.append((left, left + MAX_WINDOW_S))
            left += MAX_WINDOW_S
        if right > left:
            windows.append((left, right))
    return windows


def audio_windows(path: str) -> list[tuple[float, float]]:
    import librosa
    audio, sr = librosa.load(path, sr=16000, mono=True)
    if not len(audio) or float(max(abs(audio))) < 1e-7:
        return []
    # Sensitive enough to preserve quiet speech over a musical intro. The
    # provider's neural VAD still decides whether these energy islands speak.
    regions = [(float(a) / sr, float(b) / sr)
               for a, b in librosa.effects.split(audio, top_db=40)
               if (b - a) / sr >= 0.15]
    return plan_windows(regions, len(audio) / sr)


def absolute_segments(rows, start: float, end: float) -> list[dict]:
    """Offset every timestamp exactly once; reject invalid provider bounds."""
    result = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        out = deepcopy(row)
        try:
            a, b = float(out["start"]), float(out["end"])
            if not (math.isfinite(a) and math.isfinite(b)
                    and 0 <= a < min(b, end - start) and b <= end - start + 0.25):
                continue
            out.update(start=round(start + a, 3), end=round(min(end, start + b), 3))
        except (ValueError, TypeError, KeyError):
            continue
        words = []
        for word in out.get("words") or []:
            try:
                a, b = float(word["start"]), float(word["end"])
                if (math.isfinite(a) and math.isfinite(b)
                        and 0 <= a <= min(b, end - start) and b <= end - start + 0.25):
                    words.append({**word, "start": round(start + a, 3), "end": round(min(end, start + b), 3)})
            except (ValueError, TypeError, KeyError):
                continue
        out["words"] = words
        result.append(out)
    return result


def _independent_retry(path: str, remaining: float):
    """One bounded, prompt-free recognition, with its own language detection."""
    from openai import OpenAI
    with OpenAI(timeout=min(60.0, remaining), max_retries=0) as client:
        with open(path, "rb") as audio:
            response = client.audio.transcriptions.create(
                model="whisper-1", file=audio, response_format="verbose_json",
                timestamp_granularities=["segment"], temperature=0,
            )
    data = response.model_dump()
    return data.get("segments") or [], data.get("language")


def transcribe_windows(audio_path: str, model: str, payload: dict, budget_s: float) -> dict | None:
    from replicate_budget import call_with_budget
    from recognition_provenance import record_completed, record_window_outcome
    from whisperx_transcribe import _raw_provider_segments
    from pipeline import _is_whisper_hallucination

    deadline = time.monotonic() + budget_s
    windows = audio_windows(audio_path)
    if not windows:
        return None
    combined = []
    # Never carry a global lyric prompt/language into independent windows.
    inputs = {k: v for k, v in payload.items() if k not in {"initial_prompt", "language"}}
    with tempfile.TemporaryDirectory(prefix="whisperx-windows-") as tmp:
        for index, (start, end) in enumerate(windows):
            remaining = deadline - time.monotonic()
            if remaining <= 1:
                raise TimeoutError("WhisperX vocal-window budget exhausted")
            path = os.path.join(tmp, f"{index}.wav")
            subprocess.run([
                "ffmpeg", "-v", "error", "-y", "-ss", str(start), "-i", audio_path,
                "-t", str(end - start), "-ar", "16000", "-ac", "1", path,
            ], check=True, capture_output=True, timeout=min(30, remaining))
            outcome = {
                "schema": OUTCOME_SCHEMA, "policy": POLICY,
                "start": round(start, 3), "end": round(end, 3),
                "attempts": 0, "errors": [],
            }
            output = None
            for attempt in range(1, max_window_attempts() + 1):
                errors: list = []
                outcome["attempts"] = attempt
                output = call_with_budget(
                    model, lambda: {"audio_file": open(path, "rb"), **inputs},
                    total_budget_s=max(0, deadline - time.monotonic()), backoff=[0],
                    call_label="WHISPERX", error_sink=errors,
                )
                if isinstance(output, dict):
                    break
                err = errors[-1] if errors else None
                outcome["errors"].append(_error_code(err) if err is not None else "empty_response")
                wait = _retry_wait_s(err, attempt)
                if wait is None or attempt >= max_window_attempts():
                    break
                if deadline - time.monotonic() <= wait + 1:
                    outcome["errors"].append("deadline")
                    break
                logger.info("[WHISPERX-WINDOW] %.2f-%.2f %s — retry %s in %.1fs",
                            start, end, outcome["errors"][-1], attempt + 1, wait)
                time.sleep(wait)
            failed = not isinstance(output, dict)
            raw = _raw_provider_segments(output) if not failed else []
            language = output.get("detected_language") if not failed else None
            probability = output.get("_language_detection_probability") if not failed else None
            metadata = {"window_start": start, "window_end": end,
                        "detected_language": language, "language_probability": probability}
            if not failed:
                record_completed(
                    family=f"replicate/{model}", events=[{**s, "language_detection": metadata} for s in raw],
                    view="bounded_vocal_window",
                    transformation=f"replicate_raw:offset={start:.3f};end={end:.3f};{POLICY}",
                )
            logger.info("[WHISPERX-WINDOW] %.2f-%.2f language=%s confidence=%s", start, end, language, probability)
            outcome.update(
                whisperx="failed" if failed else "succeeded",
                language=language, language_probability=probability,
                whisperx_rows=len(raw),
            )
            suspicious = bool(raw) and (
                probability is None or probability < MIN_LANGUAGE_PROBABILITY
                or any(_is_whisper_hallucination(str(s.get("text") or "")) for s in raw)
                or not absolute_segments(raw, start, end)
            )
            if suspicious or failed:
                outcome["fallback_reason"] = "whisperx_failed" if failed else (
                    "low_language_confidence"
                    if probability is None or probability < MIN_LANGUAGE_PROBABILITY
                    else "hallucination_or_invalid_bounds"
                )
                remaining = deadline - time.monotonic()
                if remaining <= 1:
                    raise TimeoutError("WhisperX window recovery budget exhausted")
                try:
                    retry, retry_language = _independent_retry(path, remaining)
                except Exception as exc:
                    raise RuntimeError("Vocal window recovery failed; refusing partial transcription") from exc
                outcome.update(fallback="openai/whisper-1", fallback_language=retry_language,
                               fallback_rows=len(retry or []))
                record_completed(
                    family="openai/whisper-1", events=retry, view="bounded_vocal_window",
                    transformation=f"language_retry_raw:offset={start:.3f};end={end:.3f};language={retry_language}",
                )
                if any(_pathological_repetition(row) for row in retry):
                    original = absolute_segments(raw, start, end)
                    if (not original or len(original) != len(raw) or any(
                        not row.get("words") or _pathological_repetition(row)
                        or _is_whisper_hallucination(str(row.get("text") or ""))
                        for row in original
                    )):
                        raise RuntimeError("Vocal window recovery produced a repetition loop; refusing partial transcription")
                    logger.warning("[WHISPERX-WINDOW] structurally invalid retry at %.2f-%.2f; keeping timed evidence", start, end)
                    outcome["source"] = "whisperx_kept_over_looping_fallback"
                else:
                    raw = retry
                    outcome["source"] = "whisper-1"
            else:
                outcome["source"] = "whisperx"
            # Credits generated on silence are not lyrics. Empty neural-VAD
            # windows are legitimate; transport errors above are not silence.
            before_filter = len(raw)
            raw = [s for s in raw if not _is_whisper_hallucination(str(s.get("text") or ""))]
            accepted = absolute_segments(raw, start, end)
            combined.extend(accepted)
            outcome["hallucinations_discarded"] = before_filter - len(raw)
            outcome["accepted_rows"] = len(accepted)
            outcome["decision"] = _window_decision(outcome)
            record_window_outcome(outcome)
    return {"segments": combined, "_windowed": True}


def _window_decision(outcome: dict) -> str:
    """accepted / no_voice / unrecognized.

    A window is `unrecognized` (an incomplete transcription) when nothing
    usable came back although the provider never confirmed silence: WhisperX
    failed after its retries, or every row heard was discarded. A successful
    WhisperX call with no rows is the provider's VAD saying there is no voice.
    """
    if int(outcome.get("accepted_rows") or 0) > 0:
        return "accepted"
    if outcome.get("whisperx") == "succeeded" and not int(outcome.get("whisperx_rows") or 0):
        return "no_voice"
    return "unrecognized"


def unrecognized_windows(outcomes) -> list[dict]:
    """Review windows for vocal windows that ended without any transcription."""
    rows = []
    for item in outcomes or []:
        if not isinstance(item, dict) or item.get("decision") != "unrecognized":
            continue
        try:
            start, end = float(item["start"]), float(item["end"])
        except (KeyError, TypeError, ValueError):
            continue
        if math.isfinite(start) and math.isfinite(end) and end > start:
            rows.append({
                "start": round(start, 3), "end": round(end, 3),
                "reason": "vocal_window_unrecognized",
                "attempts": int(item.get("attempts") or 0),
                "errors": list(item.get("errors") or [])[:10],
            })
    return rows


def recover_reference_conflict(audio_path: str, segments: list[dict], reference: str,
                               budget_s: float = 120.0) -> list[dict]:
    """An independent audio reference may trigger another listen, never a copy.

    Only replace a whole candidate when the independent, prompt-free ASR
    improves both discrepancy and reference attestation without losing text
    coverage. Rejected attempts remain in machine evidence. Exceptions or an
    exhausted budget retain the original, which still faces the quality gate.
    Caller must restrict this to a reference derived from this complete audio.
    """
    from transcription_language import reference_divergence, localized_reference_drift
    from reference_attestation import assess_reference_attestation
    from recognition_provenance import record_completed
    from pipeline import _is_whisper_hallucination

    before = reference_divergence(segments, reference)
    local = localized_reference_drift(segments, reference)
    if not local and not (before["substantial"] >= 3 and before["ratio"] >= 0.34):
        return segments
    suspect = set(local) | set(before["unexplained_indices"])
    deadline = time.monotonic() + budget_s
    try:
        planned = audio_windows(audio_path)
        selected = [(a, b) for a, b in planned if any(
            float(segments[i]["start"]) < b and float(segments[i]["end"]) > a
            for i in suspect
        )]
        if not selected:
            return segments
        # Replacement is atomic at segment granularity. A valid phrase may
        # straddle a selected boundary; expand to its other vocal windows so
        # replacing the intersecting row cannot silently discard its tail.
        while True:
            touched = [s for s in segments if any(
                float(s["start"]) < b and float(s["end"]) > a for a, b in selected
            )]
            expanded = [(a, b) for a, b in planned if any(
                float(s["start"]) < b and float(s["end"]) > a for s in touched
            )]
            if expanded == selected:
                break
            selected = expanded
        candidate = [s for s in segments if not any(
            float(s["start"]) < b and float(s["end"]) > a for a, b in selected
        )]
        with tempfile.TemporaryDirectory(prefix="asr-language-recovery-") as tmp:
            for index, (start, end) in enumerate(selected):
                remaining = deadline - time.monotonic()
                if remaining <= 1:
                    return segments
                path = os.path.join(tmp, f"{index}.wav")
                subprocess.run([
                    "ffmpeg", "-v", "error", "-y", "-ss", str(start), "-i", audio_path,
                    "-t", str(end - start), "-ar", "16000", "-ac", "1", path,
                ], check=True, capture_output=True, timeout=min(30, remaining))
                remaining = deadline - time.monotonic()
                if remaining <= 1:
                    return segments
                raw, language = _independent_retry(path, remaining)
                record_completed(
                    family="openai/whisper-1", events=raw, view="bounded_vocal_window",
                    transformation=f"reference_conflict_retry_raw:offset={start:.3f};end={end:.3f};language={language}",
                )
                candidate.extend(absolute_segments([
                    s for s in raw if not _is_whisper_hallucination(str(s.get("text") or ""))
                ], start, end))
        candidate.sort(key=lambda s: s["start"])
        after = reference_divergence(candidate, reference)
        original_score = assess_reference_attestation(reference, segments)["metrics"]
        candidate_score = assess_reference_attestation(reference, candidate)["metrics"]
        improved = (
            after["ratio"] < before["ratio"]
            and candidate_score["attestation_score"] >= original_score["attestation_score"] + 0.08
            and candidate_score["reference_token_coverage"] >= original_score["reference_token_coverage"]
            and candidate_score["asr_token_count"] >= original_score["asr_token_count"] * 0.8
        )
        logger.info("[WHISPERX-RECOVERY] windows=%d accepted=%s divergence=%.2f->%.2f",
                    len(selected), improved, before["ratio"], after["ratio"])
        return candidate if improved else segments
    except Exception as exc:
        logger.warning("[WHISPERX-RECOVERY] independent retry failed (%s); retaining quality review", type(exc).__name__)
        return segments
