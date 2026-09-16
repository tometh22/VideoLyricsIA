"""Normalize label QC findings and measure preflight regression recall."""
from __future__ import annotations

import hashlib
import re
import unicodedata
from typing import Any, Mapping, Sequence


SCHEMA_VERSION = "genly-external-qc-regression-v1"


def _text(value: Any) -> str:
    return str(value or "").strip()


def _fold(value: Any) -> str:
    normalized = unicodedata.normalize("NFKD", _text(value).casefold())
    return "".join(
        char for char in normalized
        if unicodedata.category(char) != "Mn"
    ).strip(" .,:;!?_-/")


def _parse_description(description: str) -> dict[str, str]:
    quoted = re.findall(r'"([^"]+)"', description)
    lowered = description.casefold()
    if "should be" in lowered and len(quoted) >= 2:
        actual, expected = quoted[0], quoted[1]
        code = (
            "LYRIC_ORTHOGRAPHY_MISMATCH"
            if _fold(actual) == _fold(expected) else "LYRIC_TOKEN_TYPO"
        )
        return {"code": code, "actual": actual, "expected": expected}
    if "sung lyric" in lowered and "appears" in lowered and len(quoted) >= 2:
        expected_line, actual_line = quoted[0], quoted[1].rstrip(".")
        expected_tokens = re.findall(r"[^\W_]+", expected_line, re.UNICODE)
        actual_tokens = re.findall(r"[^\W_]+", actual_line, re.UNICODE)
        differing = [
            (actual, expected)
            for actual, expected in zip(actual_tokens, expected_tokens)
            if _fold(actual) != _fold(expected)
        ] if len(actual_tokens) == len(expected_tokens) else []
        actual, expected = (
            differing[0] if len(differing) == 1
            else (actual_line, expected_line)
        )
        return {
            "code": "LYRIC_TOKEN_TYPO",
            "actual": actual, "expected": expected,
        }
    if "metadata" in lowered and "displays" in lowered and len(quoted) >= 2:
        expected, actual = quoted[0], quoted[1]
        return {
            "code": "METADATA_TITLE_MISMATCH",
            "actual": actual, "expected": expected,
        }
    return {}


def normalize_external_finding(raw: Mapping[str, Any]) -> dict[str, Any]:
    description = _text(raw.get("description") or raw.get("summary"))
    parsed = _parse_description(description)
    actual = _text(
        raw.get("actual")
        or (raw.get("normalised_detector_pair") or {}).get("actual")
        or raw.get("displayed_actual")
        or parsed.get("actual")
    ).rstrip(".")
    expected = _text(
        raw.get("expected")
        or (raw.get("normalised_detector_pair") or {}).get("expected")
        or raw.get("sung_expected")
        or parsed.get("expected")
    )
    code = _text(raw.get("code") or parsed.get("code"))
    if not code:
        lowered = description.casefold()
        if "metadata title mismatch" in lowered:
            code = "METADATA_TITLE_MISMATCH"
        elif "missing accent" in lowered and _fold(actual) == _fold(expected):
            code = "LYRIC_ORTHOGRAPHY_MISMATCH"
        elif "sung lyric mismatch" in lowered:
            code = "LYRIC_TOKEN_TYPO"
        else:
            code = "UNCLASSIFIED"
    timecodes = [
        _text(item) for item in (
            raw.get("timecodes") or raw.get("reported_timecodes")
            or ([raw.get("timecode")] if raw.get("timecode") else [])
        ) if _text(item)
    ]
    identity = hashlib.sha256(
        f"{code}|{_fold(actual)}|{_fold(expected)}|{'|'.join(timecodes)}".encode()
    ).hexdigest()[:16]
    return {
        "finding_id": _text(raw.get("finding_id")) or identity,
        "code": code,
        "category": _text(raw.get("category") or "Other Video Issues"),
        "severity": _text(raw.get("severity") or "WARN").upper(),
        "frequency": _text(raw.get("frequency") or "UNKNOWN").upper(),
        "description": description,
        "actual": actual,
        "expected": expected,
        "timecodes": timecodes,
    }


def normalize_external_report(
    *, source: str, report_id: str,
    findings: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    rows = [normalize_external_finding(item) for item in findings]
    return {
        "schema_version": SCHEMA_VERSION,
        "source": _text(source),
        "report_id": _text(report_id),
        "finding_count": len(rows),
        "findings": rows,
    }


def evaluate_preflight_recall(
    preflight_issues: Sequence[Mapping[str, Any]],
    external_findings: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Match label findings by code and normalized actual/expected text."""
    normalized = [normalize_external_finding(item) for item in external_findings]
    issues = [dict(item) for item in preflight_issues if isinstance(item, Mapping)]
    caught: list[str] = []
    missed: list[str] = []
    for finding in normalized:
        matched = any(
            str(issue.get("code") or "") == finding["code"]
            and _fold(issue.get("actual")) == _fold(finding["actual"])
            and _fold(issue.get("expected")) == _fold(finding["expected"])
            for issue in issues
        )
        (caught if matched else missed).append(finding["finding_id"])
    total = len(normalized)
    return {
        "schema_version": SCHEMA_VERSION,
        "expected_count": total,
        "caught_count": len(caught),
        "missed_count": len(missed),
        "recall": round(len(caught) / total, 6) if total else 1.0,
        "gate_passed": not missed,
        "caught_finding_ids": caught,
        "missed_finding_ids": missed,
    }
