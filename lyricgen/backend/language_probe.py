"""Independent, lyric-free confirmation of the language that is SUNG.

WhisperX decides one language from the first 30 seconds. When that guess is
wrong (a spoken intro, a sample, a quiet start) Whisper does not transcribe the
Spanish verses: it translates them into the guessed language. Incident
2026-11-18, campaign 6637d82e93dd: "Gran Ídolo De Asia" and four more songs
reached the editor half in English, and no alert fired because the only
language checks need a reference lyric that Gemini had not returned.

This probe asks Gemini ONE question about the complete recording: which
language is sung in each part. It never asks for lyrics (so it cannot recite a
catalogue text, and no lyric leaves this module), never forces a language on the
ASR, and never rewrites text. Its answer is persisted on the job and compared
line by line with the delivered lyrics by ``transcription_language``.

Backtest on that campaign (20 songs, ~US$0.01 each): 5/5 translated songs
reported as Spanish, 12/12 controls right (English, Spanish and a bilingual
en+es song), instrumental tracks reported as no voice.
"""
from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import tempfile
import time

from transcription_language import SUPPORTED_LANGUAGES

logger = logging.getLogger("genly.language_probe")

SCHEMA = "sung-language-probe-v1"
MODEL = "gemini-2.5-flash"
_MAX_SECTIONS = 24

_PROMPT = (
    "Listen to the complete recording. Do NOT transcribe, quote or translate "
    "any lyrics. Identify which language is SUNG or spoken in each part of "
    "the recording. Merge consecutive parts that share a language and report "
    "at most 12 parts. Use ISO-639-1 codes, or \"none\" for instrumental "
    "parts. Return only JSON with this shape: "
    '{"main_language":"<code or none>","sections":'
    '[{"start_s":0,"end_s":30,"language":"<code or none>"}]}'
)

_CODE = re.compile(r"^[a-z]{2,3}$")


def enabled() -> bool:
    return os.environ.get("SUNG_LANGUAGE_PROBE_ENABLED", "1").strip().lower() in {
        "1", "true", "yes", "on",
    }


def _unavailable(reason: str) -> dict:
    return {"schema": SCHEMA, "source": "gemini", "model": MODEL,
            "status": "unavailable", "reason": reason,
            "main_language": None, "languages": [], "sections": []}


def _code(value) -> str | None:
    code = str(value or "").strip().lower().replace("_", "-").split("-", 1)[0]
    if code in {"none", "instrumental", "null", ""}:
        return "none"
    return code if _CODE.match(code) else None


# Gemini finish reasons that mean the model ended its answer on purpose. Any
# other reason (MAX_TOKENS, SAFETY, RECITATION, ...) may have cut the sections
# list short, so the languages it lists are not exhaustive.
_COMPLETE_FINISH_REASONS = frozenset({"", "STOP", "FINISH_REASON_UNSPECIFIED"})


def finish_reason_name(response) -> str:
    """``FinishReason.MAX_TOKENS`` / ``"MAX_TOKENS"`` -> ``"MAX_TOKENS"``."""
    try:
        candidate = (getattr(response, "candidates", None) or [None])[0]
    except (TypeError, IndexError):
        candidate = None
    reason = getattr(candidate, "finish_reason", None) if candidate is not None else None
    if reason is None:
        return ""
    name = getattr(reason, "name", None) or str(reason)
    return name.rsplit(".", 1)[-1].strip().upper()


def parse_probe_response(text: str, finish_reason: str | None = None) -> dict:
    """Normalize Gemini's JSON answer into the persisted probe contract.

    A truncated answer (unparseable JSON, or any finish reason other than a
    natural stop, e.g. MAX_TOKENS) keeps the sections it recovered for display
    but is marked ``status="incomplete"`` / ``complete=False``: a language that
    was cut off the list is NOT evidence that it is absent, so the line check
    must abstain instead of flagging verses in it.
    """
    raw = (text or "").strip()
    data = None
    finish = str(finish_reason or "").strip().upper()
    incomplete_reason = (
        None if finish in _COMPLETE_FINISH_REASONS else f"finish_{finish.lower()}"
    )
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        incomplete_reason = incomplete_reason or "truncated_response"
        main = re.search(r'"main_language"\s*:\s*"([^"]*)"', raw)
        sections = [
            {"start_s": float(a), "end_s": float(b), "language": c}
            for a, b, c in re.findall(
                r'\{\s*"start_s"\s*:\s*([\d.]+)\s*,\s*"end_s"\s*:\s*([\d.]+)\s*,'
                r'\s*"language"\s*:\s*"([^"]*)"\s*\}', raw)
        ]
        if main or sections:
            data = {"main_language": main.group(1) if main else None,
                    "sections": sections}
    if not isinstance(data, dict):
        return _unavailable("unparseable_response")

    sections = []
    for item in (data.get("sections") or [])[:_MAX_SECTIONS]:
        if not isinstance(item, dict):
            continue
        code = _code(item.get("language"))
        if code is None:
            continue
        try:
            start, end = float(item.get("start_s")), float(item.get("end_s"))
        except (TypeError, ValueError):
            start = end = None
        sections.append({"start_s": start, "end_s": end, "language": code})
    main = _code(data.get("main_language"))
    languages = sorted({s["language"] for s in sections} - {"none"})
    if main and main != "none" and main not in languages:
        languages = sorted({*languages, main})
    status = "ok" if languages else ("no_vocals" if main == "none" or sections else "unavailable")
    if incomplete_reason and status != "unavailable":
        status = "incomplete"
    return {
        "schema": SCHEMA, "source": "gemini", "model": MODEL,
        "status": status,
        "complete": incomplete_reason is None,
        "incomplete_reason": incomplete_reason,
        "main_language": main if main and main != "none" else None,
        "languages": languages,
        # Unsupported codes are kept: they make the line check abstain instead
        # of coercing, e.g., a Catalan verse into "foreign to Spanish".
        "unsupported_languages": sorted(set(languages) - SUPPORTED_LANGUAGES),
        "sections": sections,
    }


def _compact_audio(audio_path: str, tmp_dir: str) -> str:
    """Mono 16 kHz MP3: a 5-minute song is ~2 MB, far below the inline limit."""
    out = os.path.join(tmp_dir, "probe.mp3")
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-i", audio_path, "-ac", "1",
         "-ar", "16000", "-b:a", "48k", out],
        check=True, capture_output=True, timeout=120,
    )
    return out


def _is_quota_error(exc: BaseException) -> bool:
    text = f"{type(exc).__name__} {exc}"
    return "429" in text or "RESOURCE_EXHAUSTED" in text


def probe_sung_languages(audio_path: str, *, timeout_s: float = 60.0,
                         backoff: tuple[float, ...] = (0.0, 8.0, 25.0)) -> dict:
    """Return the persisted probe dict; never raises.

    Quota errors (429) are retried: a 182-song campaign saturates Vertex and
    the batch reference call had silently returned nothing for 62 songs.
    """
    if not enabled():
        return _unavailable("disabled")
    if not audio_path or not os.path.exists(audio_path):
        return _unavailable("audio_missing")
    try:
        from google import genai
        from pipeline import _call_with_timeout, _get_genai_client
        client = _get_genai_client()
    except Exception as exc:
        logger.warning("[LANGUAGE-PROBE] client unavailable: %s", type(exc).__name__)
        return _unavailable("client_unavailable")

    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="language-probe-") as tmp_dir:
        try:
            compact = _compact_audio(audio_path, tmp_dir)
            with open(compact, "rb") as handle:
                audio_bytes = handle.read()
        except Exception as exc:
            logger.warning("[LANGUAGE-PROBE] audio prep failed: %s", type(exc).__name__)
            return _unavailable("audio_prep_failed")
        contents = [
            genai.types.Part.from_bytes(data=audio_bytes, mime_type="audio/mpeg"),
            genai.types.Part.from_text(text=_PROMPT),
        ]
        config = genai.types.GenerateContentConfig(
            temperature=0.0, max_output_tokens=1500,
            response_mime_type="application/json",
            thinking_config=genai.types.ThinkingConfig(thinking_budget=0),
        )
        last_reason = "call_failed"
        for attempt, delay in enumerate(backoff):
            if delay:
                time.sleep(delay)
            try:
                response = _call_with_timeout(
                    lambda: client.models.generate_content(
                        model=MODEL, contents=contents, config=config,
                    ),
                    timeout_s=float(timeout_s), label="LANGUAGE-PROBE",
                )
            except Exception as exc:
                last_reason = "quota_exhausted" if _is_quota_error(exc) else "call_failed"
                logger.warning("[LANGUAGE-PROBE] attempt %d failed: %s",
                               attempt + 1, last_reason)
                if last_reason != "quota_exhausted":
                    break
                continue
            probe = parse_probe_response(
                getattr(response, "text", "") or "",
                finish_reason=finish_reason_name(response),
            )
            probe["attempts"] = attempt + 1
            probe["elapsed_s"] = round(time.monotonic() - started, 2)
            logger.info("[LANGUAGE-PROBE] status=%s main=%s languages=%s",
                        probe["status"], probe["main_language"], probe["languages"])
            return probe
    probe = _unavailable(last_reason)
    probe["attempts"] = len(backoff)
    return probe


__all__ = ["SCHEMA", "enabled", "finish_reason_name", "parse_probe_response", "probe_sung_languages"]
