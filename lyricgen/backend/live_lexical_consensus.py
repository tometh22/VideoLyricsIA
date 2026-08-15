"""Conservative catalogue spelling correction for audio-first live lyrics.

The performance owns line order, boundaries and timing.  A catalogue may only
replace the spelling of a line when it is effectively the same sentence: same
token count, few positional substitutions, high lexical similarity and no
ambiguous alternative.  It never inserts, deletes, splits or reorders lyrics.
"""
from __future__ import annotations

from difflib import SequenceMatcher
import logging
import os
import unicodedata

logger = logging.getLogger("genly.live_lexical_consensus")
_TRUE = {"1", "true", "yes", "on"}


def is_enabled() -> bool:
    return os.environ.get(
        "LIVE_LEXICAL_CONSENSUS_ENABLED", "0"
    ).strip().lower() in _TRUE


def _token(token: str) -> str:
    folded = unicodedata.normalize("NFD", token or "").casefold()
    return "".join(ch for ch in folded if ch.isalnum() and not unicodedata.combining(ch))


def _tokens(text: str) -> list[str]:
    return [token for token in (_token(part) for part in (text or "").split()) if token]


def _score(left: list[str], right: list[str]) -> float:
    if not left or len(left) != len(right):
        return 0.0
    positional = sum(a == b for a, b in zip(left, right)) / len(left)
    sequence = SequenceMatcher(None, left, right).ratio()
    return max(positional, sequence)


def correct_segments(segments: list[dict], reference_text: str, *,
                     min_score: float = 0.75,
                     ambiguity_margin: float = 0.08,
                     max_substitutions: int = 2) -> tuple[list[dict], dict]:
    """Return spelling-corrected copies plus explainable statistics.

    Short one/two-token ad-libs are only typography-normalised when their
    normalised text is already identical.  This prevents a catalogue's studio
    refrain from overwriting a different live shout such as ``Real``/``No``.
    """
    stats = {
        "enabled": True,
        "segments_considered": len(segments or []),
        "lines_corrected": 0,
        "orthography_only": 0,
        "lexical_substitutions": 0,
        "declined_ambiguous": 0,
        "declined_structure": 0,
    }
    if not segments or not (reference_text or "").strip():
        return [dict(segment) for segment in (segments or [])], stats

    references = [
        (line.strip(), _tokens(line))
        for line in reference_text.splitlines() if line.strip()
    ]
    out: list[dict] = []
    for original in segments:
        segment = dict(original)
        heard_text = str(segment.get("text") or "").strip()
        heard = _tokens(heard_text)
        if not heard:
            out.append(segment)
            continue

        candidates = []
        for ref_index, (line, ref) in enumerate(references):
            if len(ref) != len(heard):
                continue
            score = _score(heard, ref)
            if score >= min_score:
                substitutions = sum(a != b for a, b in zip(heard, ref))
                candidates.append((
                    score, -substitutions, -ref_index,
                    line, ref, substitutions,
                ))
        if not candidates:
            stats["declined_structure"] += 1
            out.append(segment)
            continue
        candidates.sort(reverse=True)
        best = candidates[0]
        # Several repeated catalogue rows with the same text are not
        # ambiguous. Different candidate texts near-tied are.
        alternatives = [
            candidate for candidate in candidates[1:]
            if candidate[4] != best[4]
            and best[0] - candidate[0] < ambiguity_margin
        ]
        if alternatives:
            stats["declined_ambiguous"] += 1
            out.append(segment)
            continue
        score, _neg_subs, _neg_index, corrected_text, _ref, substitutions = best
        allowed = min(max_substitutions, max(1, len(heard) // 3))
        if substitutions > allowed or (len(heard) <= 2 and substitutions):
            stats["declined_structure"] += 1
            out.append(segment)
            continue
        if heard_text == corrected_text:
            out.append(segment)
            continue

        segment["text"] = corrected_text
        segment["live_lexical_corrected"] = True
        segment["live_lexical_score"] = round(score, 3)
        segment["live_lexical_original"] = heard_text
        out.append(segment)
        stats["lines_corrected"] += 1
        if substitutions:
            stats["lexical_substitutions"] += substitutions
        else:
            stats["orthography_only"] += 1

    if stats["lines_corrected"]:
        logger.info(
            "[LIVE-LEXICAL] corrected %d line(s), %d lexical substitution(s)",
            stats["lines_corrected"], stats["lexical_substitutions"],
        )
    return out, stats


def propose_segments(segments: list[dict], reference_text: str) -> tuple[list[dict], dict]:
    """Attach conservative catalogue proposals without changing visible text."""
    corrected, stats = correct_segments(segments, reference_text)
    proposed = []
    for original, candidate in zip(segments or [], corrected):
        if candidate.get("live_lexical_corrected"):
            segment = dict(original)
            segment["live_lexical_suggestion"] = candidate.get("text")
            segment["live_lexical_score"] = candidate.get("live_lexical_score")
            proposed.append(segment)
        else:
            proposed.append(dict(original))
    stats = dict(stats)
    stats["lines_proposed"] = stats.get("lines_corrected", 0)
    stats["lines_corrected"] = 0
    return proposed, stats


def _bounds(word: dict) -> tuple[float, float]:
    try:
        start = float(word.get("start", 0.0))
        end = float(word.get("end", start))
    except (TypeError, ValueError):
        return 0.0, 0.0
    return start, max(start, end)


def _best_positional_score(candidate: list[str], witness: list[str]) -> float:
    if not candidate or len(witness) < len(candidate):
        return 0.0
    return max(
        sum(a == b for a, b in zip(candidate, witness[start:start + len(candidate)]))
        / len(candidate)
        for start in range(0, len(witness) - len(candidate) + 1)
    )


def verify_corrections(segments: list[dict], witness_words: list[dict], *,
                       pad: float = 0.75) -> dict:
    """Verify every lexical substitution against an independent witness.

    Punctuation/accent-only changes need no acoustic decision. A true word
    substitution passes only when a bounded witness slice is materially closer
    to the corrected catalogue line than to the original ASR line. Witness
    word intervals are matched by overlap (not midpoint), which tolerates
    Whisper's occasional long boxes without admitting the next lyric row.
    """
    details = []
    total = verified = 0
    words = [w for w in (witness_words or []) if isinstance(w, dict)]
    for index, segment in enumerate(segments or []):
        if not isinstance(segment, dict) or not segment.get("live_lexical_corrected"):
            continue
        original = _tokens(str(segment.get("live_lexical_original") or ""))
        corrected = _tokens(str(segment.get("text") or ""))
        if not original or len(original) != len(corrected):
            continue
        changed = sum(a != b for a, b in zip(original, corrected))
        if not changed:
            continue
        total += 1
        try:
            start, end = float(segment.get("start")), float(segment.get("end"))
        except (TypeError, ValueError):
            start = end = 0.0
        heard = _tokens(" ".join(
            str(word.get("word") or "")
            for word in words
            if _bounds(word)[1] >= start - pad
            and _bounds(word)[0] <= end + pad
        ))
        corrected_score = _best_positional_score(corrected, heard)
        original_score = _best_positional_score(original, heard)
        is_verified = (
            corrected_score >= 0.72
            and corrected_score - original_score >= 0.5 / len(corrected)
        )
        if is_verified:
            verified += 1
        else:
            details.append({
                "index": index, "start": round(start, 2),
                "end": round(end, 2),
                "original": str(segment.get("live_lexical_original") or ""),
                "corrected": str(segment.get("text") or ""),
                "original_score": round(original_score, 3),
                "corrected_score": round(corrected_score, 3),
            })
    return {
        "total": total, "verified": verified,
        "unverified": len(details), "details": details,
    }


def apply_verified_proposals(segments: list[dict], witness_words: list[dict]) \
        -> tuple[list[dict], dict]:
    """Apply only proposals independently supported by the acoustic witness."""
    out = []
    stats = {"proposals": 0, "applied": 0, "declined": 0}
    for original in segments or []:
        segment = dict(original)
        suggestion = str(segment.get("live_lexical_suggestion") or "").strip()
        if not suggestion:
            out.append(segment)
            continue
        stats["proposals"] += 1
        candidate = dict(segment)
        candidate["live_lexical_original"] = str(segment.get("text") or "")
        candidate["text"] = suggestion
        candidate["live_lexical_corrected"] = True
        verification = verify_corrections([candidate], witness_words)
        if verification["verified"] == 1 or verification["total"] == 0:
            candidate.pop("live_lexical_suggestion", None)
            candidate["live_lexical_verified"] = True
            corrected_parts = suggestion.split()
            timed_words = candidate.get("words") or []
            if len(timed_words) == len(corrected_parts) and all(
                isinstance(word, dict) for word in timed_words
            ):
                candidate["words"] = [
                    {**dict(word), "word": corrected_parts[index]}
                    for index, word in enumerate(timed_words)
                ]
            else:
                # Never display old karaoke tokens under corrected text.
                candidate.pop("words", None)
            out.append(candidate)
            stats["applied"] += 1
        else:
            segment.pop("live_lexical_suggestion", None)
            segment["live_lexical_declined"] = True
            out.append(segment)
            stats["declined"] += 1
    return out, stats
