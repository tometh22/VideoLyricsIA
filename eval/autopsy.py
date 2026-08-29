#!/usr/bin/env python3
"""Decompose the trustworthy prod-raw residual before choosing variants."""

from __future__ import annotations

import argparse
import csv
import json
import re
from collections import Counter
from pathlib import Path
from typing import Any, Sequence

from eval.bootstrap import percentile, song_bootstrap_ci
from eval.canonical import read_json, segments_to_lines, write_json
from eval.metrics import NORMALIZATION_VERSION, normalize_text, score_song

INTERJECTIONS = {
    "ah", "ay", "eh", "ey", "oh", "uh", "uy", "yeah", "hey", "wow",
    "na", "la", "uoh", "woo", "ooh",
}


def word_edit_operations(reference_text: str, hypothesis_text: str) -> list[dict[str, Any]]:
    """Return a deterministic minimal word-edit traceback."""
    reference = [{"word": word, "line_idx": 0, "position": index, "length": len(normalize_text(reference_text).split()), "original_line": reference_text} for index, word in enumerate(normalize_text(reference_text).split())]
    hypothesis = [{"word": word, "line_idx": 0, "position": index, "length": len(normalize_text(hypothesis_text).split()), "original_line": hypothesis_text} for index, word in enumerate(normalize_text(hypothesis_text).split())]
    return _word_edit_operations_tokens(reference, hypothesis)


def _main_tokens(lines: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    tokens = []
    for line_index, line in enumerate(lines):
        if line.get("kind", "main") != "main":
            continue
        original = re.sub(r"\([^)]*\)", " ", str(line.get("text") or ""))
        words = normalize_text(original).split()
        for position, word in enumerate(words):
            tokens.append({
                "word": word, "line_idx": line_index, "position": position,
                "length": len(words), "original_line": str(line.get("text") or ""),
            })
    return tokens


def _word_edit_operations_tokens(
    reference_tokens: Sequence[dict[str, Any]], hypothesis_tokens: Sequence[dict[str, Any]],
) -> list[dict[str, Any]]:
    reference = [token["word"] for token in reference_tokens]
    hypothesis = [token["word"] for token in hypothesis_tokens]
    rows, columns = len(reference), len(hypothesis)
    dp = [[0] * (columns + 1) for _ in range(rows + 1)]
    for i in range(rows + 1):
        dp[i][0] = i
    for j in range(columns + 1):
        dp[0][j] = j
    for i in range(1, rows + 1):
        for j in range(1, columns + 1):
            dp[i][j] = min(
                dp[i - 1][j] + 1,
                dp[i][j - 1] + 1,
                dp[i - 1][j - 1] + (reference[i - 1] != hypothesis[j - 1]),
            )
    edits = []
    i, j = rows, columns
    while i or j:
        if i and j and reference[i - 1] == hypothesis[j - 1] and dp[i][j] == dp[i - 1][j - 1]:
            i, j = i - 1, j - 1
        elif i and j and dp[i][j] == dp[i - 1][j - 1] + 1:
            ref_token, hyp_token = reference_tokens[i - 1], hypothesis_tokens[j - 1]
            edits.append({"type": "substitution", "ref_word": reference[i - 1], "hyp_word": hypothesis[j - 1], "ref_position": ref_token["position"], "ref_length": ref_token["length"], "ref_line_idx": ref_token["line_idx"], "hyp_line_idx": hyp_token["line_idx"], "original_reference": ref_token["original_line"]})
            i, j = i - 1, j - 1
        elif i and dp[i][j] == dp[i - 1][j] + 1:
            ref_token = reference_tokens[i - 1]
            edits.append({"type": "deletion", "ref_word": reference[i - 1], "hyp_word": None, "ref_position": ref_token["position"], "ref_length": ref_token["length"], "ref_line_idx": ref_token["line_idx"], "hyp_line_idx": None, "original_reference": ref_token["original_line"]})
            i -= 1
        else:
            hyp_token = hypothesis_tokens[j - 1]
            anchor = reference_tokens[min(i, len(reference_tokens) - 1)] if reference_tokens else None
            edits.append({"type": "insertion", "ref_word": None, "hyp_word": hypothesis[j - 1], "ref_position": anchor["position"] if anchor else hyp_token["position"], "ref_length": anchor["length"] if anchor else hyp_token["length"], "ref_line_idx": anchor["line_idx"] if anchor else None, "hyp_line_idx": hyp_token["line_idx"], "original_reference": anchor["original_line"] if anchor else ""})
            j -= 1
    edits.reverse()
    return edits


def _position(edit: dict[str, Any]) -> str:
    length, position = int(edit["ref_length"]), int(edit["ref_position"])
    if length <= 1 or position == length - 1:
        return "last_word"
    if position == 0:
        return "first_word"
    return "line_interior"


def _word_class(edit: dict[str, Any], original_reference: str) -> str:
    word = str(edit.get("ref_word") or edit.get("hyp_word") or "")
    if word in INTERJECTIONS or re.fullmatch(r"(?:oh|ah|eh|uh|la|na)+", word):
        return "interjection"
    original_tokens = re.findall(r"[^\W_]+", original_reference, flags=re.UNICODE)
    normalized_original = [normalize_text(token) for token in original_tokens]
    if word in normalized_original:
        index = normalized_original.index(word)
        if index > 0 and original_tokens[index][:1].isupper():
            return "proper_name_candidate"
    return "common_or_slang_unresolved"


def _add_bucket(counter: Counter[str], key: str, amount: int = 1) -> None:
    counter[key] += amount


def analyze(golden: Path, output: Path, qualities: set[str]) -> dict[str, Any]:
    manifest = read_json(golden / "manifest.json")
    word_rows: list[dict[str, Any]] = []
    timing_rows: list[dict[str, Any]] = []
    song_rows: list[dict[str, Any]] = []
    buckets: dict[str, Counter[str]] = {
        "type": Counter(), "position": Counter(), "class": Counter(),
        "language": Counter(), "repeat_context": Counter(),
    }
    observed_edit_count = derived_edit_count = 0
    repeat_reference_words: Counter[str] = Counter()
    for item in manifest["cases"]:
        case = golden / item["path"]
        meta = read_json(case / "meta.json")
        if meta["raw_quality"] not in qualities or not meta.get("has_raw"):
            continue
        reference = read_json(case / "lines.json")
        raw_payload = read_json(case / "raw_pipeline_output.json")
        hypothesis = segments_to_lines(raw_payload["segments"])
        metrics, _, _ = score_song(item["song_id"], reference, hypothesis)
        repeat_counts = Counter(normalize_text(line["text"]) for line in reference)
        for line in reference:
            if line.get("kind", "main") != "main":
                continue
            normalized = normalize_text(re.sub(r"\([^)]*\)", " ", line["text"]))
            context = "repeated_line" if repeat_counts[normalize_text(line["text"])] > 1 else "unique_line"
            repeat_reference_words[context] += len(normalized.split())
        song_edits = _word_edit_operations_tokens(_main_tokens(reference), _main_tokens(hypothesis))
        if len(song_edits) != metrics["edit_counts_main"]["word_edits"]:
            raise RuntimeError(f"word traceback diverged from WER numerator for {item['song_id']}")
        for edit in song_edits:
            ref_idx, hyp_idx = edit.get("ref_line_idx"), edit.get("hyp_line_idx")
            position = _position(edit)
            word_class = _word_class(edit, edit.get("original_reference") or "")
            if ref_idx is None:
                repeat_context = "unmatched_hypothesis"
            else:
                repeat_context = "repeated_line" if repeat_counts[normalize_text(reference[ref_idx]["text"])] > 1 else "unique_line"
            language = str((meta.get("language") or {}).get("value") or "unknown")
            row = {"song_id": item["song_id"], "ref_idx": ref_idx, "hyp_idx": hyp_idx, **edit, "position": position, "class": word_class, "language": language, "repeat_context": repeat_context}
            word_rows.append(row)
            for bucket, value in (("type", edit["type"]), ("position", position), ("class", word_class), ("language", language), ("repeat_context", repeat_context)):
                _add_bucket(buckets[bucket], value)
        song_error_count = len(song_edits)
        song_rows.append({
            "song_id": item["song_id"], "word_errors": song_error_count,
            "reference_words": metrics["edit_counts_main"]["reference_words"],
            "wer_main": metrics["wer_main"],
            "end_abs_samples_ms": metrics["timing_samples_ms"]["end_abs"],
        })
        edits_path = case / "edits.json"
        for edit in (read_json(edits_path) if edits_path.is_file() else []):
            derived = bool(edit.get("derived"))
            derived_edit_count += int(derived)
            observed_edit_count += int(not derived)
            if edit.get("op") not in {"start_edit", "end_edit"}:
                continue
            try:
                before, after = float(edit["before"]), float(edit["after"])
            except (KeyError, TypeError, ValueError):
                continue
            delta_ms = (after - before) * 1000.0
            timing_rows.append({
                "song_id": item["song_id"], "line_idx": edit.get("line_idx"),
                "boundary": "start" if edit["op"] == "start_edit" else "end",
                "position_in_line": "line_start" if edit["op"] == "start_edit" else "last_word_or_line_end",
                "before_s": before, "after_s": after, "delta_ms": delta_ms,
                "direction": "later" if delta_ms > 0 else "earlier" if delta_ms < 0 else "unchanged",
                "derived": derived, "source": edit.get("source", "initial_final_diff"),
            })

    total_errors = len(word_rows)
    bucket_report = {
        name: [
            {"bucket": key, "errors": count, "pct_error_total": count / max(1, total_errors)}
            for key, count in counter.most_common()
        ]
        for name, counter in buckets.items()
    }
    repeat_context_rates = [
        {
            "context": context,
            "errors": buckets["repeat_context"].get(context, 0),
            "reference_words": repeat_reference_words.get(context, 0),
            "error_rate": buckets["repeat_context"].get(context, 0) / max(1, repeat_reference_words.get(context, 0)),
        }
        for context in ("repeated_line", "unique_line")
    ]
    wer_ci = song_bootstrap_ci(
        song_rows,
        lambda sample: sum(row["word_errors"] for row in sample) / max(1, sum(row["reference_words"] for row in sample)),
    )
    end_ci = song_bootstrap_ci(
        song_rows,
        lambda sample: percentile([value for row in sample for value in row["end_abs_samples_ms"]], 0.90),
    )
    timing_magnitudes = [abs(row["delta_ms"]) for row in timing_rows]
    observed_timing = [row for row in timing_rows if not row["derived"]]
    by_song_timing = []
    for song_id in sorted({row["song_id"] for row in timing_rows}):
        rows = [row for row in timing_rows if row["song_id"] == song_id]
        observed = [row for row in rows if not row["derived"]]
        by_song_timing.append({
            "song_id": song_id,
            "corrections": len(rows),
            "observed_corrections": len(observed),
            "earlier": sum(row["direction"] == "earlier" for row in rows),
            "later": sum(row["direction"] == "later" for row in rows),
            "observed_earlier": sum(row["direction"] == "earlier" for row in observed),
            "observed_later": sum(row["direction"] == "later" for row in observed),
            "end_boundary_corrections": sum(row["boundary"] == "end" for row in rows),
            "abs_delta_p50_ms": percentile([abs(row["delta_ms"]) for row in rows], 0.50),
            "abs_delta_p90_ms": percentile([abs(row["delta_ms"]) for row in rows], 0.90),
        })
    report = {
        "schema_version": 1, "cohort_raw_qualities": sorted(qualities),
        "normalization_version": NORMALIZATION_VERSION,
        "songs": len(song_rows), "word_errors": total_errors,
        "wer_main_song_bootstrap_ci": wer_ci,
        "end_abs_p90_ms_song_bootstrap_ci": end_ci,
        "buckets": bucket_report,
        "repeat_context_rates": repeat_context_rates,
        "timing_corrections": {
            "count": len(timing_rows),
            "observed_count": sum(not row["derived"] for row in timing_rows),
            "derived_count": sum(row["derived"] for row in timing_rows),
            "later_count": sum(row["direction"] == "later" for row in timing_rows),
            "earlier_count": sum(row["direction"] == "earlier" for row in timing_rows),
            "abs_delta_p50_ms": percentile(timing_magnitudes, 0.50) if timing_magnitudes else None,
            "abs_delta_p90_ms": percentile(timing_magnitudes, 0.90) if timing_magnitudes else None,
            "observed_only": {
                "count": len(observed_timing),
                "later_count": sum(row["direction"] == "later" for row in observed_timing),
                "earlier_count": sum(row["direction"] == "earlier" for row in observed_timing),
                "end_boundary_count": sum(row["boundary"] == "end" for row in observed_timing),
                "start_boundary_count": sum(row["boundary"] == "start" for row in observed_timing),
            },
            "by_song": by_song_timing,
        },
        "edit_provenance": {"observed": observed_edit_count, "derived": derived_edit_count},
        "songs_by_error_contribution": [
            {**row, "pct_error_total": row["word_errors"] / max(1, total_errors)}
            for row in sorted(song_rows, key=lambda row: row["word_errors"], reverse=True)
        ],
        "provisional": observed_edit_count == 0,
    }
    output.mkdir(parents=True, exist_ok=True)
    write_json(output / "autopsy.json", report)
    _write_csv(output / "word_errors.csv", word_rows)
    _write_csv(output / "timing_corrections.csv", timing_rows)
    (output / "autopsy.md").write_text(render_markdown(report), encoding="utf-8")
    return report


def _write_csv(path: Path, rows: Sequence[dict[str, Any]]) -> None:
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def render_markdown(report: dict[str, Any]) -> str:
    wer, end = report["wer_main_song_bootstrap_ci"], report["end_abs_p90_ms_song_bootstrap_ci"]
    lines = [
        "# Autopsia del residuo", "",
        f"Cohorte: `{', '.join(report['cohort_raw_qualities'])}`; canciones: **{report['songs']}**.",
        f"WER por bootstrap de canción: **{100*wer['estimate']:.2f}%** (CI95 {100*wer['low']:.2f}–{100*wer['high']:.2f}%).",
        f"Final p90: **{end['estimate']:.0f} ms** (CI95 {end['low']:.0f}–{end['high']:.0f} ms).", "",
    ]
    if report["provisional"]:
        lines.extend(["> PROVISIONAL: los edits disponibles son diffs inicial→final derivados; todavía no son todos los eventos observados de `audit_log`.", ""])
    for name, rows in report["buckets"].items():
        lines.extend([f"## {name}", "", "| Bucket | Errores | % del total |", "|---|---:|---:|"])
        lines.extend(f"| {row['bucket']} | {row['errors']} | {100*row['pct_error_total']:.1f}% |" for row in rows)
        lines.append("")
    lines.extend(["## repeat_context_rate", "", "| Contexto | Errores | Palabras | Tasa |", "|---|---:|---:|---:|"])
    lines.extend(
        f"| {row['context']} | {row['errors']} | {row['reference_words']} | {100*row['error_rate']:.2f}% |"
        for row in report["repeat_context_rates"]
    )
    lines.append("")
    timing = report["timing_corrections"]
    lines.extend([
        "## Timing humano", "",
        f"Cambios: {timing['count']} (observados {timing['observed_count']}, derivados {timing['derived_count']}); hacia más tarde {timing['later_count']}, hacia más temprano {timing['earlier_count']}.",
        f"Magnitud p50/p90: {timing['abs_delta_p50_ms']:.0f}/{timing['abs_delta_p90_ms']:.0f} ms." if timing["count"] else "Sin cambios disponibles.", "",
    ])
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--golden", type=Path, default=Path("eval/golden"))
    parser.add_argument("--output", type=Path, default=Path("eval/runs/prod_raw/autopsy"))
    parser.add_argument("--include-estimated", action="store_true")
    args = parser.parse_args()
    qualities = {"exact", "reconstructed"}
    if args.include_estimated:
        qualities.add("estimated")
    report = analyze(args.golden.resolve(), args.output.resolve(), qualities)
    print(render_markdown(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
