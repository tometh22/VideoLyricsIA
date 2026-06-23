"""Lyrics text formatting pass — orthographic correction via LLM.

Runs on the FINAL segment list (after all ASR, reconcile, and CTC passes)
to fix accents, capitalization, and punctuation without changing words or
timestamps.

Why GPT-4o-mini at temperature=0:
  Cheap (< $0.001 per song), deterministic, and extremely reliable at
  orthographic correction when the task is tightly scoped. The whisperX
  transcript gives correct words but routinely drops accents (fragil vs.
  frágil), lowercase first-words, and missing ¿/¡ markers. This pass
  fixes those without touching timing or word identity.

Failure mode: any exception (OpenAI unavailable, count mismatch, etc.)
returns the original result unchanged — never blocks the pipeline.
"""

import asyncio
import logging
import os
import re

logger = logging.getLogger("genly.lyrics_format")

_LANG_NAMES: dict = {
    "es": "Spanish", "spa": "Spanish",
    "en": "English",  "eng": "English",
    "pt": "Portuguese", "por": "Portuguese",
    "fr": "French",   "fra": "French",
    "it": "Italian",  "ita": "Italian",
    "de": "German",   "deu": "German",
}


def _lang_name(language: str) -> str:
    return _LANG_NAMES.get((language or "es").lower()[:3], "Spanish")


async def format_lyrics_pass(result: dict, language: str = "es") -> dict:
    """Orthographic correction on the final segment list.

    Only touches `seg["text"]` — start/end/words stamps are preserved.
    Returns result unchanged on any failure or when disabled.
    """
    if os.environ.get("LYRICS_FORMAT_ENABLED", "1").strip().lower() in (
        "0", "false", "off", "no"
    ):
        return result

    if not isinstance(result, dict):
        return result

    segs = result.get("segments") or []
    if not segs:
        return result

    texts = [(s.get("text") or "").strip() for s in segs]
    if not any(texts):
        return result

    lang = _lang_name(language)
    numbered = "\n".join(f"{i + 1}. {t}" for i, t in enumerate(texts))

    prompt = (
        f"You are correcting lyrics transcription orthography for a {lang} song.\n\n"
        "Rules:\n"
        f"- Fix {lang} accents and diacritics where phonetically correct "
        f"(e.g. fragil→frágil, tu→tú when stressed, mas→más, etc.)\n"
        "- Capitalize the first word of each line\n"
        "- Fix obvious punctuation (commas, periods, ellipsis)\n"
        "- For Spanish: add inverted opening marks (¿, ¡) where the line is a question or exclamation\n"
        "- Do NOT change, add, or remove any words — only spelling / accents / punctuation\n"
        f"- Return EXACTLY {len(texts)} lines in the same '1. text' format\n\n"
        f"Input ({len(texts)} lines):\n{numbered}\n\n"
        f"Output ({len(texts)} lines):"
    )

    try:
        from openai import AsyncOpenAI
        client = AsyncOpenAI()

        resp = await asyncio.wait_for(
            client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[{"role": "user", "content": prompt}],
                temperature=0,
                max_tokens=max(len(numbered) + 300, 500),
            ),
            timeout=30,
        )

        raw = (resp.choices[0].message.content or "").strip()

        # Parse "N. text" lines; skip blank lines
        lines: list = []
        for line in raw.split("\n"):
            line = line.strip()
            if not line:
                continue
            m = re.match(r"^\d+\.\s*(.+)$", line)
            lines.append(m.group(1).strip() if m else line)

        if len(lines) != len(segs):
            logger.warning(
                "[FORMAT] count mismatch: expected %d got %d — skipping",
                len(segs), len(lines),
            )
            return result

        new_segs = []
        for seg, new_text in zip(segs, lines):
            old_text = (seg.get("text") or "").strip()
            new_segs.append({**seg, "text": new_text} if new_text and new_text != old_text else seg)

        changed = sum(1 for s, n in zip(segs, new_segs) if s is not n)
        logger.info("[FORMAT] %s ortho pass: %d/%d lines corrected", lang, changed, len(segs))

        result = dict(result)
        result["segments"] = new_segs
        return result

    except Exception as exc:
        logger.warning("[FORMAT] pass failed: %r — returning original", exc)
        return result
