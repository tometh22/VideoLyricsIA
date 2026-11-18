"""Audio-owned, bounded language detection for multilingual music.

WhisperX chooses one language from the first 30 seconds. Never let an intro
make that choice for an entire song. Silence-separated windows are decoded
independently, without a lyric prompt or a language inherited from a neighbor.
"""
from __future__ import annotations

from copy import deepcopy
import logging
import math
import os
import subprocess
import tempfile
import time

logger = logging.getLogger("genly.whisperx.windows")
POLICY = "vocal-windows-v1"
MAX_WINDOW_S = 26.0
MIN_LANGUAGE_PROBABILITY = 0.60


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
    from recognition_provenance import record_completed
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
            output = call_with_budget(
                model, lambda: {"audio_file": open(path, "rb"), **inputs},
                total_budget_s=max(0, deadline - time.monotonic()), backoff=[0],
                call_label="WHISPERX",
            )
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
            suspicious = bool(raw) and (
                probability is None or probability < MIN_LANGUAGE_PROBABILITY
                or any(_is_whisper_hallucination(str(s.get("text") or "")) for s in raw)
                or not absolute_segments(raw, start, end)
            )
            if suspicious or failed:
                remaining = deadline - time.monotonic()
                if remaining <= 1:
                    raise TimeoutError("WhisperX window recovery budget exhausted")
                try:
                    retry, retry_language = _independent_retry(path, remaining)
                except Exception as exc:
                    raise RuntimeError("Vocal window recovery failed; refusing partial transcription") from exc
                record_completed(
                    family="openai/whisper-1", events=retry, view="bounded_vocal_window",
                    transformation=f"language_retry_raw:offset={start:.3f};end={end:.3f};language={retry_language}",
                )
                raw = retry
            # Credits generated on silence are not lyrics. Empty neural-VAD
            # windows are legitimate; transport errors above are not silence.
            raw = [s for s in raw if not _is_whisper_hallucination(str(s.get("text") or ""))]
            combined.extend(absolute_segments(raw, start, end))
    return {"segments": combined, "_windowed": True}


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
