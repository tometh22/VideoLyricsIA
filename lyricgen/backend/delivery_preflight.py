"""Fail-closed delivery QC for rendered lyric videos.

This module models the useful part of a label QC report without pretending that
catalogue text or OCR are automatically ground truth.  It audits the *final*
display text and asset identity, aggregates repeated occurrences, and returns
frame timecodes that an editor can open directly.

The preflight is deliberately pure: it does not mutate lyrics, approve a
delivery, call an LLM, or upload anything.  Callers decide whether WARN items
need human approval and whether FAIL items block export.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from difflib import SequenceMatcher
import hashlib
import math
import re
import unicodedata
from typing import Any, Iterable, Mapping, Sequence


SCHEMA_VERSION = "genly-delivery-qc-v1"


def _text(value: Any) -> str:
    return str(value or "").strip()


def _strip_marks(value: Any) -> str:
    normalized = unicodedata.normalize("NFKD", _text(value).casefold())
    return "".join(ch for ch in normalized if unicodedata.category(ch) != "Mn")


def _tokens(value: Any) -> list[str]:
    return re.findall(r"[^\W_]+", _strip_marks(value), re.UNICODE)


def _surface_tokens(value: Any) -> list[str]:
    return re.findall(r"[^\W_]+", unicodedata.normalize("NFC", _text(value)), re.UNICODE)


def _folded_line(value: Any) -> str:
    return " ".join(_tokens(value))


def _finite_number(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _json_safe(value: Any) -> Any:
    """Return a strict-JSON representation suitable for PostgreSQL JSONB.

    Python's encoder accepts NaN/Infinity by default, while PostgreSQL JSONB
    correctly rejects them.  QC is a guardrail and must not become the reason
    an otherwise valid render fails to persist when upstream diagnostics carry
    one non-finite measurement.
    """
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if value is None or isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_json_safe(item) for item in value]
    return str(value)


def frame_timecode(seconds: Any, fps: float = 30.0) -> str:
    """Convert elapsed seconds to a non-drop ``HH:MM:SS:FF`` timecode."""
    value = max(0.0, _finite_number(seconds) or 0.0)
    rate = _finite_number(fps) or 30.0
    if rate <= 0:
        rate = 30.0
    nominal = max(1, int(round(rate)))
    total_frames = max(0, int(round(value * rate)))
    frames = total_frames % nominal
    total_seconds = total_frames // nominal
    secs = total_seconds % 60
    minutes = (total_seconds // 60) % 60
    hours = total_seconds // 3600
    return f"{hours:02d}:{minutes:02d}:{secs:02d}:{frames:02d}"


def _segment_text(row: Mapping[str, Any]) -> str:
    return _text(row.get("text") if row.get("text") is not None else row.get("t"))


def _segment_start(row: Mapping[str, Any]) -> float:
    return max(0.0, _finite_number(row.get("start", row.get("s"))) or 0.0)


def _segment_end(row: Mapping[str, Any]) -> float:
    start = _segment_start(row)
    return max(start, _finite_number(row.get("end", row.get("e"))) or start)


def _token_start(row: Mapping[str, Any], token_index: int) -> float:
    """Use real word timing when present; never invent intra-line precision."""
    words = row.get("words") or []
    if isinstance(words, Sequence) and not isinstance(words, (str, bytes)):
        lexical_words = [item for item in words if isinstance(item, Mapping)]
        if token_index < len(lexical_words):
            value = _finite_number(lexical_words[token_index].get("start"))
            if value is not None:
                return max(0.0, value)
    return _segment_start(row)


def _identity_tokens(value: Any) -> list[str]:
    # Separators that accidentally leak from filenames must not hide a mismatch.
    return _tokens(re.sub(r"[_./\\-]+", " ", _text(value)))


def _identity_equal(left: Any, right: Any) -> bool:
    return _identity_tokens(left) == _identity_tokens(right)


def _line_similarity(left: str, right: str) -> float:
    return SequenceMatcher(None, _folded_line(left), _folded_line(right)).ratio()


def _edit_distance(left: str, right: str) -> int:
    a, b = _strip_marks(left), _strip_marks(right)
    previous = list(range(len(b) + 1))
    for i, char_a in enumerate(a, 1):
        current = [i]
        for j, char_b in enumerate(b, 1):
            current.append(min(
                current[-1] + 1,
                previous[j] + 1,
                previous[j - 1] + (char_a != char_b),
            ))
        previous = current
    return previous[-1]


@dataclass(frozen=True)
class _Occurrence:
    code: str
    severity: str
    category: str
    summary: str
    description: str
    seconds: float
    actual: str = ""
    expected: str = ""
    detector: str = "deterministic"
    confidence: float = 1.0
    auto_fixable: bool = False
    evidence: Mapping[str, Any] = field(default_factory=dict)


def _frequency(count: int, total_segments: int, indices: Sequence[int]) -> str:
    if total_segments and count >= max(4, math.ceil(total_segments * 0.75)):
        return "GLOBAL"
    if count == 1:
        return "ISOLATED"
    if count >= 5:
        return "FREQUENT"
    if len(indices) >= 3 and max(indices) - min(indices) + 1 == len(indices):
        return "RANGE"
    return "INTERMITTENT"


def _issue_id(key: str, occurrences: Sequence[_Occurrence]) -> str:
    times = ",".join(f"{item.seconds:.3f}" for item in occurrences)
    return hashlib.sha256(f"{key}|{times}".encode()).hexdigest()[:16]


def _aggregate(
    occurrences: Iterable[tuple[int, _Occurrence]], *, total_segments: int, fps: float
) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str, str], list[tuple[int, _Occurrence]]] = defaultdict(list)
    for index, occurrence in occurrences:
        # Repeated JAMAS -> JAMÁS becomes one intermittent issue, while a
        # different misspelling remains independently actionable.
        grouped[(occurrence.code, occurrence.actual, occurrence.expected)].append(
            (index, occurrence)
        )

    severity_order = {"FAIL": 0, "WARN": 1, "PASS": 2}
    issues: list[dict[str, Any]] = []
    for key, rows in grouped.items():
        rows.sort(key=lambda item: item[1].seconds)
        first = rows[0][1]
        indices = [item[0] for item in rows]
        items = [item[1] for item in rows]
        timecodes = [frame_timecode(item.seconds, fps) for item in items]
        issues.append({
            "issue_id": _issue_id("|".join(key), items),
            "status": "OPEN",
            "severity": first.severity,
            "category": first.category,
            "universal_category": "Other Video Issues",
            "code": first.code,
            "summary": first.summary,
            "description": first.description,
            "frequency": _frequency(len(items), total_segments, indices),
            "occurrence_count": len(items),
            "timecode": timecodes[0],
            "timecodes": timecodes,
            "seconds": [round(item.seconds, 3) for item in items],
            "actual": first.actual,
            "expected": first.expected,
            "detector": first.detector,
            "confidence": round(min(item.confidence for item in items), 3),
            "auto_fixable": all(item.auto_fixable for item in items),
            "evidence": [dict(item.evidence) for item in items],
        })
    return sorted(
        issues,
        key=lambda item: (severity_order.get(item["severity"], 9), item["seconds"][0]),
    )


def _match_reference_lines(
    delivered: Sequence[Mapping[str, Any]],
    approved: Sequence[Mapping[str, Any] | str],
) -> list[tuple[int, str, str, float]]:
    """Return conservative delivered/reference pairs.

    Timed approved segments are matched by temporal overlap/proximity.  Plain
    lines use a monotonic look-ahead and require strong folded-text similarity.
    Ambiguous rows abstain instead of inventing a spelling correction.
    """
    pairs: list[tuple[int, str, str, float]] = []
    approved_rows = [
        item if isinstance(item, Mapping) else {"text": _text(item)}
        for item in approved
    ]
    has_timing = any(
        row.get("start") is not None or row.get("s") is not None
        for row in approved_rows
    )
    if len(delivered) == len(approved_rows) and not has_timing:
        for index, (actual_row, expected_row) in enumerate(zip(delivered, approved_rows)):
            actual, expected = _segment_text(actual_row), _segment_text(expected_row)
            score = _line_similarity(actual, expected)
            if score >= 0.72:
                pairs.append((index, actual, expected, score))
        return pairs

    cursor = 0
    for index, row in enumerate(delivered):
        actual = _segment_text(row)
        candidates: list[tuple[float, int, str]] = []
        for approved_index in range(cursor, min(len(approved_rows), cursor + 8)):
            ref = approved_rows[approved_index]
            expected = _segment_text(ref)
            lexical = _line_similarity(actual, expected)
            score = lexical
            if has_timing and (ref.get("start") is not None or ref.get("s") is not None):
                distance = abs(_segment_start(row) - _segment_start(ref))
                timing = max(0.0, 1.0 - distance / 4.0)
                score = lexical * 0.75 + timing * 0.25
            candidates.append((score, approved_index, expected))
        if not candidates:
            continue
        best = max(candidates, key=lambda candidate: candidate[0])
        if best[0] < 0.72:
            continue
        pairs.append((index, actual, best[2], best[0]))
        cursor = best[1] + 1
    return pairs


def _lyric_occurrences(
    segments: Sequence[Mapping[str, Any]],
    approved_lyrics: Sequence[Mapping[str, Any] | str],
    *,
    reference_trusted: bool,
) -> list[tuple[int, _Occurrence]]:
    if not reference_trusted:
        return []
    found: list[tuple[int, _Occurrence]] = []
    for index, actual_line, expected_line, match_confidence in _match_reference_lines(
        segments, approved_lyrics
    ):
        actual_tokens = _surface_tokens(actual_line)
        expected_tokens = _surface_tokens(expected_line)
        if len(actual_tokens) != len(expected_tokens):
            # A trusted reference may still describe a studio rather than live
            # performance.  Word insertions/deletions need acoustic evidence and
            # are outside this spelling-only preflight.
            continue
        for token_index, (actual, expected) in enumerate(zip(actual_tokens, expected_tokens)):
            if actual == expected:
                continue
            folded_actual, folded_expected = _strip_marks(actual), _strip_marks(expected)
            seconds = _token_start(segments[index], token_index)
            evidence = {
                "segment_index": index,
                "token_index": token_index,
                "delivered_line": actual_line,
                "approved_line": expected_line,
                "reference_match_confidence": round(match_confidence, 3),
            }
            if folded_actual == folded_expected:
                found.append((index, _Occurrence(
                    code="LYRIC_ORTHOGRAPHY_MISMATCH",
                    severity="WARN",
                    category="Misspelled lyrics",
                    summary=f'Check spelling/diacritics: "{actual}"',
                    description=f'Final displayed lyric "{actual}" should read "{expected}".',
                    seconds=seconds,
                    actual=actual,
                    expected=expected,
                    detector="trusted_reference_orthography",
                    confidence=match_confidence,
                    auto_fixable=True,
                    evidence=evidence,
                )))
                continue
            distance = _edit_distance(actual, expected)
            similarity = SequenceMatcher(None, folded_actual, folded_expected).ratio()
            if distance <= 2 and similarity >= 0.72:
                found.append((index, _Occurrence(
                    code="LYRIC_TOKEN_TYPO",
                    severity="WARN",
                    category="Sung lyric mismatch",
                    summary=f'Possible typo: "{actual}"',
                    description=(
                        f'Approved/sung lyric "{expected}" appears as "{actual}" '
                        "in the final display."
                    ),
                    seconds=seconds,
                    actual=actual,
                    expected=expected,
                    detector="trusted_reference_near_match",
                    confidence=min(match_confidence, similarity),
                    auto_fixable=False,
                    evidence=evidence,
                )))
    return found


def _metadata_occurrences(
    metadata: Mapping[str, Any], asset: Mapping[str, Any]
) -> list[tuple[int, _Occurrence]]:
    found: list[tuple[int, _Occurrence]] = []
    expected_title = _text(metadata.get("title"))
    expected_version = _text(metadata.get("version"))
    # Title and version are separate delivery fields.  Concatenating them would
    # create false mismatches against a title card that intentionally shows only
    # the work title.  A suffix leaked into the title (e.g. ``_En Vivo``) still
    # fails this comparison, exactly like the label example.
    expected_display = expected_title
    rendered_title = _text(asset.get("rendered_title") or asset.get("title_card"))
    if expected_display and rendered_title and not _identity_equal(expected_display, rendered_title):
        seconds = _finite_number(asset.get("title_time")) or 0.0
        found.append((0, _Occurrence(
            code="METADATA_TITLE_MISMATCH",
            severity="WARN",
            category="Metadata mismatch",
            summary="Rendered title does not match delivery metadata",
            description=(
                f'Metadata title/version "{expected_display}" differs from '
                f'rendered graphic text "{rendered_title}".'
            ),
            seconds=seconds,
            actual=rendered_title,
            expected=expected_display,
            detector="metadata_vs_render_manifest",
            confidence=1.0,
            auto_fixable=True,
            evidence={"field": "title", "asset_source": asset.get("source", "render_manifest")},
        )))
    expected_artist = _text(metadata.get("artist"))
    rendered_artist = _text(asset.get("rendered_artist"))
    if expected_artist and rendered_artist and not _identity_equal(expected_artist, rendered_artist):
        seconds = _finite_number(asset.get("title_time")) or 0.0
        found.append((0, _Occurrence(
            code="METADATA_ARTIST_MISMATCH",
            severity="WARN",
            category="Metadata mismatch",
            summary="Rendered artist does not match delivery metadata",
            description=(
                f'Metadata artist "{expected_artist}" differs from rendered '
                f'graphic text "{rendered_artist}".'
            ),
            seconds=seconds,
            actual=rendered_artist,
            expected=expected_artist,
            detector="metadata_vs_render_manifest",
            confidence=1.0,
            auto_fixable=True,
            evidence={"field": "artist", "asset_source": asset.get("source", "render_manifest")},
        )))
    rendered_version = _text(asset.get("rendered_version"))
    if expected_version and rendered_version and not _identity_equal(expected_version, rendered_version):
        seconds = _finite_number(asset.get("title_time")) or 0.0
        found.append((0, _Occurrence(
            code="METADATA_VERSION_MISMATCH",
            severity="WARN",
            category="Metadata mismatch",
            summary="Rendered version does not match delivery metadata",
            description=(
                f'Metadata version "{expected_version}" differs from rendered '
                f'version text "{rendered_version}".'
            ),
            seconds=seconds,
            actual=rendered_version,
            expected=expected_version,
            detector="metadata_vs_render_manifest",
            confidence=1.0,
            auto_fixable=True,
            evidence={"field": "version", "asset_source": asset.get("source", "render_manifest")},
        )))
    return found


def _timeline_occurrences(
    segments: Sequence[Mapping[str, Any]], duration: float | None
) -> list[tuple[int, _Occurrence]]:
    found: list[tuple[int, _Occurrence]] = []
    previous_end = 0.0
    for index, row in enumerate(segments):
        start, end = _segment_start(row), _segment_end(row)
        if end <= start:
            found.append((index, _Occurrence(
                code="INVALID_LYRIC_RANGE", severity="FAIL", category="Timing",
                summary="Invalid lyric time range",
                description="A final lyric event has no positive display duration.",
                seconds=start, detector="timeline_invariants", auto_fixable=False,
                evidence={"segment_index": index, "start": start, "end": end},
            )))
        if start + 0.001 < previous_end:
            found.append((index, _Occurrence(
                code="LYRIC_OVERLAP", severity="WARN", category="Timing",
                summary="Overlapping lyric events",
                description="A lyric begins before the preceding lyric has finished.",
                seconds=start, detector="timeline_invariants", auto_fixable=False,
                evidence={
                    "segment_index": index,
                    "previous_segment_index": index - 1,
                    "previous_end": previous_end,
                    "start": start,
                    "overlap_s": round(previous_end - start, 6),
                },
            )))
        if duration is not None and (start > duration + 0.05 or end > duration + 0.05):
            found.append((index, _Occurrence(
                code="LYRIC_OUTSIDE_ASSET", severity="FAIL", category="Timing",
                summary="Lyric event falls outside asset duration",
                description="A final lyric event extends beyond the delivered media.",
                seconds=min(start, duration), detector="timeline_invariants", auto_fixable=False,
                evidence={"segment_index": index, "start": start, "end": end, "duration": duration},
            )))
        previous_end = max(previous_end, end)
    return found


def _reference_health_occurrences(
    health: Mapping[str, Any] | None,
    segments: Sequence[Mapping[str, Any]],
) -> list[tuple[int, _Occurrence]]:
    if not isinstance(health, Mapping):
        return []
    found: list[tuple[int, _Occurrence]] = []
    metrics = health.get("metrics") or {}
    text_status = _text(health.get("text_status"))
    if not bool(health.get("allow_vocabulary_reconciliation", False)):
        found.append((0, _Occurrence(
            code="REFERENCE_TEXT_UNATTESTED",
            severity="FAIL",
            category="Reference integrity",
            summary="Reference lyrics are not independently supported",
            description=(
                "The selected catalogue/reference text is unsafe for automatic "
                "vocabulary correction and requires another witness or operator approval."
            ),
            seconds=0.0,
            actual=text_status or "unknown",
            expected="trusted_or_independently_attested",
            detector="reference_health",
            confidence=1.0,
            auto_fixable=False,
            evidence={"reasons": list(health.get("reasons") or []), "metrics": dict(metrics)},
        )))
    if _text(health.get("timeline_status")) == "incomplete":
        last_end = max((_segment_end(row) for row in segments), default=0.0)
        found.append((max(0, len(segments) - 1), _Occurrence(
            code="REFERENCE_TIMELINE_INCOMPLETE",
            severity="FAIL",
            category="Coverage",
            summary="Performance timeline is materially incomplete",
            description=(
                "The observed lyric timeline leaves a large gap or unfinished tail; "
                "whole-song alignment must not be delivered."
            ),
            seconds=last_end,
            actual=f"completion={metrics.get('timeline_completion_ratio')}",
            expected="complete_performance_timeline",
            detector="reference_health",
            confidence=1.0,
            auto_fixable=False,
            evidence={"reasons": list(health.get("reasons") or []), "metrics": dict(metrics)},
        )))
    return found


def _acoustic_finding_occurrences(
    findings: Sequence[Mapping[str, Any]] | None,
    segments: Sequence[Mapping[str, Any]],
) -> list[tuple[int, _Occurrence]]:
    found: list[tuple[int, _Occurrence]] = []
    for finding in findings or []:
        if not isinstance(finding, Mapping):
            continue
        code = _text(finding.get("code"))
        if code != "PREMATURE_LYRIC_END":
            continue
        try:
            index = int(finding.get("segment_index"))
            segment = segments[index]
        except (TypeError, ValueError, IndexError):
            continue
        current_end = _segment_end(segment)
        proposed_end = _finite_number(finding.get("proposed_end"))
        if proposed_end is None or proposed_end <= current_end + 0.075:
            continue
        score = _finite_number(finding.get("consensus_score")) or 0.0
        certified = bool(finding.get("certified_for_shadow"))
        found.append((index, _Occurrence(
            code=code,
            severity="WARN",
            category="Timing",
            summary="Possible prematurely clipped sung ending",
            description=(
                f"Stem/mix pitch evidence proposes extending this event from "
                f"{current_end:.3f}s to {proposed_end:.3f}s."
            ),
            seconds=current_end,
            actual=f"{current_end:.3f}",
            expected=f"{proposed_end:.3f}",
            detector="multi_view_endpoint_consensus",
            confidence=score,
            auto_fixable=certified,
            evidence=dict(finding),
        )))
    return found


def build_delivery_preflight(
    *,
    metadata: Mapping[str, Any] | None,
    segments: Sequence[Mapping[str, Any]],
    approved_lyrics: Sequence[Mapping[str, Any] | str] | None = None,
    reference_trusted: bool = False,
    asset: Mapping[str, Any] | None = None,
    quality: Mapping[str, Any] | None = None,
    reference_health: Mapping[str, Any] | None = None,
    acoustic_findings: Sequence[Mapping[str, Any]] | None = None,
    fps: float = 30.0,
) -> dict[str, Any]:
    """Build a label-style QC report for a final delivery candidate.

    ``approved_lyrics`` may influence spelling only when
    ``reference_trusted=True``.  This makes it impossible for an unverified
    catalogue page to silently overwrite a live performance.
    """
    meta = dict(metadata or {})
    asset_row = dict(asset or {})
    segment_rows = [dict(item) for item in segments if isinstance(item, Mapping)]
    duration = _finite_number(asset_row.get("duration"))
    effective_fps = _finite_number(fps)
    if effective_fps is None or effective_fps <= 0:
        effective_fps = 30.0
    occurrences: list[tuple[int, _Occurrence]] = []
    occurrences.extend(_metadata_occurrences(meta, asset_row))
    occurrences.extend(_timeline_occurrences(segment_rows, duration))
    occurrences.extend(_reference_health_occurrences(reference_health, segment_rows))
    occurrences.extend(_acoustic_finding_occurrences(acoustic_findings, segment_rows))
    if approved_lyrics:
        occurrences.extend(_lyric_occurrences(
            segment_rows, approved_lyrics, reference_trusted=reference_trusted
        ))

    issues = _aggregate(
        occurrences, total_segments=len(segment_rows), fps=effective_fps,
    )
    fail_count = sum(item["severity"] == "FAIL" for item in issues)
    warn_count = sum(item["severity"] == "WARN" for item in issues)
    decision = "BLOCK" if fail_count else "REVIEW" if warn_count else "PASS"
    abstentions: list[dict[str, str]] = []
    if approved_lyrics and not reference_trusted:
        abstentions.append({
            "detector": "lyric_reference_comparison",
            "reason": "reference_not_trusted",
        })
    if not asset_row.get("rendered_title") and not asset_row.get("title_card"):
        abstentions.append({
            "detector": "metadata_vs_rendered_title",
            "reason": "rendered_title_or_ocr_missing",
        })

    quality_verdict = _text(
        (quality or {}).get("decision") or (quality or {}).get("verdict")
    ).lower()
    if quality_verdict in {"unsafe", "fail", "blocked"}:
        decision = "BLOCK"
    elif quality_verdict == "review_required" and decision == "PASS":
        decision = "REVIEW"

    report = {
        "schema_version": SCHEMA_VERSION,
        "mode": "observe",
        "decision": decision,
        "asset": {
            "artist": meta.get("artist"),
            "title": meta.get("title"),
            "version": meta.get("version"),
            "isrc": meta.get("isrc"),
            "filename": asset_row.get("filename"),
            "duration": duration,
            "fps": effective_fps,
        },
        "summary": {
            "issue_count": len(issues),
            "fail_count": fail_count,
            "warn_count": warn_count,
            "open_count": len(issues),
            "segment_count": len(segment_rows),
        },
        "issues": issues,
        "abstentions": abstentions,
        "upstream_quality": dict(quality or {}),
        "reference_health": dict(reference_health or {}),
        "acoustic_finding_count": len(acoustic_findings or []),
    }
    return _json_safe(report)
