#!/usr/bin/env python3
"""Calibrate a song-difficulty score from LoRA/base disagreement.

The runtime score is computed from two model hypotheses only.  Gold WER is
used here solely to label the offline calibration cohort; it is never needed
by the production router.  This keeps the pilot auditable and prevents the
reference transcript from leaking into a routing decision.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import statistics
import sys
from pathlib import Path
from typing import Any, Iterable

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


TOKEN_RE = re.compile(r"[\wÀ-ÿ]+(?:['’][\wÀ-ÿ]+)?", re.UNICODE)


def tokens(text: Any) -> list[str]:
    """Normalize a hypothesis for a punctuation/case-insensitive comparison."""
    return [token.casefold() for token in TOKEN_RE.findall(str(text or ""))]


def edit_distance(left: list[str], right: list[str]) -> int:
    previous = list(range(len(right) + 1))
    for index, value in enumerate(left, 1):
        current = [index]
        for other_index, other in enumerate(right, 1):
            current.append(min(
                current[-1] + 1,
                previous[other_index] + 1,
                previous[other_index - 1] + (value != other),
            ))
        previous = current
    return previous[-1]


def sample_disagreement(base_text: Any, lora_text: Any) -> tuple[int, int]:
    """Return edits and comparison length for one paired decoding window."""
    base = tokens(base_text)
    lora = tokens(lora_text)
    return edit_distance(base, lora), max(len(base), len(lora), 1)


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            value = json.loads(line)
            if not isinstance(value, dict):
                raise ValueError(f"JSONL row is not an object: {path}")
            rows.append(value)
    return rows


def _song_disagreement(base_path: Path, lora_path: Path) -> dict[str, dict[str, Any]]:
    base_rows = {row["sample_id"]: row for row in _read_jsonl(base_path)}
    lora_rows = {row["sample_id"]: row for row in _read_jsonl(lora_path)}
    songs: dict[str, dict[str, Any]] = {}
    for sample_id, base in base_rows.items():
        lora = lora_rows.get(sample_id)
        if lora is None:
            continue
        song_id = str(base["song_id"])
        song = songs.setdefault(song_id, {
            "song_id": song_id,
            "artist": base.get("artist"),
            "windows": 0,
            "edits": 0,
            "comparison_tokens": 0,
        })
        edits, comparison_tokens = sample_disagreement(
            base.get("hypothesis"), lora.get("hypothesis"),
        )
        song["windows"] += 1
        song["edits"] += edits
        song["comparison_tokens"] += comparison_tokens
    for song in songs.values():
        song["disagreement"] = round(
            song["edits"] / max(song["comparison_tokens"], 1), 6,
        )
    return songs


def auc(scores: Iterable[float], labels: Iterable[bool]) -> float | None:
    paired = list(zip(scores, labels))
    positives = [score for score, label in paired if label]
    negatives = [score for score, label in paired if not label]
    if not positives or not negatives:
        return None
    wins = 0.0
    for positive in positives:
        for negative in negatives:
            if positive > negative:
                wins += 1.0
            elif positive == negative:
                wins += 0.5
    return wins / (len(positives) * len(negatives))


def bootstrap_auc(
    rows: list[dict[str, Any]], *, iterations: int = 2000, seed: int = 17,
) -> dict[str, Any]:
    if len(rows) < 2:
        return {"estimate": None, "ci_low": None, "ci_high": None, "samples": 0}
    score = auc(
        (row["disagreement"] for row in rows),
        (row["difficult"] for row in rows),
    )
    rng = random.Random(seed)
    values: list[float] = []
    for _ in range(iterations):
        sample = [rows[rng.randrange(len(rows))] for _ in rows]
        value = auc(
            (row["disagreement"] for row in sample),
            (row["difficult"] for row in sample),
        )
        if value is not None:
            values.append(value)
    values.sort()
    if not values:
        return {"estimate": score, "ci_low": None, "ci_high": None, "samples": 0}
    low_index = min(len(values) - 1, int(0.025 * len(values)))
    high_index = min(len(values) - 1, int(0.975 * len(values)))
    return {
        "estimate": score,
        "ci_low": values[low_index],
        "ci_high": values[high_index],
        "samples": len(values),
        "iterations": iterations,
    }


def build_report(
    cohorts: Iterable[tuple[str, Path, Path, Path]], *,
    difficult_wer_threshold: float = 0.10,
    bootstrap_iterations: int = 2000,
) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    for raw_quality, base_path, lora_path, evaluation_path in cohorts:
        disagreement = _song_disagreement(base_path, lora_path)
        evaluation = json.loads(evaluation_path.read_text(encoding="utf-8"))
        for song_id, metrics in (evaluation.get("by_song") or {}).items():
            song = disagreement.get(song_id)
            if song is None:
                continue
            baseline_wer = float(metrics.get("baseline_wer") or 0.0)
            rows.append({
                **song,
                "raw_quality": metrics.get("raw_quality") or raw_quality,
                "baseline_wer": baseline_wer,
                "difficult": baseline_wer > difficult_wer_threshold,
            })
    rows.sort(key=lambda row: (-row["disagreement"], row["song_id"]))
    scores = [row["disagreement"] for row in rows]
    wers = [row["baseline_wer"] for row in rows]
    correlation = None
    if len(rows) > 1 and len(set(scores)) > 1 and len(set(wers)) > 1:
        correlation = statistics.correlation(scores, wers)
    return {
        "schema": "lora-disagreement-router-pilot-v1",
        "metric_definition": (
            "weighted token edit distance between paired base and LoRA "
            "hypotheses, aggregated by song; punctuation and case ignored"
        ),
        "difficulty_definition": f"baseline WER > {difficult_wer_threshold:.3f}",
        "runtime_uses_gold": False,
        "songs": len(rows),
        "by_raw_quality": {
            quality: sum(row["raw_quality"] == quality for row in rows)
            for quality in sorted({row["raw_quality"] for row in rows})
        },
        "difficult_songs": sum(row["difficult"] for row in rows),
        "auc": bootstrap_auc(
            rows, iterations=bootstrap_iterations,
        ),
        "pearson_disagreement_wer": correlation,
        "songs_detail": rows,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--canonical-baseline", type=Path, required=True)
    parser.add_argument("--canonical-lora", type=Path, required=True)
    parser.add_argument("--canonical-evaluation", type=Path, required=True)
    parser.add_argument("--diagnostic-baseline", type=Path, required=True)
    parser.add_argument("--diagnostic-lora", type=Path, required=True)
    parser.add_argument("--diagnostic-evaluation", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--difficult-wer-threshold", type=float, default=0.10)
    args = parser.parse_args()
    report = build_report([
        (
            "exact", args.canonical_baseline, args.canonical_lora,
            args.canonical_evaluation,
        ),
        (
            "reconstructed", args.diagnostic_baseline, args.diagnostic_lora,
            args.diagnostic_evaluation,
        ),
    ], difficult_wer_threshold=args.difficult_wer_threshold)
    # El piloto original mezcló 13 canciones de entrenamiento del adaptador en la
    # cohorte "reconstructed"; el desacuerdo sobre audio memorizado no es la
    # señal de runtime. El registro de roles corta eso acá.
    from song_roles import filter_evaluable, role_split
    song_ids = [str(row.get("song_id")) for row in report.get("songs_detail") or []]
    _evaluable, train_songs = filter_evaluable(song_ids)
    if train_songs:
        raise SystemExit(
            "abortado: el piloto incluye canciones de entrenamiento: "
            + ", ".join(sorted(set(train_songs)))
        )
    report["cohort_role_split"] = role_split(song_ids)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps({
        "songs": report["songs"],
        "difficult_songs": report["difficult_songs"],
        "auc": report["auc"],
        "pearson_disagreement_wer": report["pearson_disagreement_wer"],
        "output": str(args.output),
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
