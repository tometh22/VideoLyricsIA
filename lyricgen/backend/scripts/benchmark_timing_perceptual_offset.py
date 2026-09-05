#!/usr/bin/env python3
"""Measure the hidden display-end offset against operator timing gold.

Read-only. The script compares the raw acoustic endpoint with the historical
``acoustic_end - 100ms`` proposal on the same eligible lines. It emits no lyric
text and never applies a proposal. Gold defaults to operator-locked lines in
the current durable EditorDocument; the machine baseline is the latest
transcription/migration EditorVersion preceding it.
"""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from statistics import mean, median
from typing import Any, Mapping, Sequence

import numpy as np

from database import EditorDocument, EditorVersion, Job, SessionLocal
from timing_review_suggestions import (
    AcousticTrack,
    TimingReviewPolicy,
    _symmetric_phrase_endpoint,
    build_timing_review_candidates,
)


def _number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def load_cached_track(path: Path) -> AcousticTrack:
    with np.load(path) as data:
        return AcousticTrack(
            frame_seconds=float(data["frame_seconds"]),
            times=np.asarray(data["times"]),
            rms=np.asarray(data["rms"]),
            active=np.asarray(data["active"], dtype=bool),
            f0=np.asarray(data["f0"]),
            voiced_probability=np.asarray(data["voiced_probability"]),
            pitched=np.asarray(data["pitched"], dtype=bool),
            energy_threshold=float(data["energy_threshold"]),
        )


def _metrics(errors: list[float], total_gold: int) -> dict[str, Any]:
    absolute = [abs(value) for value in errors]
    return {
        "gold_lines": total_gold,
        "acoustically_eligible": len(errors),
        "coverage_pct": round(100.0 * len(errors) / total_gold, 3)
        if total_gold else None,
        "abstained": total_gold - len(errors),
        "within_150ms": sum(value <= 0.15 for value in absolute),
        "within_150ms_pct_eligible": round(
            100.0 * sum(value <= 0.15 for value in absolute) / len(errors), 3,
        ) if errors else None,
        "mean_absolute_error_s": round(mean(absolute), 4) if absolute else None,
        "median_absolute_error_s": round(median(absolute), 4) if absolute else None,
        "median_signed_error_s": round(median(errors), 4) if errors else None,
    }


def evaluate_offsets(
    baseline: Sequence[Mapping[str, Any]],
    gold: Sequence[Mapping[str, Any]],
    track: AcousticTrack,
    *,
    offsets_s: Sequence[float] = (0.0, 0.1),
    next_line_guard_s: float = 0.02,
) -> dict[str, Any]:
    if len(baseline) != len(gold):
        raise ValueError("baseline/gold line counts differ; index matching is unsafe")
    gold_indices = [
        index for index, segment in enumerate(gold)
        if segment.get("locked") is True or segment.get("operator_locked") is True
    ]
    errors = {float(offset): [] for offset in offsets_s}
    eligible_rows: list[dict[str, Any]] = []
    abstentions: dict[str, int] = {}
    for index in gold_indices:
        source = baseline[index]
        target = gold[index]
        start = _number(source.get("start"))
        gold_end = _number(target.get("end"))
        if start is None or gold_end is None:
            reason = "invalid_timeline"
            abstentions[reason] = abstentions.get(reason, 0) + 1
            continue
        next_start = (
            _number(baseline[index + 1].get("start"))
            if index + 1 < len(baseline) else None
        )
        track_end = float(track.times[-1] + track.frame_seconds)
        limit = (
            min(track_end, next_start - next_line_guard_s)
            if next_start is not None else track_end
        )
        acoustic_end, evidence = _symmetric_phrase_endpoint(
            track, max(0.0, start - 0.20), limit,
        )
        if acoustic_end is None:
            reason = str(evidence.get("reason") or "no_acoustic_endpoint")
            abstentions[reason] = abstentions.get(reason, 0) + 1
            continue
        row_errors = {}
        for offset in offsets_s:
            candidate = min(limit, acoustic_end - float(offset))
            error = candidate - gold_end
            errors[float(offset)].append(error)
            row_errors[f"offset_{int(round(float(offset) * 1000))}ms"] = round(error, 4)
        eligible_rows.append({
            "line_index": index,
            "raw_acoustic_end": round(acoustic_end, 4),
            "gold_end": round(gold_end, 4),
            "errors_s": row_errors,
        })

    policies = {}
    for offset in offsets_s:
        candidates, report = build_timing_review_candidates(
            [dict(row) for row in baseline], track,
            policy=TimingReviewPolicy(perceptual_lead_s=float(offset)),
        )
        policies[f"offset_{int(round(float(offset) * 1000))}ms"] = {
            "proposal_count": len(candidates),
            "high_confidence_count": int(report.get("high_confidence_count") or 0),
            "automatic_apply_allowed": False,
        }

    return {
        "schema": "timing-perceptual-offset-benchmark-v1",
        "gold_definition": "current operator_locked lines; exact index against machine baseline",
        "gold_line_count": len(gold_indices),
        "offsets": {
            f"offset_{int(round(offset * 1000))}ms": _metrics(values, len(gold_indices))
            for offset, values in errors.items()
        },
        "proposal_population": policies,
        "abstention_reasons": abstentions,
        "eligible_rows": eligible_rows,
        "automatic_mutations": 0,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--job-id", required=True)
    parser.add_argument("--feature-cache", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    db = SessionLocal()
    try:
        job = db.query(Job).filter(Job.job_id == args.job_id).one()
        document = db.query(EditorDocument).filter(
            EditorDocument.job_id == args.job_id,
        ).one()
        baseline_version = (
            db.query(EditorVersion)
            .filter(
                EditorVersion.job_id == args.job_id,
                EditorVersion.reason.in_(("transcription", "migration")),
            )
            .order_by(EditorVersion.revision.desc())
            .first()
        )
        if baseline_version is None:
            raise RuntimeError("machine baseline EditorVersion missing")
        result = evaluate_offsets(
            list(baseline_version.segments or []),
            list(document.current_segments or []),
            load_cached_track(args.feature_cache),
        )
        result.update({
            "job_id": args.job_id,
            "audio_sha256": str(job.input_audio_sha256 or ""),
            "baseline_revision": int(baseline_version.revision),
            "gold_revision": int(document.revision),
            "database_mutations": 0,
            "paid_provider_calls": 0,
        })
        payload = json.dumps(result, indent=2, sort_keys=True) + "\n"
        if args.output:
            args.output.write_text(payload, encoding="utf-8")
        print(payload, end="")
        return 0
    finally:
        db.rollback()
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
