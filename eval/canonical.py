"""Canonical golden-set conversion and derived edit reconstruction."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, Sequence

from eval.metrics import align_lines, normalize_text

RAW_QUALITY_MAP = {
    "exact_checkpoint": "exact",
    "exact_from_recorded_diffs": "reconstructed",
    "estimated": "estimated",
    "rerun_required": "none",
}
RAW_QUALITIES = {"exact", "reconstructed", "estimated", "none"}
JOB_ORIGINS = {"staging", "production"}


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True, default=str) + "\n",
        encoding="utf-8",
    )


def canonical_sha256(value: Any) -> str:
    payload = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def infer_kind(text: str) -> tuple[str, bool]:
    """Derive only the editorial class supported by stored text.

    Entirely parenthesized lines are ad-libs. All other lines remain main;
    spoken/feat cannot be inferred safely without audio or performer metadata.
    The boolean records that the class is derived.
    """
    stripped = text.strip()
    if stripped.startswith("(") and stripped.endswith(")"):
        return "adlib", True
    return "main", True


def segments_to_lines(segments: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    lines = []
    for index, segment in enumerate(segments):
        text = str(segment.get("text") or "")
        kind, derived = infer_kind(text)
        lines.append({
            "idx": index,
            "start_s": float(segment.get("start", segment.get("start_s", 0))),
            "end_s": float(segment.get("end", segment.get("end_s", 0))),
            "text": text,
            "kind": kind,
            "kind_derived": derived,
        })
    return lines


def segments_to_words(segments: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    words = []
    for line_index, segment in enumerate(segments):
        for raw in segment.get("words") or []:
            if raw.get("start") is None or raw.get("end") is None:
                continue
            words.append({
                "line_idx": line_index,
                "start_s": float(raw["start"]),
                "end_s": float(raw["end"]),
                "text": str(raw.get("word", raw.get("text", ""))).strip(),
            })
    return words


def derive_edits(
    raw_segments: Sequence[dict[str, Any]], approved_segments: Sequence[dict[str, Any]],
) -> list[dict[str, Any]]:
    raw = segments_to_lines(raw_segments)
    approved = segments_to_lines(approved_segments)
    alignment = align_lines(approved, raw)
    edits: list[dict[str, Any]] = []

    def append(line_idx: int | None, op: str, field: str, before: Any, after: Any) -> None:
        edits.append({
            "seq": len(edits) + 1,
            "timestamp": None,
            "user": None,
            "line_idx": line_idx,
            "op": op,
            "field": field,
            "before": before,
            "after": after,
            "derived": True,
        })

    for match in alignment["matches"]:
        approved_idx, raw_idx = match["ref_idx"], match["hyp_idx"]
        before, after = raw[raw_idx], approved[approved_idx]
        if normalize_text(before["text"]) != normalize_text(after["text"]):
            append(raw_idx, "text_edit", "text", before["text"], after["text"])
        if abs(before["start_s"] - after["start_s"]) > 1e-9:
            append(raw_idx, "start_edit", "start_s", before["start_s"], after["start_s"])
        if abs(before["end_s"] - after["end_s"]) > 1e-9:
            append(raw_idx, "end_edit", "end_s", before["end_s"], after["end_s"])
        if before["kind"] != after["kind"]:
            append(raw_idx, "kind_changed", "kind", before["kind"], after["kind"])
    for raw_idx in alignment["invented_hyp_indices"]:
        append(raw_idx, "line_deleted", "line", raw[raw_idx], None)
    for approved_idx in alignment["omitted_ref_indices"]:
        append(approved_idx, "line_added", "line", None, approved[approved_idx])
    return edits


def safe_extension(filename: str | None) -> str:
    suffix = Path(filename or "").suffix.lower()
    return suffix if re.fullmatch(r"\.[a-z0-9]{1,8}", suffix) else ".audio"
