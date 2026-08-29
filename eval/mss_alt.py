"""MSS-ALT replay: use separated vocals as VAD, transcribe the original mix.

This follows the central design of arXiv:2506.15514: source separation derives
long-form boundaries but is not automatically the signal sent to Whisper.  A
native long-form control and the RMS-VAD variant always use the same model,
language and decoding options.  Outputs are resumable and preserve every
hypothesis family for future replay.
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import Any, Sequence

import numpy as np

from eval.bootstrap import song_bootstrap_ci
from eval.canonical import read_json, segments_to_lines, write_json
from eval.metrics import error_rate_counts, full_text


def rms_vad_boundaries(
    stem: Path, *, threshold: float = 0.10, minimum_silence_s: float = 1.0,
    maximum_segment_s: float = 30.0,
) -> list[tuple[float, float]]:
    import librosa

    waveform, sample_rate = librosa.load(str(stem), sr=16000, mono=True)
    hop = 320
    rms = librosa.feature.rms(y=waveform, frame_length=1024, hop_length=hop)[0]
    if not len(rms) or float(np.max(rms)) <= 0:
        return []
    active = rms / float(np.max(rms)) >= threshold
    minimum_silence_frames = max(1, int(round(minimum_silence_s * sample_rate / hop)))
    regions, start, quiet = [], None, 0
    for index, value in enumerate(active):
        if value:
            if start is None:
                start = index
            quiet = 0
        elif start is not None:
            quiet += 1
            if quiet >= minimum_silence_frames:
                end = index - quiet + 1
                regions.append((start * hop / sample_rate, max(start + 1, end) * hop / sample_rate))
                start, quiet = None, 0
    if start is not None:
        regions.append((start * hop / sample_rate, len(rms) * hop / sample_rate))

    # Cut & merge: retain the vocal intervals and group adjacent ones while
    # the resulting Whisper window remains <=30 s. Long regions are split.
    split_regions = []
    for left, right in regions:
        while right - left > maximum_segment_s:
            split_regions.append((left, left + maximum_segment_s))
            left += maximum_segment_s
        if right > left:
            split_regions.append((left, right))
    groups: list[tuple[float, float]] = []
    for left, right in split_regions:
        left = max(0.0, left - 0.20)
        right = min(len(waveform) / sample_rate, right + 0.20)
        if groups and right - groups[-1][0] <= maximum_segment_s:
            groups[-1] = (groups[-1][0], right)
        else:
            groups.append((left, right))
    return groups


def _transcribe(model, audio, language: str) -> dict[str, Any]:
    return model.transcribe(
        audio, language=language, word_timestamps=True, verbose=False,
        condition_on_previous_text=False, beam_size=1, best_of=1,
    )


def _offset_segments(segments: Sequence[dict[str, Any]], offset: float) -> list[dict[str, Any]]:
    output = []
    for segment in segments:
        copied = dict(segment)
        copied["start"] = float(segment.get("start") or 0) + offset
        copied["end"] = float(segment.get("end") or 0) + offset
        copied["words"] = [
            {
                **word,
                "start": float(word["start"]) + offset if word.get("start") is not None else None,
                "end": float(word["end"]) + offset if word.get("end") is not None else None,
            }
            for word in (segment.get("words") or [])
        ]
        output.append(copied)
    return output


def _score(approved: Sequence[dict[str, Any]], hypothesis: Sequence[dict[str, Any]]) -> dict[str, Any]:
    reference = full_text(segments_to_lines(approved))
    hypothesis_text = " ".join(str(segment.get("text") or "") for segment in hypothesis)
    counts = error_rate_counts(reference, hypothesis_text)
    return {
        **counts,
        "wer": counts["word_edits"] / max(1, counts["reference_words"]),
    }


def run(
    golden: Path, stems: Path, output: Path, model_name: str, limit: int | None,
    song_ids: set[str] | None,
) -> dict[str, Any]:
    import whisper

    manifest = read_json(golden / "manifest.json")
    cases = [item for item in manifest["cases"] if item["raw_quality"] in {"exact", "reconstructed"}]
    if song_ids:
        cases = [item for item in cases if item["song_id"] in song_ids]
    if limit is not None:
        cases = cases[:limit]
    model = whisper.load_model(model_name)
    rows, failures = [], []
    for position, item in enumerate(cases, 1):
        case, song_id = golden / item["path"], item["song_id"]
        stem = stems / song_id / "vocals.wav"
        if not stem.is_file():
            failures.append({"song_id": song_id, "reason": "missing_exact_mdx_extra_stem"})
            continue
        meta = read_json(case / "meta.json")
        mix = case / meta["audio"]["filename"]
        language = (meta.get("language") or {}).get("value") or "es"
        audio = whisper.load_audio(str(mix))
        boundaries = rms_vad_boundaries(stem)
        family_metrics = {}
        for family in ("native", "mss_rms_vad"):
            destination = output / model_name / family / f"{song_id}.json"
            print(f"mss-alt {position}/{len(cases)} {model_name} {family} {song_id}", flush=True)
            if destination.is_file():
                payload = read_json(destination)
                segments = payload["segments"]
            elif family == "native":
                result = _transcribe(model, audio, language)
                segments = result.get("segments") or []
            else:
                segments = []
                for left, right in boundaries:
                    chunk = audio[int(left * 16000):int(right * 16000)]
                    result = _transcribe(model, chunk, language)
                    segments.extend(_offset_segments(result.get("segments") or [], left))
            if not destination.is_file():
                write_json(destination, {
                    "schema_version": 1, "song_id": song_id, "family": family,
                    "model": model_name, "input_audio": "original_mix",
                    "boundary_source": "native" if family == "native" else "mdx_extra_rms_vad",
                    "boundaries": boundaries if family == "mss_rms_vad" else None,
                    "segments": segments,
                })
            family_metrics[family] = _score(read_json(case / "approved.json"), segments)
        rows.append({"song_id": song_id, "families": family_metrics})

    def corpus_wer(sample, family):
        edits = sum(row["families"][family]["word_edits"] for row in sample)
        words = sum(row["families"][family]["reference_words"] for row in sample)
        return edits / max(1, words)
    summary = {
        "schema_version": 1, "experiment": "arxiv-2506.15514-rms-vad",
        "model": model_name, "data_egress": False,
        "eligible_songs": len(cases), "completed_songs": len(rows), "failures": failures,
        "families": {
            family: {
                "wer": song_bootstrap_ci(rows, lambda sample, name=family: corpus_wer(sample, name)),
            }
            for family in ("native", "mss_rms_vad")
        },
        "by_song": rows,
        "ztlr": "NOT_DIRECTLY_DERIVABLE_FROM_UNFORMATTED_ASR_SEGMENTS",
    }
    write_json(output / model_name / "report.json", summary)
    print(json.dumps({"completed_songs": len(rows), "families": summary["families"]}, indent=2))
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--golden", type=Path, default=Path("eval/golden"))
    parser.add_argument("--stems", type=Path, default=Path("eval/cache/full_stems"))
    parser.add_argument("--output", type=Path, default=Path("eval/runs/mss_alt"))
    parser.add_argument("--model", default="large-v3-turbo")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--song-id", action="append", default=[])
    args = parser.parse_args()
    run(
        args.golden.resolve(), args.stems.resolve(), args.output.resolve(), args.model,
        args.limit, set(args.song_id) or None,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
