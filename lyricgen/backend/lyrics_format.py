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

# Words that cannot naturally close a Spanish lyric card.  This list is
# intentionally small: the cross-card repair below is a precision rule, not a
# general-purpose parser.  Ambiguous words such as ``bajo`` are excluded.
_SPANISH_BOUNDARY_PREFIXES = {
    "a", "al", "con", "contra", "de", "del", "desde", "e", "el", "en",
    "entre", "hacia", "hasta", "la", "las", "lo", "los", "mi", "mis",
    "ni", "o", "para", "pero", "por", "que", "se", "sin", "sobre", "su",
    "sus", "tras", "tu", "tus", "u", "un", "una", "unas", "unos", "y",
}
_SENTENCE_END = frozenset(".!?…")
_MAX_BOUNDARY_SHIFT_S = 0.50
_MAX_JOIN_GAP_S = 0.75


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


def _without_terminal_full_stop(value: str) -> str:
    """Remove a single editorial full stop from a display line.

    Question/exclamation marks and ellipses carry performance meaning and are
    left alone.  A final period, by contrast, is visual prose punctuation and
    produces the dotted lyric cards reported by campaign reviewers.
    """
    text = str(value or "").strip()
    if not text.endswith(".") or text.endswith("..."):
        return text
    return text[:-1].rstrip()


def _plain_token(value: str) -> str:
    # Boundary grammar is accent-sensitive: Spanish ``qué`` (interrogative)
    # is not the conjunction ``que``, and ``dé`` is not the preposition
    # ``de``.  The broader lexical-safety comparison intentionally remains
    # accent-insensitive because the formatter is allowed to fix diacritics.
    normalized = unicodedata.normalize("NFKC", str(value or "")).casefold()
    tokens = re.findall(r"[^\W\d_]+", normalized, re.UNICODE)
    return tokens[0] if len(tokens) == 1 else ""


def _line_tokens(value: str) -> list[str]:
    return [token for token in str(value or "").strip().split() if token]


def _lower_initial_if_grammar_word(value: str, words: list[dict] | None = None) -> str:
    """Lowercase a line-initial grammar word after prepending a connector."""
    tokens = _line_tokens(value)
    if not tokens:
        return ""
    first_plain = _plain_token(tokens[0])
    raw_first = ""
    if words and isinstance(words[0], dict):
        raw_first = str(words[0].get("word") or "").strip()
    source_was_lower = bool(raw_first[:1] and raw_first[:1].islower())
    if first_plain in _SPANISH_BOUNDARY_PREFIXES or source_was_lower:
        tokens[0] = tokens[0][:1].lower() + tokens[0][1:]
    return " ".join(tokens)


def _boundary_suffix(tokens: list[str]) -> list[str]:
    """Return the one/two-token grammatical suffix eligible to move right."""
    if not tokens or tokens[-1][-1:] in _SENTENCE_END:
        return []
    last = _plain_token(tokens[-1])
    if last not in _SPANISH_BOUNDARY_PREFIXES:
        return []
    suffix = [tokens[-1]]
    if len(tokens) >= 2:
        previous = _plain_token(tokens[-2])
        # Keep conventional pairs together (``de los``, ``por la``).  Do not
        # greedily move any two grammar words: ``para que`` at a sentence end
        # is often a complete interrogative phrase.
        if previous in {"a", "al", "con", "de", "del", "en", "para", "por", "sin"} \
                and last in {"el", "la", "las", "los", "lo", "un", "una", "unas", "unos"}:
            suffix.insert(0, tokens[-2])
    return suffix


def _word_matches_token(word: dict, token: str) -> bool:
    return _lexical_tokens(str(word.get("word") or "")) == _lexical_tokens(token)


def _can_shift_boundary(left: dict, right: dict, suffix: list[str]) -> bool:
    """Require word evidence proving the grammatical suffix hugs the boundary."""
    left_words = left.get("words") or []
    right_words = right.get("words") or []
    if not left_words or not right_words or len(left_words) < len(suffix):
        return False
    moved_words = left_words[-len(suffix):]
    if not all(
        isinstance(word, dict) and _word_matches_token(word, token)
        for word, token in zip(moved_words, suffix)
    ):
        return False
    try:
        moved_start = float(moved_words[0]["start"])
        moved_end = float(moved_words[-1]["end"])
        next_start = float(right["start"])
        next_word_start = float(right_words[0]["start"])
    except (KeyError, TypeError, ValueError):
        return False
    if abs(next_start - moved_start) > _MAX_BOUNDARY_SHIFT_S:
        return False
    return -0.05 <= next_word_start - moved_end <= _MAX_JOIN_GAP_S


def polish_display_layout(result: dict) -> dict:
    """Repair obvious caption-boundary defects without changing any timing.

    The policy has two deliberately narrow operations:

    * remove terminal prose periods from lyric display lines;
    * move a stranded Spanish connector/article to the following card only
      when word timestamps prove it sits within 500 ms of that boundary.

    Segment count, order, ``start`` and ``end`` are invariant.  The lexical
    token stream is invariant too; only its card assignment can change.
    """
    if not isinstance(result, dict):
        return result
    source = result.get("segments") or []
    if not source or not all(isinstance(segment, dict) for segment in source):
        return result

    segments = [dict(segment) for segment in source]
    changed = False

    for segment in segments:
        current = str(segment.get("text") or "").strip()
        cleaned = _without_terminal_full_stop(current)
        if cleaned != current:
            segment["text"] = cleaned
            changed = True

    for index in range(len(segments) - 1):
        left = segments[index]
        right = segments[index + 1]
        left_tokens = _line_tokens(left.get("text") or "")
        right_tokens = _line_tokens(right.get("text") or "")
        suffix = _boundary_suffix(left_tokens)
        retained = left_tokens[:-len(suffix)] if suffix else left_tokens
        # A one-word or all-connector fragment needs editorial context; never
        # guess by emptying a timestamped card automatically.
        if not suffix or len(_lexical_tokens(" ".join(retained))) < 2:
            continue
        if not any(
            _plain_token(token) not in _SPANISH_BOUNDARY_PREFIXES
            for token in retained
        ):
            continue
        if len(_lexical_tokens(" ".join(suffix + right_tokens))) > 12:
            continue
        if not _can_shift_boundary(left, right, suffix):
            continue

        moved_words = list(left.get("words") or [])[-len(suffix):]
        left["text"] = " ".join(retained).strip()
        right_text = _lower_initial_if_grammar_word(
            " ".join(right_tokens), list(right.get("words") or []),
        )
        moved_text = " ".join(suffix)
        moved_text = moved_text[:1].upper() + moved_text[1:]
        right["text"] = f"{moved_text} {right_text}".strip()
        left["words"] = list(left.get("words") or [])[:-len(suffix)]
        right["words"] = moved_words + list(right.get("words") or [])
        changed = True

    # A moved suffix can expose a period that used to be internal
    # (``mejillas. En`` -> ``mejillas.``); apply the display rule once more.
    for segment in segments:
        current = str(segment.get("text") or "").strip()
        cleaned = _without_terminal_full_stop(current)
        if cleaned != current:
            segment["text"] = cleaned
            changed = True

    if not changed:
        return result

    # Fail closed if a future edit accidentally changes the approved timeline
    # or vocabulary while extending this policy.
    before_timing = [(s.get("start"), s.get("end")) for s in source]
    after_timing = [(s.get("start"), s.get("end")) for s in segments]
    before_lexical = _lexical_tokens(" ".join(str(s.get("text") or "") for s in source))
    after_lexical = _lexical_tokens(" ".join(str(s.get("text") or "") for s in segments))
    if before_timing != after_timing or before_lexical != after_lexical:
        logger.error("[FORMAT] display-layout invariant failed; keeping source")
        return result

    polished = dict(result)
    polished["segments"] = segments
    return polished


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
        "- Fix punctuation inside a line when it clarifies the phrase\n"
        "- NEVER end a lyric display line with a full stop (.)\n"
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
        return polish_display_layout(result)

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
            return polish_display_layout(result)

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
        return polish_display_layout(result)

    except Exception as exc:
        if recorder:
            recorder.finish(
                response_summary=f"error: {type(exc).__name__}: {str(exc)[:300]}"
            )
        logger.warning("[FORMAT] pass failed: %r — returning original", exc)
        return polish_display_layout(result)
