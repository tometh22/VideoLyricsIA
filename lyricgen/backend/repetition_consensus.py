"""Review-only content consensus across repeated lyric occurrences.

The audio pipeline already discovers recurring motifs.  This module provides
the complementary content check: if at least two independent occurrences in
the same job agree on wording and a third differs slightly, propose the stable
wording for operator review.  It never changes timing or mutates segments.
"""
from __future__ import annotations

from collections import Counter
from difflib import SequenceMatcher
import re
import unicodedata


def _tokens(text: str) -> tuple[str, ...]:
    normalized = unicodedata.normalize("NFKD", str(text or "").lower())
    normalized = "".join(char for char in normalized if not unicodedata.combining(char))
    return tuple(re.findall(r"[a-z0-9]+", normalized))


def _similarity(left: tuple[str, ...], right: tuple[str, ...]) -> float:
    if not left or not right:
        return 0.0
    sequence = SequenceMatcher(None, left, right).ratio()
    union = set(left) | set(right)
    jaccard = len(set(left) & set(right)) / max(1, len(union))
    return 0.72 * sequence + 0.28 * jaccard


def propose_recurrence_corrections(
    segments: list[dict], *, min_occurrences: int = 3,
    min_separation_s: float = 8.0, cluster_similarity: float = 0.62,
) -> list[dict]:
    """Return conservative, uncalibrated content suggestions.

    A canonical line must occur verbatim at least twice.  A unique medoid is
    not enough: that prevents three different hallucinations from voting one
    another into a fabricated lyric.
    """
    rows = []
    for index, segment in enumerate(segments or []):
        if not isinstance(segment, dict):
            continue
        tokens = _tokens(segment.get("text") or "")
        if len(tokens) < 3:
            continue
        try:
            start = float(segment.get("start"))
        except (TypeError, ValueError):
            continue
        rows.append({"index": index, "start": start, "tokens": tokens,
                     "text": str(segment.get("text") or "").strip()})
    parent = list(range(len(rows)))

    def find(value: int) -> int:
        while parent[value] != value:
            parent[value] = parent[parent[value]]
            value = parent[value]
        return value

    def union(left: int, right: int) -> None:
        a, b = find(left), find(right)
        if a != b:
            parent[b] = a

    for left in range(len(rows)):
        for right in range(left + 1, len(rows)):
            if abs(rows[right]["start"] - rows[left]["start"]) < min_separation_s:
                continue
            if _similarity(rows[left]["tokens"], rows[right]["tokens"]) >= cluster_similarity:
                union(left, right)
    groups: dict[int, list[dict]] = {}
    for position, row in enumerate(rows):
        groups.setdefault(find(position), []).append(row)

    suggestions = []
    for group in groups.values():
        if len(group) < min_occurrences:
            continue
        # Exact duplicates close together are commonly editor fragmentation,
        # not distinct performances.  Count only temporally independent
        # occurrences as canonical support.
        counts = Counter()
        for tokens in {item["tokens"] for item in group}:
            starts = sorted(item["start"] for item in group if item["tokens"] == tokens)
            kept = []
            for value in starts:
                if not kept or value - kept[-1] >= min_separation_s:
                    kept.append(value)
            counts[tokens] = len(kept)
        canonical_tokens, support = counts.most_common(1)[0]
        if support < 2:
            continue
        tied = sum(count == support for count in counts.values()) > 1
        if tied:
            continue
        canonical_row = next(item for item in group if item["tokens"] == canonical_tokens)
        compatible = [
            item for item in group
            if _similarity(item["tokens"], canonical_tokens) >= cluster_similarity
        ]
        independent_starts = []
        for item in sorted(compatible, key=lambda row: row["start"]):
            if not independent_starts or item["start"] - independent_starts[-1] >= min_separation_s:
                independent_starts.append(item["start"])
        if len(independent_starts) < min_occurrences:
            continue
        for item in compatible:
            if item["tokens"] == canonical_tokens:
                continue
            suggestions.append({
                "segment_index": item["index"],
                "suggested_text": canonical_row["text"],
                "occurrences": len(independent_starts),
                "exact_support": support,
                "similarity": round(_similarity(item["tokens"], canonical_tokens), 4),
                "confidence_kind": "uncalibrated",
                "review": True,
                "automatic_apply_allowed": False,
            })
    return sorted(suggestions, key=lambda item: item["segment_index"])
