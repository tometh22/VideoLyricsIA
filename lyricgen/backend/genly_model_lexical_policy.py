"""Local, occurrence-level lexical arbitration after blind audio recognition.

An external reference may veto or corroborate a hypothesis that was actually
heard; it never supplies new words on its own. Agreement of two audio views is
explicitly correlated evidence, not two independent model families.
"""
from __future__ import annotations

from difflib import SequenceMatcher
import math
import re
import unicodedata

from reviewer_shadow import tokens
from shadow_reference_import import digest

WORD = re.compile(r"[^\W_]+(?:['’][^\W_]+)?", re.UNICODE)


def _fold(text):
    return " ".join("".join(c for c in unicodedata.normalize("NFKD", str(text))
                             if not unicodedata.combining(c)).casefold().split())


def fetch_reference(artist, title, duration, *, get=None):
    """One bounded metadata-only LRCLIB lookup, with exact identity checks."""
    if not artist or not title or not math.isfinite(duration) or duration <= 0:
        return {"status": "unavailable", "reason": "metadata_missing"}
    try:
        if get is None:
            import requests
            get = requests.get
        response = get("https://lrclib.net/api/get", params={"artist_name": artist,
                       "track_name": title, "duration": round(duration)},
                       headers={"User-Agent": "GenlyLocalModel/1.0"}, timeout=8,
                       allow_redirects=False)
        if response.status_code != 200:
            return {"status": "unavailable", "reason": "reference_not_found"}
        value = response.json()
        if (_fold(value.get("artistName")) != _fold(artist) or _fold(value.get("trackName")) != _fold(title)
                or not isinstance(value.get("duration"), (int, float))
                or not math.isfinite(value["duration"])
                or abs(value["duration"] - duration) > max(3., duration * .02)):
            return {"status": "unavailable", "reason": "reference_identity_mismatch"}
        text = value.get("plainLyrics")
        if not isinstance(text, str) or not text.strip() or len(text) > 50000:
            return {"status": "unavailable", "reason": "reference_text_missing"}
        return {"status": "matched_metadata", "provider": "lrclib",
                "record_id": value.get("id"), "artist": artist, "title": title,
                "duration": value["duration"], "text": text, "text_sha256": digest(text),
                "audio_identity_verified": False, "use": "post_recognition_corroboration_only"}
    except Exception as exc:
        return {"status": "unavailable", "reason": "reference_fetch_failed", "error_type": type(exc).__name__}


def _edits(baseline, text):
    result = []
    for op, a, b, c, d in SequenceMatcher(None, tokens(baseline), tokens(text), autojunk=False).get_opcodes():
        if op == "equal":
            continue
        if op == "replace" and b - a == d - c:
            result.extend((a + i, a + i + 1, c + i, c + i + 1) for i in range(b - a))
        else:
            result.append((a, b, c, d))
    return result


def _contains(haystack, needle):
    return bool(needle) and any(haystack[i:i + len(needle)] == needle
                               for i in range(len(haystack) - len(needle) + 1))


def choose_lexical(baseline: str, primary: str, alternatives: dict, reference=None) -> dict:
    """Preserve style and apply only locally corroborated acoustic edits.

    This returns a REVIEW DRAFT, never a certificate or auto-application grant.
    Large rewrites remain visible as hypotheses, not replacements of a line.
    """
    left, right = tokens(baseline), tokens(primary)
    left_spans, right_spans = list(WORD.finditer(baseline)), list(WORD.finditer(primary))
    evidence = {}
    for view, hypothesis in alternatives.items():
        if not isinstance(hypothesis, str):
            continue
        values = tokens(hypothesis)
        evidence[view] = {(a, b, tuple(values[c:d])) for a, b, c, d in _edits(baseline, hypothesis)}
    ref_tokens = []
    if reference and reference.get("status") == "matched_metadata":
        ref_tokens = tokens(reference.get("text", ""))
    reference_veto = len(left) >= 3 and _contains(ref_tokens, left) and not _contains(ref_tokens, right)
    reference_support = len(right) >= 3 and _contains(ref_tokens, right) and not _contains(ref_tokens, left)
    accepted, withheld, patches = [], [], []
    for a, b, c, d in _edits(baseline, primary):
        key = (a, b, tuple(right[c:d]))
        views = sorted(view for view, edits in evidence.items() if key in edits)
        reason = None
        if reference_veto:
            reason = "reference_supports_original"
        elif a == b and a in {0, len(left)}:
            reason = "line_boundary_insertion_requires_review"
        elif max(b - a, d - c) > 2 or max(b - a, d - c) / max(1, len(left)) > .5:
            reason = "large_rewrite_requires_review"
        elif len(views) < 2 and not reference_support:
            reason = "single_view_change_requires_review"
        item = {"original_token_range": [a, b], "candidate_token_range": [c, d],
                "before": left[a:b], "after": right[c:d], "supporting_views": views,
                "reference_support": bool(reference_support), "independent_families": 1}
        if reason:
            withheld.append({**item, "reason": reason})
            continue
        start = left_spans[a].start() if a < len(left_spans) else len(baseline)
        end = left_spans[b - 1].end() if b > a else start
        replacement = " ".join(m.group() for m in right_spans[c:d])
        if a == b and replacement:
            replacement = (" " if start and not baseline[start - 1].isspace() else "") + replacement
            replacement += " " if start < len(baseline) and not baseline[start].isspace() else ""
        patches.append((start, end, replacement))
        accepted.append(item)
    text = baseline
    for start, end, replacement in reversed(patches):
        text = text[:start] + replacement + text[end:]
    return {"text": text, "accepted_edits": accepted, "withheld_edits": withheld,
            "policy": "corroborated-local-edits-v1", "correctness_certified": False,
            "automatic_apply_allowed": False}
