#!/usr/bin/env python3
"""Budgeted heavy transcription replay for songs routed as difficult.

Generation never reads approved text. Whisper TTA variants are one hypothesis
family; an optional Gemini candidate is an independent family and can verify,
but never judge, its own output. Live songs remain Tier 2 regardless of score.
"""

from __future__ import annotations

import argparse
import csv
import json
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Sequence

import numpy as np

from eval.bootstrap import song_bootstrap_ci
from eval.canonical import read_json, segments_to_lines, write_json
from eval.code_switching import transcribe_song
from eval.metrics import error_rate_counts, main_text, normalize_text
from eval.mss_alt import rms_vad_boundaries


VARIANTS = ("mss_original", "mss_slow_20pct", "mss_pitch_minus_3")


def _offset_segments(
    segments: Sequence[dict[str, Any]], offset: float, time_scale: float = 1.0,
) -> list[dict[str, Any]]:
    output = []
    for segment in segments:
        copied = dict(segment)
        copied["start"] = offset + float(segment.get("start") or 0.0) * time_scale
        copied["end"] = offset + float(segment.get("end") or 0.0) * time_scale
        copied["words"] = [
            {
                **word,
                "start": offset + float(word["start"]) * time_scale if word.get("start") is not None else None,
                "end": offset + float(word["end"]) * time_scale if word.get("end") is not None else None,
            }
            for word in (segment.get("words") or [])
        ]
        output.append(copied)
    return output


def _text(segments: Sequence[dict[str, Any]]) -> str:
    return " ".join(str(row.get("text") or "") for row in segments).strip()


def _similarity(left: Sequence[dict[str, Any]], right: Sequence[dict[str, Any]]) -> float:
    return SequenceMatcher(None, normalize_text(_text(left)), normalize_text(_text(right)), autojunk=False).ratio()


def whisper_family_medoid(families: dict[str, list[dict[str, Any]]]) -> tuple[str, dict[str, float]]:
    names = sorted(families)
    if not names:
        raise ValueError("at least one Whisper hypothesis is required")
    scores = {
        name: sum(_similarity(families[name], families[other]) for other in names if other != name)
        / max(1, len(names) - 1)
        for name in names
    }
    winner = max(names, key=lambda name: (scores[name], len(normalize_text(_text(families[name])))))
    return winner, scores


def independent_family_agreement(
    whisper_segments: Sequence[dict[str, Any]], independent_segments: Sequence[dict[str, Any]],
    threshold: float = 0.82,
) -> dict[str, Any]:
    similarity = _similarity(whisper_segments, independent_segments)
    return {"similarity": similarity, "verified": similarity >= threshold, "threshold": threshold}


def _transcribe_chunk(model, chunk: np.ndarray, language: str | None) -> list[dict[str, Any]]:
    result = model.transcribe(
        chunk, language=language, word_timestamps=True, verbose=None,
        condition_on_previous_text=False, beam_size=3, temperature=0.0,
    )
    return result.get("segments") or []


def _tta_segments(model, audio: np.ndarray, boundaries: Sequence[tuple[float, float]], language: str | None, variant: str) -> list[dict[str, Any]]:
    import librosa

    output = []
    for left, right in boundaries:
        chunk = audio[int(left * 16000):int(right * 16000)]
        time_scale = 1.0
        if variant == "mss_slow_20pct":
            chunk = librosa.effects.time_stretch(chunk, rate=0.8)
            time_scale = 0.8
        elif variant == "mss_pitch_minus_3":
            chunk = librosa.effects.pitch_shift(chunk, sr=16000, n_steps=-3)
        elif variant != "mss_original":
            raise ValueError(f"unknown TTA variant: {variant}")
        output.extend(_offset_segments(_transcribe_chunk(model, chunk, language), left, time_scale))
    return output


def _read_routes(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def generate(
    golden: Path, routes_path: Path, stems: Path, lid_root: Path, output: Path,
    model_name: str, independent_root: Path | None, code_switch_gate_path: Path,
    limit: int | None, song_ids: set[str] | None,
) -> dict[str, Any]:
    import whisper

    manifest = {row["song_id"]: row for row in read_json(golden / "manifest.json")["cases"]}
    route_rows = [row for row in _read_routes(routes_path) if int(row["route_heavy"])]
    if song_ids:
        route_rows = [row for row in route_rows if row["song_id"] in song_ids]
    if limit is not None:
        route_rows = route_rows[:limit]
    model = whisper.load_model(model_name)
    code_switch_report = read_json(code_switch_gate_path) if code_switch_gate_path.is_file() else {}
    code_switch_enabled = (code_switch_report.get("gate") or {}).get("status") == "GO"
    completed, failures = [], []
    for position, route in enumerate(route_rows, 1):
        song_id = route["song_id"]
        item = manifest[song_id]
        case = golden / item["path"]
        meta = read_json(case / "meta.json")
        audio_path = case / meta["audio"]["filename"]
        stem_path = stems / song_id / "vocals.wav"
        if not stem_path.is_file():
            failures.append({"song_id": song_id, "reason": "missing_full_mdx_extra_stem"})
            continue
        destination = output / "cases" / f"{song_id}.json"
        if destination.is_file():
            completed.append({"song_id": song_id, "cached": True})
            continue
        print(f"heavy {position}/{len(route_rows)} {song_id}", flush=True)
        audio = whisper.load_audio(str(audio_path))
        boundaries = rms_vad_boundaries(stem_path)
        language = (meta.get("language") or {}).get("value") or None
        whisper_families = {
            variant: _tta_segments(model, audio, boundaries, language, variant)
            for variant in VARIANTS
        }
        lid_path = lid_root / "cases" / f"{song_id}.json"
        if lid_path.is_file():
            lid = read_json(lid_path)
            if lid.get("is_es_en_code_switch") and code_switch_enabled:
                whisper_families["code_switch_per_chunk"] = transcribe_song(
                    model, audio_path, lid, mode="per_chunk_language",
                )
        winner, medoid_scores = whisper_family_medoid(whisper_families)
        independent_path = independent_root / song_id / "hypothesis.json" if independent_root else None
        independent = None
        if independent_path and independent_path.is_file():
            payload = read_json(independent_path)
            independent = payload.get("segments") or payload.get("lines") or payload
        agreement = independent_family_agreement(whisper_families[winner], independent) if independent else None
        raw_pipeline_path = case / "raw_pipeline_output.json"
        if not raw_pipeline_path.is_file():
            failures.append({"song_id": song_id, "reason": "missing_pre_human_baseline_for_safe_fallback"})
            continue
        baseline_segments = read_json(raw_pipeline_path).get("segments") or []
        independently_verified = bool(agreement and agreement["verified"])
        selected_segments = whisper_families[winner] if independently_verified else baseline_segments
        selected_source = winner if independently_verified else "production_baseline_fallback"
        write_json(destination, {
            "schema_version": 1,
            "song_id": song_id,
            "pipeline": "mss_alt_plus_whisper_tta_plus_optional_independent_consensus",
            "model": model_name,
            "gpu_passes": len(whisper_families) + int(independent is not None),
            "gpu_budget_passes": 5,
            "code_switch_decoder_enabled": code_switch_enabled,
            "approved_text_visible_to_generation": False,
            "live_tier2_required": bool(int(route["is_live"])),
            "families": {name: {"segments": segments} for name, segments in whisper_families.items()},
            "whisper_family_medoid": winner,
            "whisper_family_medoid_scores": medoid_scores,
            "independent_family": {"name": "gemini_audio", "segments": independent} if independent else None,
            "independent_consensus": agreement,
            "selected": {
                "segments": selected_segments,
                "source": selected_source,
                "verified_by_independent_family": independently_verified,
                "fallback_preserves_current_quality": not independently_verified,
            },
        })
        completed.append({"song_id": song_id, "cached": False})
    report = {
        "schema_version": 1, "model": model_name, "routed_songs": len(route_rows),
        "completed_songs": len(completed), "failures": failures,
        "independent_family_available": independent_root is not None,
        "code_switch_decoder_enabled": code_switch_enabled,
        "code_switch_gate": (code_switch_report.get("gate") or {}).get("status", "MISSING"),
        "safety": "Whisper TTA variants are one family; Gemini may verify but is never its own judge",
    }
    write_json(output / "generation_report.json", report)
    return report


def _score(reference_lines: Sequence[dict[str, Any]], segments: Sequence[dict[str, Any]]) -> dict[str, Any]:
    reference = main_text(reference_lines)
    counts = error_rate_counts(reference, main_text(segments_to_lines(segments)))
    return {**counts, "wer": counts["word_edits"] / max(1, counts["reference_words"])}


def score(golden: Path, cohort_path: Path, candidates: Path, output: Path) -> dict[str, Any]:
    cohort = read_json(cohort_path)
    rows, missing = [], []
    for label in cohort["cases"]:
        if not label.get("difficult_gold"):
            continue
        song_id = label["song_id"]
        payload = candidates / "cases" / f"{song_id}.json"
        if not payload.is_file():
            missing.append(song_id)
            continue
        case = golden / song_id
        approved = read_json(case / "lines.json")
        candidate = read_json(payload)["selected"]["segments"]
        baseline = {
            "word_edits": int(label["word_errors"]),
            "reference_words": int(label["reference_words"]),
            "wer": float(label["wer_main"]),
        }
        rows.append({
            "song_id": song_id, "is_live": bool(label["is_live"]),
            "baseline": baseline, "heavy": _score(approved, candidate),
            "independent_verified": bool(read_json(payload)["selected"]["verified_by_independent_family"]),
        })

    def corpus_wer(sample: Sequence[dict[str, Any]], key: str) -> float:
        edits = sum(row[key]["word_edits"] for row in sample)
        words = sum(row[key]["reference_words"] for row in sample)
        return edits / max(1, words)

    def relative(sample: Sequence[dict[str, Any]]) -> float:
        before, after = corpus_wer(sample, "baseline"), corpus_wer(sample, "heavy")
        return (before - after) / max(1e-9, before)

    ci = song_bootstrap_ci(rows, relative) if rows else None
    before = corpus_wer(rows, "baseline") if rows else None
    after = corpus_wer(rows, "heavy") if rows else None
    complete = len(rows) == sum(bool(row.get("difficult_gold")) for row in cohort["cases"])
    passed = bool(complete and ci and ci["estimate"] >= .25 and ci["low"] > 0)
    projected_minutes = 25.0 * after / max(1e-9, before) if rows else None
    report = {
        "schema_version": 1,
        "difficult_queue_wer": {"before": before, "after": after, "relative_improvement": ci},
        "difficult_minutes_projection": {
            "before_operation_midpoint": 25.0, "after_wer_ratio_model": projected_minutes,
            "warning": "projection only; editor timer remains the product metric",
        },
        "songs_scored": len(rows), "missing_difficult_song_ids": missing,
        "independent_verified_songs": sum(row["independent_verified"] for row in rows),
        "live_policy": "Tier 2 human always",
        "gate": {
            "requirements": "all difficult songs; relative WER >=25%; song-bootstrap CI95 low >0",
            "status": "GO_STAGING_SUGGESTIONS" if passed else "BLOCKED_INCOMPLETE_COHORT" if not complete else "NO_GO",
        },
        "cases": rows,
    }
    write_json(output, report)
    return report


def enforce_safe_fallback(golden: Path, candidates: Path) -> dict[str, Any]:
    updated, already_verified = [], []
    for path in sorted((candidates / "cases").glob("*.json")):
        payload = read_json(path)
        song_id = str(payload["song_id"])
        if payload.get("selected", {}).get("verified_by_independent_family"):
            already_verified.append(song_id)
            continue
        baseline_path = golden / song_id / "raw_pipeline_output.json"
        if not baseline_path.is_file():
            raise RuntimeError(f"missing pre-human baseline for {song_id}")
        payload["selected"] = {
            "segments": read_json(baseline_path).get("segments") or [],
            "source": "production_baseline_fallback",
            "verified_by_independent_family": False,
            "fallback_preserves_current_quality": True,
        }
        write_json(path, payload)
        updated.append(song_id)
    return {"updated_to_safe_fallback": updated, "independently_verified_unchanged": already_verified}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    run = sub.add_parser("run")
    run.add_argument("--golden", type=Path, default=Path("eval/golden"))
    run.add_argument("--routes", type=Path, default=Path("eval/runs/difficulty_router/routes.csv"))
    run.add_argument("--stems", type=Path, default=Path("eval/cache/full_stems"))
    run.add_argument("--lid-root", type=Path, default=Path("eval/runs/code_switch_lid_full"))
    run.add_argument("--output", type=Path, default=Path("eval/runs/difficult_pipeline"))
    run.add_argument("--model", default="large-v3-turbo")
    run.add_argument("--independent-root", type=Path)
    run.add_argument("--code-switch-gate", type=Path, default=Path("eval/runs/code_switch_score/report.json"))
    run.add_argument("--limit", type=int)
    run.add_argument("--song-id", action="append", default=[])
    measure = sub.add_parser("score")
    measure.add_argument("--golden", type=Path, default=Path("eval/golden"))
    measure.add_argument("--cohort", type=Path, default=Path("eval/runs/difficult_cohort/report.json"))
    measure.add_argument("--candidates", type=Path, default=Path("eval/runs/difficult_pipeline"))
    measure.add_argument("--output", type=Path, default=Path("eval/runs/difficult_pipeline/score.json"))
    finalize = sub.add_parser("finalize-fallback")
    finalize.add_argument("--golden", type=Path, default=Path("eval/golden"))
    finalize.add_argument("--candidates", type=Path, default=Path("eval/runs/difficult_pipeline"))
    args = parser.parse_args()
    if args.action == "run":
        result = generate(
            args.golden.resolve(), args.routes.resolve(), args.stems.resolve(), args.lid_root.resolve(),
            args.output.resolve(), args.model, args.independent_root.resolve() if args.independent_root else None,
            args.code_switch_gate.resolve(), args.limit, set(args.song_id) or None,
        )
    elif args.action == "score":
        result = score(args.golden.resolve(), args.cohort.resolve(), args.candidates.resolve(), args.output.resolve())
    else:
        result = enforce_safe_fallback(args.golden.resolve(), args.candidates.resolve())
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
