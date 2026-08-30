#!/usr/bin/env python3
"""Chunk-level es/en LID and code-switch transcription replay.

Language detection uses a small Whisper model on the vocal stem.  Only a song
that exhibits both languages triggers the expensive forced-language
log-probability confirmation.  Decoding candidates never see approved text.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import zlib
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Sequence

import numpy as np
from lingua import Language, LanguageDetectorBuilder

from eval.bootstrap import song_bootstrap_ci
from eval.canonical import read_json, segments_to_lines, write_json
from eval.metrics import error_rate_counts, main_text, normalize_text


_TEXT_LID = LanguageDetectorBuilder.from_languages(Language.SPANISH, Language.ENGLISH).build()


def _lexical_language(text: str) -> tuple[str | None, float]:
    lexical = " ".join(normalize_text(text).split())
    if len(lexical) < 6:
        return None, 0.0
    tokens = lexical.split()
    nonlexical = {"ah", "ay", "eh", "hey", "oh", "oo", "uh", "uhh", "uo", "uoh", "woah", "yeah"}
    if tokens and all(token in nonlexical or len(set(token)) <= 2 for token in tokens):
        return None, 0.0
    values = _TEXT_LID.compute_language_confidence_values(lexical)
    best = values[0] if values else None
    if not best:
        return None, 0.0
    language = "es" if best.language == Language.SPANISH else "en"
    return language, float(best.value)


def _usable_lid_text(text: str) -> tuple[bool, int]:
    tokens = normalize_text(text).split()
    if len(tokens) < 3:
        return False, len(tokens)
    counts = {token: tokens.count(token) for token in set(tokens)}
    dominant = max(counts.values()) / len(tokens)
    encoded = " ".join(tokens).encode("utf-8")
    compression_ratio = len(encoded) / max(1, len(zlib.compress(encoded)))
    boilerplate = " ".join(tokens) in {"thank you", "thanks for watching", "subtitles by"}
    repetitive = len(tokens) >= 10 and (dominant >= .45 or compression_ratio >= 2.4)
    return not boilerplate and not repetitive, len(tokens)


def lid_chunks(stem: Path, *, chunk_s: float = 8.0, overlap_s: float = 0.75) -> list[tuple[float, float]]:
    import librosa

    audio, sample_rate = librosa.load(str(stem), sr=16000, mono=True)
    if not len(audio):
        return []
    frame, hop = 1024, 320
    rms = librosa.feature.rms(y=audio, frame_length=frame, hop_length=hop)[0]
    threshold = max(1e-5, float(np.percentile(rms, 80)) * 0.12)
    output, step = [], max(1.0, chunk_s - overlap_s)
    cursor, duration = 0.0, len(audio) / sample_rate
    while cursor < duration:
        right = min(duration, cursor + chunk_s)
        lo, hi = int(cursor * sample_rate / hop), min(len(rms), int(math.ceil(right * sample_rate / hop)))
        if hi > lo and float(np.mean(rms[lo:hi] >= threshold)) >= 0.20:
            output.append((cursor, right))
        if right >= duration:
            break
        cursor += step
    return output


def _decode_candidate(model, mel, language: str) -> dict[str, Any]:
    import whisper

    result = model.decode(mel, whisper.DecodingOptions(
        language=language, without_timestamps=True, fp16=False,
        temperature=0.0, beam_size=1,
    ))
    lexical_language, lexical_confidence = _lexical_language(str(result.text or ""))
    return {
        "avg_logprob": float(result.avg_logprob),
        "text": str(result.text or "").strip(),
        "lexical_language": lexical_language,
        "lexical_confidence": lexical_confidence,
    }


def _language_persistence(rows: Sequence[dict[str, Any]]) -> dict[str, Any]:
    durations = {
        language: sum(
            float(row["end_s"]) - float(row["start_s"])
            for row in rows if row.get("confirmed_language") == language
        )
        for language in ("es", "en")
    }
    counts = {
        language: sum(row.get("confirmed_language") == language for row in rows)
        for language in ("es", "en")
    }
    lexical_words = {
        language: sum(int(row.get("lid_lexical_words") or 0) for row in rows if row.get("confirmed_language") == language)
        for language in ("es", "en")
    }
    max_runs = {"es": 0, "en": 0}
    for language in ("es", "en"):
        run = 0
        for row in rows:
            if row.get("confirmed_language") == language:
                run += 1
                max_runs[language] = max(max_runs[language], run)
            else:
                run = 0
    persistent = {
        language for language in ("es", "en")
        if durations[language] >= 10.0 and lexical_words[language] >= 12 and counts[language] >= 2
    }
    return {
        "durations": durations, "counts": counts, "lexical_words": lexical_words,
        "max_runs": max_runs, "persistent": persistent,
    }


def detect_song_languages(model, stem: Path) -> dict[str, Any]:
    import whisper

    audio = whisper.load_audio(str(stem))
    rows = []
    for start, end in lid_chunks(stem):
        chunk = whisper.pad_or_trim(audio[int(start * 16000):int(end * 16000)])
        mel = whisper.log_mel_spectrogram(chunk, n_mels=model.dims.n_mels).to(model.device)
        _tokens, probabilities = model.detect_language(mel)
        es, en = float(probabilities.get("es", 0.0)), float(probabilities.get("en", 0.0))
        winner = "es" if es >= en else "en"
        rows.append({
            "start_s": start, "end_s": end, "p_es": es, "p_en": en,
            "detected_language": winner, "detected_confidence": max(es, en),
            "confirmed_language": None, "confirmation": "pending_switch_test",
        })
    strong = {row["detected_language"] for row in rows if row["detected_confidence"] >= 0.60}
    potential_mixed = strong == {"es", "en"}
    if potential_mixed:
        for row in rows:
            chunk = whisper.pad_or_trim(audio[int(row["start_s"] * 16000):int(row["end_s"] * 16000)])
            mel = whisper.log_mel_spectrogram(chunk, n_mels=model.dims.n_mels).to(model.device)
            decoded = {language: _decode_candidate(model, mel, language) for language in ("es", "en")}
            logprob = {language: decoded[language]["avg_logprob"] for language in ("es", "en")}
            winner = max(logprob, key=logprob.get)
            margin = logprob[winner] - logprob["en" if winner == "es" else "es"]
            lexical_agreement = (
                decoded[winner]["lexical_language"] == winner
                and decoded[winner]["lexical_confidence"] >= 0.65
            )
            usable_text, lexical_words = _usable_lid_text(decoded[winner]["text"])
            row.update({
                "forced_logprob_es": logprob["es"], "forced_logprob_en": logprob["en"],
                "forced_text_es": decoded["es"]["text"], "forced_text_en": decoded["en"]["text"],
                "lexical_language": decoded[winner]["lexical_language"],
                "lexical_confidence": decoded[winner]["lexical_confidence"],
                "lid_lexical_words": lexical_words,
                "perplexity_margin": margin,
                "confirmed_language": winner if row["detected_confidence"] >= 0.55 and margin >= 0.03 and lexical_agreement and usable_text else None,
                "confirmation": "detect_logprob_and_lexical" if margin >= 0.03 and lexical_agreement and usable_text else "ambiguous_or_nonlexical",
            })
    else:
        for row in rows:
            row["confirmed_language"] = row["detected_language"] if row["detected_confidence"] >= 0.65 else None
            row["confirmation"] = "single_language_fast_path"
    persistence = _language_persistence(rows)
    durations, counts = persistence["durations"], persistence["counts"]
    max_runs, persistent = persistence["max_runs"], persistence["persistent"]
    confirmed = {row["confirmed_language"] for row in rows if row["confirmed_language"]}
    return {
        "chunks": rows,
        "potential_mixed": potential_mixed,
        "confirmed_languages": sorted(confirmed),
        "confirmed_chunk_counts": counts,
        "confirmed_duration_s": durations,
        "max_consecutive_confirmed_chunks": max_runs,
        "persistent_languages": sorted(persistent),
        "is_es_en_code_switch": persistent == {"es", "en"},
        "ambiguous_chunk_fraction": sum(row["confirmed_language"] is None for row in rows) / max(1, len(rows)),
    }


def _finalize_lid_case(result: dict[str, Any]) -> dict[str, Any]:
    """Recompute policy fields so cached acoustic evidence is safely reusable."""
    for row in result.get("chunks") or []:
        language = row.get("confirmed_language")
        forced_key = f"forced_text_{language}"
        if language not in {"es", "en"} or forced_key not in row:
            row.setdefault("lid_lexical_words", 0)
            continue
        text = str(row.get(forced_key) or "")
        usable, words = _usable_lid_text(text)
        row["lid_lexical_words"] = words
        if language and not usable:
            row["confirmed_language"] = None
            row["confirmation"] = "rejected_boilerplate_or_repetition"
    persistence = _language_persistence(result.get("chunks") or [])
    persistent = persistence["persistent"]
    input_source = result.get("input_source") or "unknown"
    raw_mixed = persistent == {"es", "en"}
    result.update({
        "confirmed_chunk_counts": persistence["counts"],
        "confirmed_duration_s": persistence["durations"],
        "confirmed_lexical_words": persistence["lexical_words"],
        "max_consecutive_confirmed_chunks": persistence["max_runs"],
        "persistent_languages": sorted(persistent),
        "mix_code_switch_candidate": bool(raw_mixed and input_source != "full_vocal_stem"),
        "is_es_en_code_switch": bool(raw_mixed and input_source == "full_vocal_stem"),
    })
    return result


def contextual_transcript_confirmation(model, audio_path: Path) -> dict[str, Any]:
    """Confirm a mixed candidate with an unforced, full-context transcript."""
    result = model.transcribe(
        str(audio_path), language=None, word_timestamps=False, verbose=None,
        condition_on_previous_text=True, beam_size=1, temperature=0.0,
    )
    rows, line_counts, characters = [], {"es": 0, "en": 0}, {"es": 0, "en": 0}
    for segment in result.get("segments") or []:
        text = str(segment.get("text") or "").strip()
        usable, _words = _usable_lid_text(text)
        language, confidence = _lexical_language(text) if usable else (None, 0.0)
        if language in {"es", "en"} and confidence >= .65:
            line_counts[language] += 1
            characters[language] += len(normalize_text(text).replace(" ", ""))
        rows.append({
            "start_s": float(segment.get("start") or 0), "end_s": float(segment.get("end") or 0),
            "text": text, "language": language, "confidence": confidence,
        })
    mixed = all(line_counts[language] >= 2 and characters[language] >= 20 for language in ("es", "en"))
    return {
        "model_role": "same_lid_model_unforced_full_context_confirmation",
        "line_counts": line_counts, "lexical_characters": characters,
        "is_es_en_code_switch": mixed, "segments": rows,
    }


def run_lid(
    golden: Path, stems: Path, output: Path, model_name: str, qualities: set[str],
    *, fallback_to_mix: bool = False,
) -> dict[str, Any]:
    import whisper

    model = whisper.load_model(model_name)
    rows, failures = [], []
    eligible = [
        item for item in read_json(golden / "manifest.json")["cases"]
        if item["raw_quality"] in qualities
    ]
    for position, item in enumerate(eligible, 1):
        case = golden / item["path"]
        stem = stems / item["song_id"] / "vocals.wav"
        input_path, input_source = stem, "full_vocal_stem"
        if not input_path.is_file() and fallback_to_mix:
            meta = read_json(case / "meta.json")
            input_path, input_source = case / meta["audio"]["filename"], "original_mix_fallback"
        if not input_path.is_file():
            failures.append({"song_id": item["song_id"], "reason": "missing_lid_stem"})
            continue
        destination = output / "cases" / f"{item['song_id']}.json"
        if destination.is_file():
            result = read_json(destination)
        else:
            print(f"code-switch LID {position}/{len(eligible)} {item['song_id']}", file=sys.stderr, flush=True)
            result = {
                "song_id": item["song_id"], "input_source": input_source,
                **detect_song_languages(model, input_path),
            }
        result = _finalize_lid_case(result)
        acoustic_candidate = bool(result.get("is_es_en_code_switch"))
        if acoustic_candidate and result.get("input_source") == "full_vocal_stem":
            if not result.get("context_transcript_confirmation"):
                result["context_transcript_confirmation"] = contextual_transcript_confirmation(model, input_path)
            result["acoustic_code_switch_candidate"] = True
            result["is_es_en_code_switch"] = bool(
                result["context_transcript_confirmation"].get("is_es_en_code_switch")
            )
        else:
            result["acoustic_code_switch_candidate"] = acoustic_candidate
        write_json(destination, result)
        rows.append(result)
    report = {
        "schema_version": 1,
        "model": model_name,
        "input": "full_vocal_stem_with_explicit_mix_fallback" if fallback_to_mix else "full_vocal_stem",
        "songs": len(rows),
        "mixed_song_ids": [row["song_id"] for row in rows if row["is_es_en_code_switch"]],
        "failures": failures,
        "cases": rows,
    }
    write_json(output / "report.json", report)
    return report


def _owned_segments(chunk_results: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    candidates = []
    for index, chunk in enumerate(chunk_results):
        left = float(chunk["start_s"])
        for segment in chunk.get("segments") or []:
            copied = dict(segment)
            copied["start"] = float(segment.get("start") or 0.0) + left
            copied["end"] = float(segment.get("end") or 0.0) + left
            copied["_chunk_idx"] = index
            candidates.append(copied)
    candidates.sort(key=lambda row: (float(row["start"]), float(row["end"])))
    output = []
    for row in candidates:
        duplicate_idx = None
        row_center = (float(row["start"]) + float(row["end"])) / 2.0
        for index in range(max(0, len(output) - 3), len(output)):
            prior = output[index]
            prior_center = (float(prior["start"]) + float(prior["end"])) / 2.0
            similarity = SequenceMatcher(
                None, normalize_text(str(prior.get("text") or "")),
                normalize_text(str(row.get("text") or "")), autojunk=False,
            ).ratio()
            if abs(row_center - prior_center) <= 1.5 and similarity >= 0.80:
                duplicate_idx = index
                break
        if duplicate_idx is None:
            output.append(row)
        else:
            prior = output[duplicate_idx]
            prior_duration = float(prior["end"]) - float(prior["start"])
            row_duration = float(row["end"]) - float(row["start"])
            if row_duration > prior_duration:
                output[duplicate_idx] = row
    for row in output:
        row.pop("_chunk_idx", None)
    return output


def decode_windows(
    lid: dict[str, Any], *, window_s: float = 24.0, overlap_s: float = 2.0,
) -> list[dict[str, Any]]:
    """Turn fine LID evidence into longer ASR windows without forcing ambiguity."""
    chunks = lid.get("chunks") or []
    if not chunks:
        return []
    duration = max(float(row["end_s"]) for row in chunks)
    windows, cursor, step = [], 0.0, max(4.0, window_s - overlap_s)
    while cursor < duration:
        right = min(duration, cursor + window_s)
        votes = {"es": 0.0, "en": 0.0}
        for row in chunks:
            language = row.get("confirmed_language")
            if language not in votes:
                continue
            overlap = max(0.0, min(right, float(row["end_s"])) - max(cursor, float(row["start_s"])))
            votes[language] += overlap
        total = sum(votes.values())
        winner = max(votes, key=votes.get)
        language = winner if total >= 8.0 and votes[winner] / max(1e-9, total) >= .75 else None
        windows.append({
            "start_s": cursor, "end_s": right, "confirmed_language": language,
            "lid_vote_seconds": votes, "lid_winner_share": votes[winner] / max(1e-9, total),
        })
        if right >= duration:
            break
        cursor += step
    return windows


def transcribe_song(
    decode_model, audio_path: Path, lid: dict[str, Any], *, mode: str,
) -> list[dict[str, Any]]:
    import whisper

    audio = whisper.load_audio(str(audio_path))
    if mode == "bilingual_full_song":
        result = decode_model.transcribe(
            audio, language=None,
            initial_prompt="Spanish and English song lyrics; preserve code-switching; do not translate.",
            word_timestamps=True, condition_on_previous_text=True,
            beam_size=3, temperature=0.0, verbose=None,
        )
        return result.get("segments") or []
    chunks = []
    for row in decode_windows(lid):
        start, end = float(row["start_s"]), float(row["end_s"])
        chunk = audio[int(start * 16000):int(end * 16000)]
        if mode == "per_chunk_language":
            language = row.get("confirmed_language")
            prompt = None if language else "Spanish and English song lyrics; preserve code-switching; do not translate."
        elif mode == "bilingual_auto_prompt":
            language = None
            prompt = "Spanish and English song lyrics; español, English, oh, yeah."
        else:
            raise ValueError(f"unknown code-switch mode: {mode}")
        result = decode_model.transcribe(
            chunk, language=language, initial_prompt=prompt,
            word_timestamps=True, condition_on_previous_text=False,
            beam_size=3, temperature=0.0, verbose=None,
        )
        chunks.append({**row, "segments": result.get("segments") or []})
    return _owned_segments(chunks)


def run_decode(
    golden: Path, lid_root: Path, output: Path, model_name: str,
    modes: Sequence[str], song_ids: set[str] | None,
) -> dict[str, Any]:
    """Generate both code-switch alternatives without reading approved text."""
    import whisper

    model = whisper.load_model(model_name)
    rows, failures = [], []
    manifest = read_json(golden / "manifest.json")
    for item in manifest["cases"]:
        song_id = item["song_id"]
        if song_ids and song_id not in song_ids:
            continue
        lid_path = lid_root / "cases" / f"{song_id}.json"
        if not lid_path.is_file():
            failures.append({"song_id": song_id, "reason": "missing_full_song_lid"})
            continue
        lid = read_json(lid_path)
        if not lid.get("is_es_en_code_switch"):
            continue
        case = golden / item["path"]
        meta = read_json(case / "meta.json")
        audio_path = case / meta["audio"]["filename"]
        completed = []
        for mode in modes:
            destination = output / mode / song_id / "hypothesis.json"
            if not destination.is_file():
                segments = transcribe_song(model, audio_path, lid, mode=mode)
                write_json(destination, {
                    "schema_version": 1, "song_id": song_id, "mode": mode,
                    "model": model_name, "approved_text_visible_to_decoder": False,
                    "segments": segments,
                })
            completed.append(mode)
        rows.append({"song_id": song_id, "modes": completed})
    report = {"schema_version": 1, "model": model_name, "cases": rows, "failures": failures}
    write_json(output / "report.json", report)
    return report


def _wer(approved: Sequence[dict[str, Any]], segments: Sequence[dict[str, Any]]) -> dict[str, Any]:
    reference = main_text(segments_to_lines(approved))
    hypothesis = main_text(segments_to_lines(segments))
    counts = error_rate_counts(reference, hypothesis)
    return {**counts, "wer": counts["word_edits"] / max(1, counts["reference_words"])}


def evaluate(
    golden: Path, cohort_path: Path, candidate_root: Path, lid_path: Path, output: Path,
) -> dict[str, Any]:
    cohort = read_json(cohort_path)
    cases = {row["song_id"]: row for row in cohort["cases"]}
    rows = []
    for song_id, label in cases.items():
        if label["raw_quality"] not in {"exact", "reconstructed"}:
            continue
        case = golden / song_id
        approved = read_json(case / "approved.json")
        raw = read_json(case / "raw_pipeline_output.json")["segments"]
        payload = candidate_root / song_id / "hypothesis.json"
        if not payload.is_file():
            continue
        candidate = read_json(payload)
        segments = candidate.get("segments") or candidate.get("lines") or candidate
        rows.append({
            "song_id": song_id,
            "spanglish": bool(label["text_language_gold"]["is_es_en_code_switch"]),
            "baseline": _wer(approved, raw),
            "candidate": _wer(approved, segments),
        })

    def corpus_wer(sample: Sequence[dict[str, Any]], family: str) -> float:
        errors = sum(row[family]["word_edits"] for row in sample)
        words = sum(row[family]["reference_words"] for row in sample)
        return errors / max(1, words)

    spanglish = [row for row in rows if row["spanglish"]]
    mono = [row for row in rows if not row["spanglish"]]
    if spanglish:
        relative = song_bootstrap_ci(spanglish, lambda sample: (
            corpus_wer(sample, "baseline") - corpus_wer(sample, "candidate")
        ) / max(1e-9, corpus_wer(sample, "baseline")))
    else:
        relative = None
    mono_regressions = [
        {"song_id": row["song_id"], "absolute_wer_regression": row["candidate"]["wer"] - row["baseline"]["wer"]}
        for row in mono if row["candidate"]["wer"] > row["baseline"]["wer"] + 1e-12
    ]
    detected_mixed = set((read_json(lid_path) if lid_path.is_file() else {}).get("mixed_song_ids") or [])
    comparable_labels = [row for row in cases.values() if row["raw_quality"] in {"exact", "reconstructed"}]
    false_positive_routes = [
        row["song_id"] for row in comparable_labels
        if not row["text_language_gold"]["is_es_en_code_switch"] and row["song_id"] in detected_mixed
    ]
    missed_code_switch = [
        row["song_id"] for row in comparable_labels
        if row["text_language_gold"]["is_es_en_code_switch"] and row["song_id"] not in detected_mixed
    ]
    enough = len(spanglish) >= 3
    passed = bool(
        enough and relative and relative["estimate"] >= 0.30
        and not mono_regressions and not false_positive_routes and not missed_code_switch
    )
    report = {
        "schema_version": 1,
        "songs_scored": len(rows),
        "spanglish_songs": len(spanglish),
        "spanglish_relative_wer_improvement": relative,
        "monolingual_regressions": mono_regressions,
        "lid_against_human_text_labels": {
            "false_positive_code_switch_routes": false_positive_routes,
            "missed_code_switch_song_ids": missed_code_switch,
            "note": "gold language labels are scorer-only and never used by production LID",
        },
        "gate": {
            "requirements": "spanglish relative WER >=30%; >=3 song groups; zero mono regression or false code-switch route",
            "status": "GO" if passed else "BLOCKED_INSUFFICIENT_SPANGLISH_GOLD" if not enough else "NO_GO",
        },
        "cases": rows,
    }
    write_json(output, report)
    return report


def summarize(score_root: Path, output: Path) -> dict[str, Any]:
    rows = []
    for path in sorted(score_root.glob("*.json")):
        if path.name == output.name:
            continue
        report = read_json(path)
        cases = report.get("cases") or []
        if not cases:
            continue
        before_errors = sum(row["baseline"]["word_edits"] for row in cases if row["spanglish"])
        after_errors = sum(row["candidate"]["word_edits"] for row in cases if row["spanglish"])
        words = sum(row["baseline"]["reference_words"] for row in cases if row["spanglish"])
        rows.append({
            "mode": path.stem, "baseline_wer": before_errors / max(1, words),
            "candidate_wer": after_errors / max(1, words),
            "relative_improvement": (before_errors - after_errors) / max(1, before_errors),
            "gate": report["gate"]["status"], "spanglish_songs": report["spanglish_songs"],
            "false_positive_code_switch_routes": report["lid_against_human_text_labels"]["false_positive_code_switch_routes"],
        })
    best = min(rows, key=lambda row: row["candidate_wer"]) if rows else None
    enough = bool(rows and min(row["spanglish_songs"] for row in rows) >= 3)
    any_go = any(row["gate"] == "GO" for row in rows)
    any_improvement = any(row["relative_improvement"] > 0 for row in rows)
    status = (
        "GO" if any_go else "NO_GO" if enough
        else "BLOCKED_INSUFFICIENT_SPANGLISH_GOLD" if any_improvement
        else "NO_GO_PILOT_AND_INSUFFICIENT_GOLD"
    )
    report = {
        "schema_version": 1, "variants": rows, "best_observed": best,
        "lid_false_positive_routes": sorted({song_id for row in rows for song_id in row["false_positive_code_switch_routes"]}),
        "gate": {
            "status": status,
            "note": "negative pilot is sufficient to reject current variants; >=3 songs still required for a positive product claim",
        },
    }
    write_json(output, report)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    lid = sub.add_parser("lid")
    lid.add_argument("--golden", type=Path, default=Path("eval/golden"))
    lid.add_argument("--stems", type=Path, default=Path("eval/cache/language_id/stems"))
    lid.add_argument("--output", type=Path, default=Path("eval/runs/code_switch_lid"))
    lid.add_argument("--model", default="base")
    lid.add_argument("--quality", action="append", choices=["exact", "reconstructed", "estimated", "none"], default=[])
    lid.add_argument("--fallback-to-mix", action="store_true")
    score = sub.add_parser("score")
    score.add_argument("--golden", type=Path, default=Path("eval/golden"))
    score.add_argument("--cohort", type=Path, default=Path("eval/runs/difficult_cohort/report.json"))
    score.add_argument("--candidates", type=Path, required=True)
    score.add_argument("--lid", type=Path, default=Path("eval/runs/code_switch_lid_full/report.json"))
    score.add_argument("--output", type=Path, default=Path("eval/runs/code_switch_score/report.json"))
    decode = sub.add_parser("decode")
    decode.add_argument("--golden", type=Path, default=Path("eval/golden"))
    decode.add_argument("--lid-root", type=Path, default=Path("eval/runs/code_switch_lid"))
    decode.add_argument("--output", type=Path, default=Path("eval/runs/code_switch_decode"))
    decode.add_argument("--model", default="large-v3-turbo")
    decode.add_argument("--mode", action="append", choices=["per_chunk_language", "bilingual_auto_prompt", "bilingual_full_song"], default=[])
    decode.add_argument("--song-id", action="append", default=[])
    summary = sub.add_parser("summarize")
    summary.add_argument("--score-root", type=Path, default=Path("eval/runs/code_switch_score"))
    summary.add_argument("--output", type=Path, default=Path("eval/runs/code_switch_score/report.json"))
    args = parser.parse_args()
    if args.action == "lid":
        result = run_lid(
            args.golden.resolve(), args.stems.resolve(), args.output.resolve(), args.model,
            set(args.quality) or {"exact", "reconstructed", "estimated"},
            fallback_to_mix=args.fallback_to_mix,
        )
    elif args.action == "score":
        result = evaluate(
            args.golden.resolve(), args.cohort.resolve(), args.candidates.resolve(),
            args.lid.resolve(), args.output.resolve(),
        )
    elif args.action == "decode":
        result = run_decode(
            args.golden.resolve(), args.lid_root.resolve(), args.output.resolve(), args.model,
            args.mode or ["per_chunk_language", "bilingual_full_song"], set(args.song_id) or None,
        )
    else:
        result = summarize(args.score_root.resolve(), args.output.resolve())
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
