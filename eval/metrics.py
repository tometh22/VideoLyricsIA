"""Deterministic text, alignment, timing, error, and edit-effort metrics."""

from __future__ import annotations

import math
import re
import statistics
import unicodedata
from collections import Counter
from difflib import SequenceMatcher
from typing import Any, Iterable, Sequence

from scipy.optimize import linear_sum_assignment

KINDS = {"main", "adlib", "spoken", "feat"}
MATCH_THRESHOLD = 0.35
TIMING_OK_MS = 150.0
MAX_TEXT_ONLY_DRIFT_S = 10.0
NORMALIZATION_VERSION = "genly-wer-nfd-strip-marks-v1"


def normalize_text(text: str) -> str:
    """Apply the golden-set normalization contract to a string.

    NFD decomposition removes combining marks, letters and numbers remain,
    punctuation becomes whitespace, and runs of whitespace collapse.
    Numbers intentionally remain numeric.
    """
    decomposed = unicodedata.normalize("NFD", text or "").casefold()
    chars = [
        char if (char.isalnum() or char.isspace()) else " "
        for char in decomposed
        if not unicodedata.combining(char)
    ]
    return " ".join("".join(chars).split())


def main_text(lines: Sequence[dict[str, Any]]) -> str:
    chunks = []
    for line in lines:
        if line.get("kind", "main") != "main":
            continue
        chunks.append(re.sub(r"\([^)]*\)", " ", str(line.get("text") or "")))
    return " ".join(chunks)


def full_text(lines: Sequence[dict[str, Any]]) -> str:
    return " ".join(str(line.get("text") or "") for line in lines)


def _levenshtein(left: Sequence[Any], right: Sequence[Any]) -> int:
    if len(left) < len(right):
        left, right = right, left
    previous = list(range(len(right) + 1))
    for row, left_item in enumerate(left, start=1):
        current = [row]
        for column, right_item in enumerate(right, start=1):
            current.append(min(
                current[-1] + 1,
                previous[column] + 1,
                previous[column - 1] + (left_item != right_item),
            ))
        previous = current
    return previous[-1]


def error_rate_counts(reference: str, hypothesis: str) -> dict[str, int]:
    reference_norm = normalize_text(reference)
    hypothesis_norm = normalize_text(hypothesis)
    reference_words = reference_norm.split()
    hypothesis_words = hypothesis_norm.split()
    reference_chars = list(reference_norm.replace(" ", ""))
    hypothesis_chars = list(hypothesis_norm.replace(" ", ""))
    return {
        "word_edits": _levenshtein(reference_words, hypothesis_words),
        "reference_words": len(reference_words),
        "character_edits": _levenshtein(reference_chars, hypothesis_chars),
        "reference_characters": len(reference_chars),
    }


def _rate(edits: int, reference_count: int) -> float:
    return edits / max(1, reference_count)


def text_similarity(left: str, right: str) -> float:
    left_norm, right_norm = normalize_text(left), normalize_text(right)
    if not left_norm and not right_norm:
        return 1.0
    maximum = max(len(left_norm), len(right_norm), 1)
    return 1.0 - (_levenshtein(list(left_norm), list(right_norm)) / maximum)


def interval_iou(left: dict[str, Any], right: dict[str, Any]) -> float:
    intersection = max(
        0.0,
        min(float(left["end_s"]), float(right["end_s"]))
        - max(float(left["start_s"]), float(right["start_s"])),
    )
    union = max(float(left["end_s"]), float(right["end_s"])) - min(
        float(left["start_s"]), float(right["start_s"])
    )
    return intersection / union if union > 0 else 0.0


def validate_lines(lines: Sequence[dict[str, Any]], label: str) -> list[dict[str, Any]]:
    validated = []
    for position, raw in enumerate(lines):
        line = dict(raw)
        start = float(line["start_s"])
        end = float(line["end_s"])
        if not math.isfinite(start) or not math.isfinite(end) or start < 0 or end < start:
            raise ValueError(f"{label}[{position}] has invalid timing")
        kind = str(line.get("kind") or "main")
        if kind not in KINDS:
            raise ValueError(f"{label}[{position}] has invalid kind {kind!r}")
        validated.append({
            **line,
            "idx": int(line.get("idx", position)),
            "start_s": start,
            "end_s": end,
            "text": str(line.get("text") or ""),
            "kind": kind,
        })
    return validated


def align_lines(
    reference: Sequence[dict[str, Any]], hypothesis: Sequence[dict[str, Any]],
    *, threshold: float = MATCH_THRESHOLD,
) -> dict[str, Any]:
    reference = validate_lines(reference, "reference")
    hypothesis = validate_lines(hypothesis, "hypothesis")
    matrix: list[list[float]] = []
    candidates: list[list[bool]] = []
    for ref in reference:
        score_row, candidate_row = [], []
        for hyp in hypothesis:
            iou = interval_iou(ref, hyp)
            similarity = text_similarity(ref["text"], hyp["text"])
            center_drift = abs(
                ((ref["start_s"] + ref["end_s"]) / 2)
                - ((hyp["start_s"] + hyp["end_s"]) / 2)
            )
            # Text-only matches are useful for modest boundary drift, but an
            # identical chorus tens of seconds away is a different occurrence.
            # Letting it match produces spectacularly wrong timing metrics.
            candidate = not (
                iou == 0
                and (similarity < 0.5 or center_drift > MAX_TEXT_ONLY_DRIFT_S)
            )
            score_row.append((0.6 * iou + 0.4 * similarity) if candidate else 0.0)
            candidate_row.append(candidate)
        matrix.append(score_row)
        candidates.append(candidate_row)

    matches: list[dict[str, Any]] = []
    matched_ref: set[int] = set()
    matched_hyp: set[int] = set()
    if reference and hypothesis:
        rows, columns = linear_sum_assignment(
            [[1.0 - score for score in row] for row in matrix]
        )
        for ref_idx, hyp_idx in zip(rows.tolist(), columns.tolist()):
            score = matrix[ref_idx][hyp_idx]
            if candidates[ref_idx][hyp_idx] and score >= threshold:
                matches.append({
                    "ref_idx": ref_idx,
                    "hyp_idx": hyp_idx,
                    "score": score,
                    "iou_t": interval_iou(reference[ref_idx], hypothesis[hyp_idx]),
                    "sim_txt": text_similarity(
                        reference[ref_idx]["text"], hypothesis[hyp_idx]["text"]
                    ),
                })
                matched_ref.add(ref_idx)
                matched_hyp.add(hyp_idx)
    matches.sort(key=lambda item: (item["ref_idx"], item["hyp_idx"]))
    return {
        "schema_version": 1,
        "threshold": threshold,
        "score_matrix": matrix,
        "matches": matches,
        "omitted_ref_indices": [i for i in range(len(reference)) if i not in matched_ref],
        "invented_hyp_indices": [i for i in range(len(hypothesis)) if i not in matched_hyp],
    }


def _distribution(values: Iterable[float]) -> dict[str, float | None]:
    ordered = sorted(float(value) for value in values)
    if not ordered:
        return {"count": 0, "p50": None, "p90": None, "mean": None, "max": None}

    def percentile(fraction: float) -> float:
        index = min(len(ordered) - 1, math.ceil(fraction * len(ordered)) - 1)
        return ordered[max(0, index)]

    return {
        "count": len(ordered),
        "p50": statistics.median(ordered),
        "p90": percentile(0.90),
        "mean": statistics.fmean(ordered),
        "max": ordered[-1],
    }


def _segmentation_errors(
    reference: Sequence[dict[str, Any]], hypothesis: Sequence[dict[str, Any]],
) -> list[tuple[int | None, int | None]]:
    errors: set[tuple[int | None, int | None]] = set()
    for hyp_idx, hyp in enumerate(hypothesis):
        refs = [i for i, ref in enumerate(reference) if interval_iou(ref, hyp) > 0.3]
        if any(right == left + 1 for left, right in zip(refs, refs[1:])):
            errors.add((refs[0], hyp_idx))
    for ref_idx, ref in enumerate(reference):
        hyps = [i for i, hyp in enumerate(hypothesis) if interval_iou(ref, hyp) > 0.3]
        if any(right == left + 1 for left, right in zip(hyps, hyps[1:])):
            errors.add((ref_idx, hyps[0]))
    return sorted(errors, key=lambda pair: (-1 if pair[0] is None else pair[0], -1 if pair[1] is None else pair[1]))


def score_song(
    song_id: str,
    reference_lines: Sequence[dict[str, Any]],
    hypothesis_lines: Sequence[dict[str, Any]],
    *, reference_words: Sequence[dict[str, Any]] | None = None,
    hypothesis_words: Sequence[dict[str, Any]] | None = None,
) -> tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]]]:
    reference = validate_lines(reference_lines, "reference")
    hypothesis = validate_lines(hypothesis_lines, "hypothesis")
    alignment = align_lines(reference, hypothesis)
    full_counts = error_rate_counts(full_text(reference), full_text(hypothesis))
    main_counts = error_rate_counts(main_text(reference), main_text(hypothesis))
    start_errors, end_errors, end_early = [], [], []
    timing_ok = start_ok = end_ok = 0
    errors: list[dict[str, Any]] = []

    def add_error(error_type: str, ref_idx: int | None, hyp_idx: int | None) -> None:
        ref = reference[ref_idx] if ref_idx is not None else None
        hyp = hypothesis[hyp_idx] if hyp_idx is not None else None
        errors.append({
            "song_id": song_id,
            "ref_idx": ref_idx,
            "hyp_idx": hyp_idx,
            "type": error_type,
            "ref_text": ref["text"] if ref else None,
            "hyp_text": hyp["text"] if hyp else None,
            "ref_start": ref["start_s"] if ref else None,
            "hyp_start": hyp["start_s"] if hyp else None,
            "ref_end": ref["end_s"] if ref else None,
            "hyp_end": hyp["end_s"] if hyp else None,
        })

    normalized_reference = [normalize_text(line["text"]) for line in reference]
    repeated = Counter(normalized_reference)
    for ref_idx in alignment["omitted_ref_indices"]:
        error_type = "omitted_repeat" if repeated[normalized_reference[ref_idx]] > 1 else "omitted_other"
        add_error(error_type, ref_idx, None)
    for hyp_idx in alignment["invented_hyp_indices"]:
        add_error("invented", None, hyp_idx)

    for match in alignment["matches"]:
        ref_idx, hyp_idx = match["ref_idx"], match["hyp_idx"]
        ref, hyp = reference[ref_idx], hypothesis[hyp_idx]
        start_delta = (hyp["start_s"] - ref["start_s"]) * 1000.0
        end_delta = (hyp["end_s"] - ref["end_s"]) * 1000.0
        start_errors.append(abs(start_delta))
        end_errors.append(abs(end_delta))
        start_ok += int(abs(start_delta) <= TIMING_OK_MS)
        end_ok += int(abs(end_delta) <= TIMING_OK_MS)
        timing_ok += int(abs(start_delta) <= TIMING_OK_MS and abs(end_delta) <= TIMING_OK_MS)
        if -end_delta > 0:
            end_early.append(-end_delta)
        if -end_delta > 300:
            add_error("end_cut", ref_idx, hyp_idx)
        if start_delta > 300:
            add_error("start_late", ref_idx, hyp_idx)
        elif start_delta < -300:
            add_error("start_early", ref_idx, hyp_idx)
        if match["sim_txt"] < 0.8:
            add_error("text_sub", ref_idx, hyp_idx)
    for ref_idx, hyp_idx in _segmentation_errors(reference, hypothesis):
        add_error("segmentation", ref_idx, hyp_idx)

    matched = len(alignment["matches"])
    word_errors: list[float] = []
    if reference_words and hypothesis_words:
        ref_by_text: dict[str, list[dict[str, Any]]] = {}
        for word in reference_words:
            ref_by_text.setdefault(normalize_text(str(word.get("text") or "")), []).append(word)
        offsets: Counter[str] = Counter()
        for word in hypothesis_words:
            key = normalize_text(str(word.get("text") or ""))
            position = offsets[key]
            offsets[key] += 1
            candidates = ref_by_text.get(key, [])
            if position < len(candidates):
                word_errors.append(abs(float(word["start_s"]) - float(candidates[position]["start_s"])) * 1000)

    metrics = {
        "schema_version": 1,
        "song_id": song_id,
        "wer_full": _rate(full_counts["word_edits"], full_counts["reference_words"]),
        "wer_main": _rate(main_counts["word_edits"], main_counts["reference_words"]),
        "cer_full": _rate(full_counts["character_edits"], full_counts["reference_characters"]),
        "cer_main": _rate(main_counts["character_edits"], main_counts["reference_characters"]),
        "edit_counts_full": full_counts,
        "edit_counts_main": main_counts,
        "line_recall": matched / max(1, len(reference)),
        "line_precision": matched / max(1, len(hypothesis)),
        "lines_omitted": len(alignment["omitted_ref_indices"]),
        "lines_invented": len(alignment["invented_hyp_indices"]),
        "start_abs_err_ms": _distribution(start_errors),
        "end_abs_err_ms": _distribution(end_errors),
        "end_early_ms": _distribution(end_early),
        "word_timing_err_ms": _distribution(word_errors),
        "timing_samples_ms": {
            "start_abs": start_errors,
            "end_abs": end_errors,
            "end_early": end_early,
            "word_start_abs": word_errors,
        },
        "pct_lines_start_ok": start_ok / max(1, matched),
        "pct_lines_end_ok": end_ok / max(1, matched),
        "pct_lines_timing_ok": timing_ok / max(1, matched),
    }
    metrics["song_perfect"] = bool(
        metrics["wer_main"] == 0
        and metrics["lines_omitted"] == 0
        and metrics["lines_invented"] == 0
        and metrics["pct_lines_timing_ok"] == 1
    )
    metrics["song_near_perfect"] = bool(
        metrics["wer_main"] <= 0.02
        and metrics["lines_omitted"] + metrics["lines_invented"] <= 1
        and metrics["pct_lines_timing_ok"] >= 0.95
    )
    return metrics, alignment, errors


def score_edit_effort(edits: Sequence[dict[str, Any]], raw_line_count: int) -> dict[str, Any]:
    operations = Counter(str(edit.get("op") or "unknown") for edit in edits)
    touched = {edit.get("line_idx") for edit in edits if edit.get("line_idx") is not None}
    timing_shifts = []
    word_corrections: Counter[tuple[str, str]] = Counter()
    timestamps = []
    for edit in edits:
        if edit.get("timestamp"):
            timestamps.append(str(edit["timestamp"]))
        if edit.get("op") in {"start_edit", "end_edit"}:
            try:
                timing_shifts.append(abs(float(edit["after"]) - float(edit["before"])) * 1000)
            except (KeyError, TypeError, ValueError):
                pass
        if edit.get("op") == "text_edit":
            before = normalize_text(str(edit.get("before") or "")).split()
            after = normalize_text(str(edit.get("after") or "")).split()
            matcher = SequenceMatcher(None, before, after)
            for tag, i1, i2, j1, j2 in matcher.get_opcodes():
                if tag == "replace":
                    word_corrections[(" ".join(before[i1:i2]), " ".join(after[j1:j2]))] += 1
    text_ops = sum(operations[op] for op in ("text_edit", "line_added", "line_deleted", "line_split", "line_merged", "kind_changed"))
    timing_ops = operations["start_edit"] + operations["end_edit"]
    return {
        "edit_count_total": len(edits),
        "edit_count_text": text_ops,
        "edit_count_timing": timing_ops,
        "lines_touched_pct": len(touched) / max(1, raw_line_count),
        "operations": dict(sorted(operations.items())),
        "timing_shift_ms": _distribution(timing_shifts),
        "timing_shift_samples_ms": timing_shifts,
        "word_corrections": [
            {"before": before, "after": after, "count": count}
            for (before, after), count in word_corrections.most_common(30)
        ],
        "first_edit_at": min(timestamps) if timestamps else None,
        "last_edit_at": max(timestamps) if timestamps else None,
    }


def aggregate_song_metrics(rows: Sequence[dict[str, Any]]) -> dict[str, Any]:
    if not rows:
        return {"songs": 0}
    scalar_names = [
        "wer_full", "wer_main", "cer_full", "cer_main", "line_recall",
        "line_precision", "lines_omitted", "lines_invented",
        "pct_lines_start_ok", "pct_lines_end_ok", "pct_lines_timing_ok",
    ]
    aggregate: dict[str, Any] = {"songs": len(rows)}
    for name in scalar_names:
        values = [(float(row[name]), row["song_id"]) for row in rows]
        distribution = _distribution(value for value, _ in values)
        distribution["worst_song_id"] = (
            max(values, key=lambda pair: pair[0])[1]
            if name not in {"line_recall", "line_precision", "pct_lines_start_ok", "pct_lines_end_ok", "pct_lines_timing_ok"}
            else min(values, key=lambda pair: pair[0])[1]
        )
        aggregate[name] = distribution
    aggregate["song_perfect_pct"] = sum(bool(row["song_perfect"]) for row in rows) / len(rows)
    aggregate["song_near_perfect_pct"] = sum(bool(row["song_near_perfect"]) for row in rows) / len(rows)
    aggregate["lines_omitted_total"] = sum(int(row["lines_omitted"]) for row in rows)
    aggregate["lines_invented_total"] = sum(int(row["lines_invented"]) for row in rows)
    word_edits = sum(row["edit_counts_main"]["word_edits"] for row in rows)
    reference_words = sum(row["edit_counts_main"]["reference_words"] for row in rows)
    aggregate["wer_main_corpus"] = _rate(word_edits, reference_words)
    for output_name, sample_name in (
        ("start_abs_err_ms", "start_abs"),
        ("end_abs_err_ms", "end_abs"),
        ("end_early_ms", "end_early"),
        ("word_timing_err_ms", "word_start_abs"),
    ):
        samples = [
            float(value)
            for row in rows
            for value in (row.get("timing_samples_ms") or {}).get(sample_name, [])
        ]
        aggregate[output_name] = _distribution(samples)
    return aggregate


def aggregate_edit_effort(rows: Sequence[dict[str, Any]]) -> dict[str, Any]:
    operations: Counter[str] = Counter()
    corrections: Counter[tuple[str, str]] = Counter()
    shifts: list[float] = []
    for row in rows:
        operations.update(row.get("operations") or {})
        shifts.extend((row.get("timing_shift_samples_ms") or []))
        for item in row.get("word_corrections") or []:
            corrections[(item["before"], item["after"])] += int(item["count"])
    return {
        "songs": len(rows),
        "edit_count_total": sum(int(row.get("edit_count_total", 0)) for row in rows),
        "edit_count_text": sum(int(row.get("edit_count_text", 0)) for row in rows),
        "edit_count_timing": sum(int(row.get("edit_count_timing", 0)) for row in rows),
        "lines_touched_pct": _distribution(float(row.get("lines_touched_pct", 0)) for row in rows),
        "operations": dict(sorted(operations.items())),
        "timing_shift_ms": _distribution(shifts),
        "word_corrections": [
            {"before": before, "after": after, "count": count}
            for (before, after), count in corrections.most_common(30)
        ],
    }
