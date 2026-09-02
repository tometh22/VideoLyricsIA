"""Lyrics text formatting pass — orthographic correction + line splitting via LLM.

Two things Rotor does that we don't (but now do):
  1. Orthographic correction: fix accents, capitalization, ¿/¡, punctuation.
  2. Line splitting: break long whisperX segments at musical-phrase boundaries.

Both happen in a single GPT-4o-mini call (temperature=0, < $0.001/song).

Timestamp assignment for splits:
  - If word-level timestamps are present (whisperX forced-alignment), we assign
    each sub-line's start/end from the proportional slice of the word list.
    Proportional by sub-line word count gives ±1 word accuracy, which is
    < 0.3s for typical vocal tempo.
  - If word timestamps are absent (adlib or ghost segments), we fall back to
    proportional split by character count.

Failure mode: any exception returns the original result unchanged.
"""

import asyncio
import logging
import os
import re
import unicodedata

logger = logging.getLogger("genly.lyrics_format")

_LANG_NAMES: dict = {
    "es": "Spanish", "spa": "Spanish",
    "en": "English",  "eng": "English",
    "pt": "Portuguese", "por": "Portuguese",
    "fr": "French",   "fra": "French",
    "it": "Italian",  "ita": "Italian",
    "de": "German",   "deu": "German",
}

_SPLIT_MIN_WORDS = 8   # segments shorter than this are never split
_SPLIT_MAX_PARTS = 3   # never more than 3 sub-lines per segment


def _lang_name(language: str | None) -> str:
    """Human-readable prompt label without treating auto as Spanish."""
    if not language:
        return "the source language"
    return _LANG_NAMES.get(language.lower()[:3], "the source language")


def _lexical_tokens(text: str) -> list[str]:
    """Return accent-insensitive lexical tokens for mutation safety.

    This formatter is allowed to change capitalization, punctuation and
    diacritics, but never vocabulary.  Folding accents makes legitimate
    corrections such as ``fragil`` -> ``frágil`` compare equal while a
    semantic rewrite such as ``Are you ready`` -> ``Estoy listo`` cannot pass.
    Apostrophes and hyphens are separators so harmless typography changes do
    not look like word insertions/deletions.
    """
    normalized = unicodedata.normalize("NFKD", str(text or "")).casefold()
    without_marks = "".join(
        char for char in normalized if unicodedata.category(char) != "Mn"
    )
    # Keep digits: changing a sung number is a semantic rewrite too (for
    # example ``638`` -> ``780465``), not harmless typography.
    return re.findall(r"[^\W_]+", without_marks, re.UNICODE)


def preserves_lexical_content(original: str, candidate: str) -> bool:
    """Whether ``candidate`` is only an orthographic rewrite of ``original``."""
    return _lexical_tokens(original) == _lexical_tokens(candidate)


# ── Timestamp assignment ──────────────────────────────────────────────────────

def _split_by_words(seg: dict, sub_texts: list) -> list:
    """Split one segment into sub-segments, using word timestamps when available.

    Assigns each sub-text a proportional slice of the word list (by word count).
    Falls back to proportional character-count split when words are absent.
    """
    if len(sub_texts) == 1:
        return [{**seg, "text": sub_texts[0]}]

    words = seg.get("words") or []
    seg_start = float(seg.get("start", 0))
    seg_end   = float(seg.get("end", 0))
    total_dur = seg_end - seg_start

    if words:
        # Proportional word-count slice → ≤1-word error, which is < ~0.3s
        sub_wc = [max(1, len(t.split())) for t in sub_texts]
        total_wc = sum(sub_wc)
        result = []
        widx = 0
        is_last = [i == len(sub_texts) - 1 for i in range(len(sub_texts))]
        for i, (text, wc) in enumerate(zip(sub_texts, sub_wc)):
            if is_last[i]:
                chunk = words[widx:]
            else:
                take = max(1, round(wc / total_wc * len(words)))
                chunk = words[widx:widx + take]
                widx += take
            if chunk:
                s = float(chunk[0].get("start") or seg_start)
                # Always use original seg_end for last sub-line (word end can lag by ~0.1s)
                e = seg_end if is_last[i] else float(chunk[-1].get("end") or seg_end)
                # guard: mono-increasing
                s = max(s, seg_start if i == 0 else result[-1]["end"])
                e = max(e, s + 0.1)
            else:
                s = result[-1]["end"] if result else seg_start
                e = seg_end if is_last[i] else s + total_dur / len(sub_texts)
            out = {**seg, "text": text, "start": round(s, 3), "end": round(e, 3)}
            if chunk:
                out["words"] = chunk
            elif "words" in out:
                del out["words"]
            result.append(out)
        return result
    else:
        # No word timestamps: proportional by character count
        total_chars = sum(len(t) for t in sub_texts) or 1
        result = []
        t = seg_start
        for i, text in enumerate(sub_texts):
            frac = len(text) / total_chars
            end = (t + total_dur * frac) if i < len(sub_texts) - 1 else seg_end
            end = round(end, 3)
            out = {k: v for k, v in seg.items() if k != "words"}
            out["text"] = text
            out["start"] = round(t, 3)
            out["end"] = end
            result.append(out)
            t = end
        return result


# ── LLM call ──────────────────────────────────────────────────────────────────

def _build_prompt(texts: list, lang: str) -> str:
    n = len(texts)
    numbered = "\n".join(f"{i + 1}. {t}" for i, t in enumerate(texts))
    return (
        "You are correcting song lyric transcription orthography. "
        f"The primary context may be {lang}, but the song may code-switch.\n"
        "Preserve the ORIGINAL language of EACH line independently. A line or "
        "phrase in another language must remain in that language. NEVER "
        "translate or paraphrase.\n\n"
        "For each numbered line:\n"
        "- Fix accents and diacritics appropriate for that line's own language\n"
        "- Capitalize the first word of each line\n"
        "- Fix punctuation (commas, periods, ellipsis)\n"
        "- For Spanish: add inverted opening marks (¿, ¡) where the line is a question or exclamation\n\n"
        "Rules:\n"
        f"- Output EXACTLY {n} lines — one per input line, same order\n"
        "- Do NOT add, remove, merge, or split lines\n"
        "- Do NOT change any words — only fix spelling/accents/punctuation\n"
        f"- Use the same '1. text' format\n\n"
        f"Input ({n} lines):\n{numbered}\n\n"
        f"Output ({n} lines):"
    )


def _parse_response(raw: str, n_input: int) -> "dict | None":
    """Parse LLM output into {1-based-idx: [sub_text, ...]} or None on error."""
    groups: dict = {}
    for line in raw.split("\n"):
        line = line.strip()
        if not line:
            continue
        m = re.match(r"^(\d+)\.\s*(.+)$", line)
        if not m:
            continue
        idx = int(m.group(1))
        text = m.group(2).strip()
        if 1 <= idx <= n_input:
            groups.setdefault(idx, []).append(text)

    # Every input index must appear; gaps → failure
    if set(groups.keys()) != set(range(1, n_input + 1)):
        return None
    # Enforce max parts per segment
    for k in groups:
        groups[k] = groups[k][:_SPLIT_MAX_PARTS]
    return groups


# ── Public API ────────────────────────────────────────────────────────────────

async def format_lyrics_pass(result: dict, language: str | None = None) -> dict:
    """Orthographic correction + line splitting on the final segment list.

    Only touches `seg["text"]`, `seg["start"]`, `seg["end"]` (and `seg["words"]`
    for split sub-segments). Returns result unchanged on any failure or when
    disabled via LYRICS_FORMAT_ENABLED=0.
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
    prompt = _build_prompt(texts, lang)
    recorder = None

    try:
        from openai import AsyncOpenAI
        client = AsyncOpenAI()

        job_id = result.get("job_id")
        if job_id:
            from provenance import record_ai_call
            recorder = record_ai_call(
                job_id=job_id,
                step="lyrics_format",
                tool_name="gpt-4o-mini",
                tool_provider="openai",
                prompt=prompt,
                input_data_types=["lyrics_text"],
            )

        resp = await asyncio.wait_for(
            client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[{"role": "user", "content": prompt}],
                temperature=0,
                max_tokens=max(len(prompt) // 2 + 400, 600),
            ),
            timeout=30,
        )
        if recorder:
            recorder.finish(response_summary="succeeded")

        raw = (resp.choices[0].message.content or "").strip()
        groups = _parse_response(raw, len(segs))

        if groups is None:
            logger.warning("[FORMAT] parse failed or missing indices — skipping")
            return result

        new_segs: list = []
        n_corrected = 0
        n_split = 0
        n_rejected = 0

        for i, seg in enumerate(segs):
            sub_texts = groups[i + 1]
            original_text = texts[i]
            candidate_text = " ".join(sub_texts)
            if not preserves_lexical_content(original_text, candidate_text):
                # The prompt is not a security boundary.  A language model may
                # still translate a short code-switched phrase while preserving
                # numbering, so validate vocabulary before applying anything.
                # Reject only this line; safe orthographic fixes on other lines
                # remain useful.
                logger.warning(
                    "[FORMAT] rejected lexical rewrite at line %d; keeping source text",
                    i + 1,
                )
                sub_texts = [original_text]
                n_rejected += 1

            if len(sub_texts) == 1:
                corrected = sub_texts[0]
                if corrected != original_text:
                    n_corrected += 1
                    new_segs.append({**seg, "text": corrected})
                else:
                    new_segs.append(seg)
            else:
                # Split: assign timestamps
                n_split += 1
                n_corrected += 1
                new_segs.extend(_split_by_words(seg, sub_texts))

        logger.info(
            "[FORMAT] %s: %d/%d lines corrected, %d lexical rewrite(s) rejected",
            lang, n_corrected, len(segs), n_rejected,
        )

        result = dict(result)
        result["segments"] = new_segs
        return result

    except Exception as exc:
        if recorder:
            recorder.finish(
                response_summary=f"error: {type(exc).__name__}: {str(exc)[:300]}"
            )
        logger.warning("[FORMAT] pass failed: %r — returning original", exc)
        return result
