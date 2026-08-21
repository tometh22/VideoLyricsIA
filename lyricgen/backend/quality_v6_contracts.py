"""Fail-closed contracts separating v6 diagnostics, proposals and mutations.

The types intentionally make it impossible to pass an arbitrary diagnostic to
the editor proposal persistence path.  ``CertifiedMutation`` exists only as a
non-constructible runtime boundary until a separately signed runtime
authorization artifact is introduced.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import math
import re
from typing import Any, Mapping, Sequence


POLICY_VERSION = "lyrics-quality-v6"
DIAGNOSTIC_SCHEMA = "lyrics-quality-v6-diagnostic-v1"
PROPOSAL_CANDIDATE_SCHEMA = "lyrics-quality-v6-proposal-candidate-v1"
PROPOSAL_WINDOW_SCHEMA = "lyrics-quality-v6-proposal-window-v1"
REVIEW_PROPOSAL_SCHEMA = "lyrics-quality-v6-review-proposal-v1"


def _finite(value: Any) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("timing must be finite")
    return number


_SEGMENT_KEYS = frozenset({
    "_id", "id", "segment_id", "start", "end", "text", "words",
    "locked", "review", "pos", "scale", "rot",
})
_IDENTIFIER_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:@-]{0,127}\Z")
_WORD_KEYS = frozenset({"word", "text", "start", "end", "score", "probability"})


def _identifier(value: Any, *, label: str, required: bool = True) -> str | None:
    if value is None and not required:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{label} is invalid")
    normalized = value.strip()
    if not _IDENTIFIER_RE.fullmatch(normalized):
        raise ValueError(f"{label} is invalid")
    return normalized


def _metadata_number(value: Any, *, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} must be numeric")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{label} must be finite")
    return number


def _segment_words(value: Any) -> list[dict]:
    words: list[dict] = []
    for raw in _mapping_sequence(value, label="segment words", maximum=512):
        unknown = set(raw) - _WORD_KEYS
        if unknown:
            raise ValueError("segment word metadata contains unknown fields")
        lexical_key = "word" if "word" in raw else "text" if "text" in raw else None
        if lexical_key is None:
            raise ValueError("segment word text is required")
        lexical_value = raw.get(lexical_key)
        if not isinstance(lexical_value, str) or len(lexical_value) > 256:
            raise ValueError("segment word text is invalid")
        word: dict[str, Any] = {lexical_key: lexical_value}
        for key in ("start", "end"):
            if key in raw:
                number = _metadata_number(raw[key], label=f"segment word {key}")
                if number < 0:
                    raise ValueError("segment word timing is invalid")
                word[key] = number
        if "start" in word and "end" in word and word["end"] < word["start"]:
            raise ValueError("segment word timing is invalid")
        for key in ("score", "probability"):
            if key in raw:
                number = _metadata_number(raw[key], label=f"segment word {key}")
                if not 0 <= number <= 1:
                    raise ValueError("segment word confidence is invalid")
                word[key] = number
        words.append(word)
    return words


def _segment_position(value: Any) -> dict[str, float]:
    if not isinstance(value, Mapping) or set(value) != {"x", "y"}:
        raise ValueError("segment position is invalid")
    return {
        "x": _metadata_number(value["x"], label="segment position x"),
        "y": _metadata_number(value["y"], label="segment position y"),
    }


def _validated_segment_metadata(raw: Mapping[str, Any]) -> dict[str, Any]:
    row: dict[str, Any] = {}
    for key in ("_id", "id", "segment_id"):
        if key in raw:
            row[key] = _identifier(raw[key], label=f"segment {key}")
    if "words" in raw:
        row["words"] = _segment_words(raw["words"])
    for key in ("locked", "review"):
        if key in raw:
            if not isinstance(raw[key], bool):
                raise ValueError(f"segment {key} must be boolean")
            row[key] = raw[key]
    if "pos" in raw:
        row["pos"] = _segment_position(raw["pos"])
    if "scale" in raw:
        row["scale"] = _metadata_number(raw["scale"], label="segment scale")
        if row["scale"] <= 0:
            raise ValueError("segment scale must be positive")
    if "rot" in raw:
        row["rot"] = _metadata_number(raw["rot"], label="segment rotation")
    return row


def _mapping_sequence(value: Any, *, label: str, maximum: int) -> tuple[Mapping, ...]:
    if not isinstance(value, (list, tuple)) or isinstance(value, (str, bytes)):
        raise ValueError(f"{label} must be an array")
    if len(value) > maximum:
        raise ValueError(f"{label} is too large")
    if not all(isinstance(item, Mapping) for item in value):
        raise ValueError(f"{label} entries must be objects")
    return tuple(value)


def _string_sequence(value: Any, *, label: str, maximum: int = 64) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)) or isinstance(value, (str, bytes)):
        raise ValueError(f"{label} must be an array")
    if len(value) > maximum or not all(isinstance(item, str) for item in value):
        raise ValueError(f"{label} is invalid")
    return tuple(sorted({item for item in value if item}))


def _proposal_segments(value: Any, *, start: float, end: float) -> tuple[dict, ...]:
    rows: list[dict] = []
    for raw in _mapping_sequence(
        value, label="proposal segments", maximum=256,
    ):
        row_start, row_end = _finite(raw.get("start")), _finite(raw.get("end"))
        if row_start < 0 or row_end <= row_start:
            raise ValueError("proposal segment timing is invalid")
        if row_end <= start or row_start >= end:
            raise ValueError("proposal segment lies outside its window")
        text = raw.get("text", "")
        if not isinstance(text, str) or len(text) > 2_000:
            raise ValueError("proposal segment text is invalid")
        row = {
            "start": row_start,
            "end": row_end,
            "text": text,
        }
        row.update(_validated_segment_metadata(raw))
        rows.append(row)
    return tuple(rows)


@dataclass(frozen=True)
class DiagnosticFinding:
    kind: str
    schema: str
    window_id: str
    start: float
    end: float
    reason_codes: tuple[str, ...]
    evidence_digest: str | None = None

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "DiagnosticFinding":
        if value.get("kind") != "diagnostic_finding":
            raise ValueError("diagnostic kind mismatch")
        if value.get("schema") != DIAGNOSTIC_SCHEMA:
            raise ValueError("diagnostic schema mismatch")
        start, end = _finite(value.get("start")), _finite(value.get("end"))
        if start < 0 or end <= start:
            raise ValueError("invalid diagnostic window")
        reasons = _string_sequence(
            value.get("reasons"), label="diagnostic reasons",
        )
        if not reasons:
            raise ValueError("diagnostic requires a reason")
        window_id = _identifier(
            value.get("id") or value.get("window_id"),
            label="diagnostic window id",
        )
        return cls(
            kind="diagnostic_finding", schema=DIAGNOSTIC_SCHEMA,
            window_id=window_id,
            start=start, end=end, reason_codes=reasons,
            evidence_digest=(str(value.get("evidence_digest"))
                             if value.get("evidence_digest") else None),
        )


@dataclass(frozen=True)
class ReviewProposalCandidate:
    """Tenant-scoped candidate emitted by content analysis, never a diagnostic."""

    kind: str
    schema: str
    id: str
    parent_window_id: str
    start: float
    end: float
    reasons: tuple[str, ...]
    current_segments: tuple[dict, ...]
    proposed_segments: tuple[dict, ...]
    certification: dict | None

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "ReviewProposalCandidate":
        if value.get("kind") != "review_proposal_candidate":
            raise ValueError("proposal candidate kind mismatch")
        if value.get("schema") != PROPOSAL_CANDIDATE_SCHEMA:
            raise ValueError("proposal candidate schema mismatch")
        candidate_id = _identifier(value.get("id"), label="proposal candidate id")
        parent_id = _identifier(
            value.get("parent_window_id"), label="proposal parent window id",
        )
        start, end = _finite(value.get("start")), _finite(value.get("end"))
        if start < 0 or end <= start:
            raise ValueError("invalid proposal candidate window")
        current = _proposal_segments(
            value.get("current_segments"), start=start, end=end,
        )
        proposed = _proposal_segments(
            value.get("proposed_segments"), start=start, end=end,
        )
        if not proposed:
            raise ValueError("proposal candidate requires proposed segments")
        certification = value.get("certification")
        if certification is not None and not isinstance(certification, Mapping):
            raise ValueError("proposal certification must be an object")
        return cls(
            kind="review_proposal_candidate",
            schema=PROPOSAL_CANDIDATE_SCHEMA,
            id=candidate_id,
            parent_window_id=parent_id,
            start=start,
            end=end,
            reasons=_string_sequence(
                value.get("reasons"), label="proposal candidate reasons",
            ),
            current_segments=current,
            proposed_segments=proposed,
            certification=dict(certification) if certification is not None else None,
        )


@dataclass(frozen=True)
class ReviewProposalWindow:
    kind: str
    schema: str
    id: str
    start: float
    end: float
    reasons: tuple[str, ...]
    current_segments: tuple[dict, ...]
    proposed_segments: tuple[dict, ...]

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "ReviewProposalWindow":
        if value.get("kind") != "review_proposal_window":
            raise ValueError("proposal window kind mismatch")
        if value.get("schema") != PROPOSAL_WINDOW_SCHEMA:
            raise ValueError("proposal window schema mismatch")
        start, end = _finite(value.get("start")), _finite(value.get("end"))
        if start < 0 or end <= start:
            raise ValueError("invalid proposal window")
        current = _proposal_segments(
            value.get("current_segments"), start=start, end=end,
        )
        proposed = _proposal_segments(
            value.get("proposed_segments"), start=start, end=end,
        )
        if not proposed:
            raise ValueError("proposal window requires proposed segments")
        return cls(
            kind="review_proposal_window", schema=PROPOSAL_WINDOW_SCHEMA,
            id=_identifier(value.get("id"), label="proposal window id"),
            start=start, end=end,
            reasons=_string_sequence(
                value.get("reasons"), label="proposal window reasons",
            ),
            current_segments=current, proposed_segments=proposed,
        )

    def to_dict(self) -> dict:
        return {
            "kind": self.kind, "schema": self.schema,
            "id": self.id, "start": self.start, "end": self.end,
            "reasons": list(self.reasons),
            "current_segments": [dict(item) for item in self.current_segments],
            "proposed_segments": [dict(item) for item in self.proposed_segments],
        }


@dataclass(frozen=True)
class ReviewProposal:
    kind: str
    schema: str
    policy_version: str
    windows: tuple[ReviewProposalWindow, ...]
    id: str | None = None
    review_only: bool = True

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "ReviewProposal":
        if value.get("kind") != "review_proposal":
            raise ValueError("proposal kind mismatch")
        if value.get("schema") != REVIEW_PROPOSAL_SCHEMA:
            raise ValueError("proposal schema mismatch")
        if value.get("policy_version") != POLICY_VERSION:
            raise ValueError("proposal policy mismatch")
        if value.get("review_only") is not True:
            raise ValueError("v6 proposals must be review-only")
        windows = tuple(
            ReviewProposalWindow.from_mapping(item)
            for item in _mapping_sequence(
                value.get("windows"), label="proposal windows", maximum=64,
            )
        )
        if not windows:
            raise ValueError("proposal requires at least one window")
        ids = [item.id for item in windows]
        if not all(ids) or len(ids) != len(set(ids)):
            raise ValueError("proposal window ids must be unique")
        proposal_id = _identifier(
            value.get("id"), label="proposal id", required=False,
        )
        return cls(
            kind="review_proposal", schema=REVIEW_PROPOSAL_SCHEMA,
            policy_version=POLICY_VERSION, windows=windows, id=proposal_id,
        )

    def to_dict(self) -> dict:
        payload = {
            "kind": self.kind,
            "schema": self.schema,
            "policy_version": self.policy_version,
            "review_only": True,
            "windows": [item.to_dict() for item in self.windows],
        }
        if self.id:
            payload["id"] = self.id
        return payload


@dataclass(frozen=True, init=False)
class CertifiedMutation:
    """Reserved boundary: v6 runtime cannot construct certified mutations."""

    def __init__(self, *_args, **_kwargs):
        raise RuntimeError("v6 certified mutation runtime is not authorized")


def proposal_expiry_iso(days: int = 7) -> str:
    from datetime import timedelta
    return (datetime.now(timezone.utc) + timedelta(days=days)).isoformat()
