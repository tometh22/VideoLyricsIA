"""Replay forced alignment using approved text without approved timing.

The experiment answers one operational question: after a reviewer confirms
the words, can the system reconstruct the approved display boundaries well
enough that the reviewer no longer drags them?  Approved timestamps are loaded
only by the scorer, after an aligner has persisted its hypothesis.

Backends:
* ``current_xlsr``: the exact current Genly Spanish CTC implementation loaded
  from a pinned Git ref, with neutral uniformly spaced input boundaries.
* ``mms_fa``: TorchAudio's multilingual MMS forced-alignment bundle (research
  evaluation only; its model license is non-commercial).
* ``xlsr_ipa``: Meta's Apache-2.0 XLSR phoneme recognizer plus eSpeak G2P.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import math
import os
import re
import subprocess
import sys
import types
import unicodedata
from pathlib import Path
from typing import Any, Callable, Sequence

import numpy as np

from eval.bootstrap import percentile, song_bootstrap_ci
from eval.canonical import read_json, segments_to_lines, write_json
from eval.metrics import align_lines, normalize_text


ALIGNERS = {"current_xlsr", "mms_fa", "xlsr_ipa"}
IPA_MODEL_ID = "facebook/wav2vec2-xlsr-53-espeak-cv-ft"


def _neutral_segments(approved: Sequence[dict[str, Any]], duration_s: float) -> list[dict[str, Any]]:
    """Give runtime controls no approved timing, only ordered text."""
    weights = [max(1, len(normalize_text(str(row.get("text") or "")))) for row in approved]
    total = max(1, sum(weights))
    cursor = 0.0
    out = []
    for row, weight in zip(approved, weights):
        start = duration_s * cursor / total
        cursor += weight
        end = duration_s * cursor / total
        out.append({"start": start, "end": end, "text": str(row.get("text") or "")})
    return out


def _source_from_git(ref: str, path: str) -> tuple[str, str]:
    result = subprocess.run(
        ["git", "show", f"{ref}:{path}"], check=True, capture_output=True, text=True,
    )
    source = result.stdout
    return source, hashlib.sha256(source.encode("utf-8")).hexdigest()


def _load_current(ref: str) -> tuple[types.ModuleType, dict[str, str]]:
    path = "lyricgen/backend/ctc_align.py"
    source, sha = _source_from_git(ref, path)
    module = types.ModuleType("_pinned_ctc_align")
    module.__file__ = f"git:{ref}:{path}"
    exec(compile(source, module.__file__, "exec"), module.__dict__)
    return module, {"git_ref": ref, "source_path": path, "source_sha256": sha}


def _audio(path: Path):
    import torchaudio

    wav, sr = torchaudio.load(str(path))
    wav = wav.mean(0, keepdim=True)
    if sr != 16000:
        wav = torchaudio.functional.resample(wav, sr, 16000)
    return wav


def _chunked_emission(
    wav, forward: Callable[[Any], Any], *, chunk_s: float = 25.0, context_s: float = 3.0,
):
    """Bound wav2vec attention while preserving a global monotonic timeline."""
    import torch

    sr = 16000
    chunk, context = int(chunk_s * sr), int(context_s * sr)
    pieces = []
    with torch.inference_mode():
        for start in range(0, wav.shape[1], chunk):
            end = min(start + chunk, wav.shape[1])
            left, right = max(0, start - context), min(wav.shape[1], end + context)
            window = wav[:, left:right]
            emission = forward(window)
            if isinstance(emission, tuple):
                emission = emission[0]
            if hasattr(emission, "logits"):
                emission = emission.logits
            emission = emission[0]
            frames = emission.shape[0]
            ratio = frames / max(1, right - left)
            lo = int(round((start - left) * ratio))
            hi = int(round((end - left) * ratio))
            pieces.append(emission[lo:max(lo + 1, hi)].cpu())
    return torch.cat(pieces, dim=0)


def _plain_words(approved: Sequence[dict[str, Any]]) -> tuple[list[str], list[int]]:
    words, line_ids = [], []
    for line_idx, row in enumerate(approved):
        for word in re.findall(r"[^\W_]+(?:['’][^\W_]+)?", str(row.get("text") or ""), flags=re.UNICODE):
            words.append(word)
            line_ids.append(line_idx)
    return words, line_ids


def _romanize(word: str) -> str:
    value = unicodedata.normalize("NFD", word.casefold())
    return "".join(char for char in value if not unicodedata.combining(char) and char.isascii() and (char.isalpha() or char == "'"))


def _lines_from_word_spans(
    approved: Sequence[dict[str, Any]], words: Sequence[str], line_ids: Sequence[int],
    spans: Sequence[tuple[float, float, float]],
) -> list[dict[str, Any]]:
    by_line: dict[int, list[tuple[str, float, float, float]]] = {}
    for word, line_idx, span in zip(words, line_ids, spans):
        by_line.setdefault(line_idx, []).append((word, *span))
    output = []
    for line_idx, row in enumerate(approved):
        line_words = by_line.get(line_idx, [])
        if not line_words:
            output.append({"text": str(row.get("text") or ""), "unaligned": True})
            continue
        output.append({
            "text": str(row.get("text") or ""),
            "start": float(line_words[0][1]),
            "end": float(line_words[-1][2]),
            "words": [
                {"word": word, "start": start, "end": end, "score": score}
                for word, start, end, score in line_words
            ],
        })
    return output


_MMS = None


def _align_mms(audio_path: Path, approved: Sequence[dict[str, Any]]) -> tuple[list[dict[str, Any]] | None, dict[str, Any]]:
    global _MMS
    import torch
    import torchaudio

    bundle = torchaudio.pipelines.MMS_FA
    if _MMS is None:
        _MMS = (bundle.get_model().eval(), bundle.get_tokenizer(), bundle.get_aligner())
    model, tokenizer, aligner = _MMS
    words, line_ids = _plain_words(approved)
    normalized, kept_words, kept_ids = [], [], []
    for word, line_idx in zip(words, line_ids):
        value = _romanize(word)
        if value:
            normalized.append(value)
            kept_words.append(word)
            kept_ids.append(line_idx)
    wav = _audio(audio_path)
    emission = _chunked_emission(wav, model)
    with torch.inference_mode():
        token_spans = aligner(emission, tokenizer(normalized))
    ratio = wav.shape[1] / max(1, emission.shape[0]) / 16000.0
    spans = []
    for word_spans in token_spans:
        score_weight = sum(float(span.score) * max(1, span.end - span.start) for span in word_spans)
        weight = sum(max(1, span.end - span.start) for span in word_spans)
        spans.append((word_spans[0].start * ratio, word_spans[-1].end * ratio, score_weight / max(1, weight)))
    return _lines_from_word_spans(approved, kept_words, kept_ids, spans), {
        "model": "torchaudio.pipelines.MMS_FA",
        "license": "CC-BY-NC-4.0",
        "production_eligible": False,
    }


_IPA = None


ESPEAK_LANGUAGES = {
    "de": "de",
    "en": "en-us",
    "es": "es",
    "fr": "fr-fr",
    "it": "it",
    "pt": "pt",
}


def _phonemize_words(words: Sequence[str], language: str) -> list[str]:
    try:
        from phonemizer import phonemize
        from phonemizer.separator import Separator
    except ImportError as exc:
        raise RuntimeError("xlsr_ipa requires phonemizer and a local espeak-ng binary") from exc
    espeak_language = ESPEAK_LANGUAGES.get(language)
    if not espeak_language:
        raise RuntimeError(f"xlsr_ipa has no audited eSpeak mapping for language={language!r}")
    return list(phonemize(
        list(words), language=espeak_language, backend="espeak", strip=True, njobs=1,
        preserve_punctuation=False, with_stress=False,
        separator=Separator(phone="", word="", syllable=""),
    ))


def _align_ipa(
    audio_path: Path, approved: Sequence[dict[str, Any]], language: str,
) -> tuple[list[dict[str, Any]] | None, dict[str, Any]]:
    global _IPA
    import torch
    import torchaudio
    from transformers import AutoModelForCTC, AutoProcessor

    if _IPA is None:
        processor = AutoProcessor.from_pretrained(IPA_MODEL_ID, trust_remote_code=False)
        model = AutoModelForCTC.from_pretrained(IPA_MODEL_ID, trust_remote_code=False).eval()
        _IPA = model, processor
    model, processor = _IPA
    words, line_ids = _plain_words(approved)
    phonemes = _phonemize_words(words, language)
    token_groups, kept_words, kept_ids = [], [], []
    unk = processor.tokenizer.unk_token_id
    for word, line_idx, phones in zip(words, line_ids, phonemes):
        ids = processor.tokenizer(phones, add_special_tokens=False).input_ids
        ids = [int(value) for value in ids if int(value) != unk]
        if ids:
            token_groups.append(ids)
            kept_words.append(word)
            kept_ids.append(line_idx)
    targets = [token for group in token_groups for token in group]
    wav = _audio(audio_path)
    emission = _chunked_emission(
        wav, lambda window: model((window - window.mean()) / (window.std() + 1e-7)).logits,
    )
    log_probs = torch.log_softmax(emission, dim=-1)
    blank = int(processor.tokenizer.pad_token_id)
    aligned, scores = torchaudio.functional.forced_align(
        log_probs.unsqueeze(0), torch.tensor(targets, dtype=torch.int32).unsqueeze(0), blank=blank,
    )
    merged = torchaudio.functional.merge_tokens(aligned[0], scores[0].exp(), blank=blank)
    if len(merged) != len(targets):
        raise RuntimeError(f"IPA token/span mismatch: {len(targets)} targets, {len(merged)} spans")
    ratio = wav.shape[1] / max(1, emission.shape[0]) / 16000.0
    spans, offset = [], 0
    for group in token_groups:
        chunk = merged[offset:offset + len(group)]
        offset += len(group)
        weight = sum(max(1, span.end - span.start) for span in chunk)
        score = sum(float(span.score) * max(1, span.end - span.start) for span in chunk) / max(1, weight)
        spans.append((chunk[0].start * ratio, chunk[-1].end * ratio, score))
    return _lines_from_word_spans(approved, kept_words, kept_ids, spans), {
        "model": IPA_MODEL_ID,
        "license": "Apache-2.0",
        "production_eligible": True,
        "g2p": f"espeak-{ESPEAK_LANGUAGES[language]}-local",
    }


def _align_current(
    module: types.ModuleType, audio_path: Path, approved: Sequence[dict[str, Any]], duration_s: float, song_id: str,
) -> tuple[list[dict[str, Any]] | None, dict[str, Any]]:
    neutral = _neutral_segments(approved, duration_s)
    os.environ["CTC_ALIGN_ENABLED"] = "1"
    result = module.retime_segments(str(audio_path), neutral, job_id=f"gold-no-timing-{song_id}")
    return result, {
        "model": getattr(module, "MODEL_ID", None),
        "model_revision": getattr(module, "MODEL_REVISION", None),
        "approved_timing_input": False,
        "neutral_timing_policy": "character-weighted uniform full-song scaffold",
    }


def _score_prediction(
    song_id: str, prediction: Sequence[dict[str, Any]], approved: Sequence[dict[str, Any]],
    raw: Sequence[dict[str, Any]],
) -> dict[str, Any]:
    if len(prediction) != len(approved):
        raise RuntimeError(f"{song_id}: prediction has {len(prediction)} lines, expected {len(approved)}")
    start_errors, end_errors, boundary_errors = [], [], []
    both_ok, aligned = 0, 0
    per_line = []
    normalized_texts = [normalize_text(str(row.get("text") or "")) for row in approved]
    repeat_counts = {value: normalized_texts.count(value) for value in set(normalized_texts)}
    for idx, (hyp, ref) in enumerate(zip(prediction, approved)):
        if hyp.get("start") is None or hyp.get("end") is None:
            per_line.append({"line_idx": idx, "aligned": False})
            continue
        start_error = (float(hyp["start"]) - float(ref["start"])) * 1000.0
        end_error = (float(hyp["end"]) - float(ref["end"])) * 1000.0
        start_errors.append(abs(start_error))
        end_errors.append(abs(end_error))
        boundary_errors.extend((abs(start_error), abs(end_error)))
        ok = abs(start_error) <= 150 and abs(end_error) <= 150
        both_ok += int(ok)
        aligned += 1
        predicted_words = hyp.get("words") if isinstance(hyp.get("words"), list) else []
        scores = [float(word["score"]) for word in predicted_words if word.get("score") is not None]
        first_word_duration = (
            float(predicted_words[0]["end"]) - float(predicted_words[0]["start"])
            if predicted_words else 0.0
        )
        last_word_duration = (
            float(predicted_words[-1]["end"]) - float(predicted_words[-1]["start"])
            if predicted_words else 0.0
        )
        previous_end = float(prediction[idx - 1]["end"]) if idx and prediction[idx - 1].get("end") is not None else float(hyp["start"])
        next_start = float(prediction[idx + 1]["start"]) if idx + 1 < len(prediction) and prediction[idx + 1].get("start") is not None else float(hyp["end"])
        text_norm = normalized_texts[idx]
        per_line.append({
            "line_idx": idx, "aligned": True, "text": str(ref.get("text") or ""),
            "predicted_start": float(hyp["start"]), "approved_start": float(ref["start"]),
            "predicted_end": float(hyp["end"]), "approved_end": float(ref["end"]),
            "start_error_ms": start_error, "end_error_ms": end_error, "within_150ms_both": ok,
            "features": {
                "predicted_duration_s": max(0.0, float(hyp["end"]) - float(hyp["start"])),
                "gap_before_s": float(hyp["start"]) - previous_end,
                "gap_after_s": next_start - float(hyp["end"]),
                "line_position": idx / max(1, len(approved) - 1),
                "word_count": len(text_norm.split()),
                "character_count": len(text_norm.replace(" ", "")),
                "first_word_duration_s": first_word_duration,
                "last_word_duration_s": last_word_duration,
                "mean_alignment_score": float(np.mean(scores)) if scores else 0.0,
                "repeated_occurrences": repeat_counts[text_norm],
                "parenthesized": float(str(ref.get("text") or "").strip().startswith("(")),
            },
        })

    approved_lines, raw_lines = segments_to_lines(approved), segments_to_lines(raw)
    raw_alignment = align_lines(approved_lines, raw_lines)
    raw_text_untouched = {
        match["ref_idx"] for match in raw_alignment["matches"]
        if normalize_text(approved_lines[match["ref_idx"]]["text"])
        == normalize_text(raw_lines[match["hyp_idx"]]["text"])
    }
    for row in per_line:
        row["raw_text_untouched"] = bool(row.get("aligned") and row["line_idx"] in raw_text_untouched)
    projected_zero = sum(
        bool(row.get("within_150ms_both")) and row["line_idx"] in raw_text_untouched
        for row in per_line
    )
    work_units = (
        len(raw_alignment["matches"])
        + len(raw_alignment["omitted_ref_indices"])
        + len(raw_alignment["invented_hyp_indices"])
    )
    return {
        "song_id": song_id, "approved_lines": len(approved), "aligned_lines": aligned,
        "coverage": aligned / max(1, len(approved)),
        "within_150ms_both": both_ok / max(1, len(approved)),
        "start_abs_ms": start_errors, "end_abs_ms": end_errors,
        "boundary_abs_ms": boundary_errors,
        "projected_zero_touch_lines": projected_zero,
        "work_units": work_units,
        "projected_ztlr": projected_zero / max(1, work_units),
        "per_line": per_line,
    }


def _aggregate(rows: Sequence[dict[str, Any]]) -> dict[str, Any]:
    def boundaries(sample):
        return [value for row in sample for value in row["boundary_abs_ms"]]
    def p(sample, fraction):
        values = boundaries(sample)
        return percentile(values, fraction) if values else float("inf")
    def within(sample):
        return sum(row["within_150ms_both"] * row["approved_lines"] for row in sample) / max(1, sum(row["approved_lines"] for row in sample))
    def coverage(sample):
        return sum(row["aligned_lines"] for row in sample) / max(1, sum(row["approved_lines"] for row in sample))
    def ztlr(sample):
        return sum(row["projected_zero_touch_lines"] for row in sample) / max(1, sum(row["work_units"] for row in sample))
    p90 = song_bootstrap_ci(rows, lambda sample: p(sample, 0.90))
    return {
        "songs": len(rows),
        "boundaries": len(boundaries(rows)),
        "p50_boundary_abs_ms": song_bootstrap_ci(rows, lambda sample: p(sample, 0.50)),
        "p90_boundary_abs_ms": p90,
        "within_150ms_both": song_bootstrap_ci(rows, within),
        "coverage": song_bootstrap_ci(rows, coverage),
        "projected_ztlr": song_bootstrap_ci(rows, ztlr),
        "gate": {"requirement": "p90_boundary_abs_ms < 250", "status": "GO" if p90["estimate"] < 250 else "NO_GO"},
    }


def _loo_display_calibration(rows: Sequence[dict[str, Any]]) -> dict[str, Any] | None:
    """Learn acoustic→display deltas without ever training on the held song."""
    if len(rows) < 5:
        return None
    from lightgbm import LGBMRegressor

    feature_names = [
        "predicted_duration_s", "gap_before_s", "gap_after_s", "line_position",
        "word_count", "character_count", "first_word_duration_s",
        "last_word_duration_s", "mean_alignment_score", "repeated_occurrences",
        "parenthesized",
    ]
    flat = [
        {"song_id": song["song_id"], **line}
        for song in rows for line in song["per_line"]
        if line.get("aligned") and line.get("features")
    ]
    calibrated_by_song: dict[str, list[dict[str, Any]]] = {}
    median_by_song: dict[str, list[dict[str, Any]]] = {}
    for held_song in sorted({row["song_id"] for row in flat}):
        train = [row for row in flat if row["song_id"] != held_song]
        test = [row for row in flat if row["song_id"] == held_song]
        start_median_correction = -float(np.median([row["start_error_ms"] for row in train]))
        end_median_correction = -float(np.median([row["end_error_ms"] for row in train]))
        for row in test:
            median_by_song.setdefault(held_song, []).append({
                **row,
                "start_error_ms": float(row["start_error_ms"]) + start_median_correction,
                "end_error_ms": float(row["end_error_ms"]) + end_median_correction,
                "start_display_delta_ms": start_median_correction,
                "end_display_delta_ms": end_median_correction,
            })
        x_train = np.asarray([[row["features"][name] for name in feature_names] for row in train], dtype=np.float32)
        x_test = np.asarray([[row["features"][name] for name in feature_names] for row in test], dtype=np.float32)
        predictions = []
        for target in ("start_error_ms", "end_error_ms"):
            # Target is the correction to add, the negative signed residual.
            y = np.asarray([-float(row[target]) for row in train], dtype=np.float32)
            model = LGBMRegressor(
                n_estimators=120, learning_rate=0.035, max_depth=3,
                num_leaves=12, min_child_samples=20, reg_lambda=2.0,
                verbosity=-1, random_state=20260829,
            )
            model.fit(x_train, y)
            predictions.append(np.clip(model.predict(x_test), -1500.0, 1500.0))
        for index, row in enumerate(test):
            calibrated_by_song.setdefault(held_song, []).append({
                **row,
                "start_error_ms": float(row["start_error_ms"]) + float(predictions[0][index]),
                "end_error_ms": float(row["end_error_ms"]) + float(predictions[1][index]),
                "start_display_delta_ms": float(predictions[0][index]),
                "end_display_delta_ms": float(predictions[1][index]),
            })

    def rebuild(by_song):
        calibrated_rows = []
        for original in rows:
            lines = by_song.get(original["song_id"], [])
            start_abs = [abs(row["start_error_ms"]) for row in lines]
            end_abs = [abs(row["end_error_ms"]) for row in lines]
            both = [abs(row["start_error_ms"]) <= 150 and abs(row["end_error_ms"]) <= 150 for row in lines]
            projected_zero = sum(ok and row["raw_text_untouched"] for ok, row in zip(both, lines))
            calibrated_rows.append({
                "song_id": original["song_id"],
                "approved_lines": original["approved_lines"],
                "aligned_lines": len(lines),
                "within_150ms_both": sum(both) / max(1, original["approved_lines"]),
                "start_abs_ms": start_abs, "end_abs_ms": end_abs,
                "boundary_abs_ms": start_abs + end_abs,
                "projected_zero_touch_lines": projected_zero,
                "work_units": original["work_units"],
                "per_line": lines,
            })
        return calibrated_rows
    lightgbm_rows = rebuild(calibrated_by_song)
    median_rows = rebuild(median_by_song)
    return {
        "policy": "acoustic-to-display residual; strict leave-one-song-out",
        "feature_names": feature_names,
        "correction_cap_ms": 1500,
        "variants": {
            "robust_global_median": {
                "metrics": _aggregate(median_rows), "by_song": median_rows,
            },
            "lightgbm": {
                "metrics": _aggregate(lightgbm_rows), "by_song": lightgbm_rows,
            },
        },
    }


def run(
    golden: Path, stems: Path, output: Path, aligners: Sequence[str], runtime_ref: str,
    limit: int | None, song_ids: set[str] | None = None, audio_source: str = "stem",
) -> dict[str, Any]:
    unknown = set(aligners) - ALIGNERS
    if unknown:
        raise ValueError(f"unknown aligners: {sorted(unknown)}")
    if audio_source not in {"stem", "mix"}:
        raise ValueError("audio_source must be stem or mix")
    output.mkdir(parents=True, exist_ok=True)
    manifest = read_json(golden / "manifest.json")
    cases = [item for item in manifest["cases"] if item["raw_quality"] in {"exact", "reconstructed"}]
    if song_ids:
        cases = [item for item in cases if item["song_id"] in song_ids]
    if limit is not None:
        cases = cases[:limit]
    current = current_source = None
    if "current_xlsr" in aligners:
        current, current_source = _load_current(runtime_ref)
    reports: dict[str, list[dict[str, Any]]] = {name: [] for name in aligners}
    failures: dict[str, list[dict[str, str]]] = {name: [] for name in aligners}
    source_metadata: dict[str, Any] = {"current_xlsr": current_source} if current_source else {}

    for position, item in enumerate(cases, 1):
        case = golden / item["path"]
        meta = read_json(case / "meta.json")
        audio_path = (
            stems / item["song_id"] / "vocals.wav"
            if audio_source == "stem"
            else case / meta["audio"]["filename"]
        )
        if not audio_path.is_file():
            for name in aligners:
                failures[name].append({"song_id": item["song_id"], "reason": f"missing_{audio_source}_audio"})
            continue
        approved = read_json(case / "approved.json")
        raw = read_json(case / "raw_pipeline_output.json")["segments"]
        duration = float(meta["duration_s"])
        language = str((meta.get("language") or {}).get("value") or "")
        for name in aligners:
            destination = output / name / f"{item['song_id']}.json"
            print(f"realign {position}/{len(cases)} {name} {item['song_id']}", flush=True)
            try:
                stale_ipa = False
                if destination.is_file():
                    persisted = read_json(destination)
                    expected_g2p = f"espeak-{ESPEAK_LANGUAGES.get(language)}-local"
                    stale_ipa = name == "xlsr_ipa" and persisted.get("metadata", {}).get("g2p") != expected_g2p
                    if stale_ipa:
                        prediction, metadata = _align_ipa(audio_path, approved, language)
                    else:
                        prediction, metadata = persisted["prediction"], persisted["metadata"]
                elif name == "current_xlsr":
                    prediction, metadata = _align_current(current, audio_path, approved, duration, item["song_id"])
                elif name == "mms_fa":
                    prediction, metadata = _align_mms(audio_path, approved)
                else:
                    prediction, metadata = _align_ipa(audio_path, approved, language)
                if not prediction:
                    raise RuntimeError("aligner declined")
                if not destination.is_file() or stale_ipa:
                    write_json(destination, {
                        "song_id": item["song_id"], "aligner": name,
                        "approved_text_sha256": hashlib.sha256("\n".join(str(row.get("text") or "") for row in approved).encode()).hexdigest(),
                        "approved_timing_supplied_to_aligner": False,
                        "audio_source": audio_source,
                        "mix_audio_sha256": meta["audio"]["sha256"],
                        "metadata": metadata, "prediction": prediction,
                    })
                reports[name].append(_score_prediction(item["song_id"], prediction, approved, raw))
                source_metadata.setdefault(name, metadata)
            except Exception as exc:
                failures[name].append({"song_id": item["song_id"], "reason": f"{type(exc).__name__}: {exc}"})
                print(f"  decline {type(exc).__name__}: {exc}", flush=True)

    summary = {
        "schema_version": 1,
        "experiment": "final-text-conditioned-realignment",
        "gold_leakage": False,
        "approved_timing_supplied_to_aligner": False,
        "audio_source": audio_source,
        "eligible_songs": len(cases),
        "audio_available": sum(
            (
                (stems / item["song_id"] / "vocals.wav")
                if audio_source == "stem"
                else (golden / item["path"] / read_json(golden / item["path"] / "meta.json")["audio"]["filename"])
            ).is_file()
            for item in cases
        ),
        "sources": source_metadata,
        "aligners": {
            name: {
                "metrics": _aggregate(rows) if rows else None,
                "loo_display_calibration": _loo_display_calibration(rows),
                "failures": failures[name],
                "by_song": rows,
            }
            for name, rows in reports.items()
        },
    }
    write_json(output / "report.json", summary)
    print(json.dumps({name: payload["metrics"] for name, payload in summary["aligners"].items()}, indent=2))
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--golden", type=Path, default=Path("eval/golden"))
    parser.add_argument("--stems", type=Path, default=Path("eval/cache/full_stems"))
    parser.add_argument("--output", type=Path, default=Path("eval/runs/final_text_realign"))
    parser.add_argument("--aligners", default=",".join(sorted(ALIGNERS)))
    parser.add_argument("--runtime-ref", default="origin/staging")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--song-id", action="append", default=[])
    parser.add_argument("--audio-source", choices=("stem", "mix"), default="stem")
    args = parser.parse_args()
    names = [name.strip() for name in args.aligners.split(",") if name.strip()]
    run(
        args.golden.resolve(), args.stems.resolve(), args.output.resolve(), names,
        args.runtime_ref, args.limit, set(args.song_id) or None, args.audio_source,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
