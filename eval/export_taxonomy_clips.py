#!/usr/bin/env python3
"""Attach bounded local audio clips to the disputed taxonomy queue."""

from __future__ import annotations

import argparse
import csv
import json
from collections import defaultdict
from pathlib import Path
from typing import Any

import librosa
import soundfile as sf

from eval.canonical import read_json, write_json


def run(golden: Path, queue: Path, output: Path) -> dict[str, Any]:
    rows = list(csv.DictReader(queue.open(encoding="utf-8")))
    by_song: dict[str, list[tuple[int, dict[str, str]]]] = defaultdict(list)
    for index, row in enumerate(rows):
        by_song[row["song_id"]].append((index, row))
    clip_dir = output / "clips"
    clip_dir.mkdir(parents=True, exist_ok=True)
    result_rows: list[dict[str, Any] | None] = [None] * len(rows)
    failures = []
    for song_id, song_rows in sorted(by_song.items()):
        case = golden / song_id
        meta = read_json(case / "meta.json")
        audio, sample_rate = librosa.load(case / meta["audio"]["filename"], sr=16000, mono=True)
        raw = read_json(case / "raw_pipeline_output.json").get("segments") or []
        approved = read_json(case / "lines.json")
        for index, row in song_rows:
            try:
                if row.get("hyp_line_idx") not in {None, ""}:
                    segment = raw[int(row["hyp_line_idx"])]
                    start, end = float(segment["start"]), float(segment["end"])
                elif row.get("ref_line_idx") not in {None, ""}:
                    segment = approved[int(row["ref_line_idx"])]
                    start, end = float(segment["start_s"]), float(segment["end_s"])
                else:
                    raise ValueError("no line anchor")
                center = (start + end) / 2
                clip_start = max(0.0, min(start - 1.0, center - 6.0))
                clip_end = min(len(audio) / sample_rate, max(end + 1.0, center + 6.0))
                if clip_end - clip_start > 12.0:
                    clip_start, clip_end = max(0.0, center - 6.0), min(len(audio) / sample_rate, center + 6.0)
                clip_path = clip_dir / f"{index:04d}-{song_id}.flac"
                sf.write(
                    clip_path,
                    audio[round(clip_start * sample_rate):round(clip_end * sample_rate)],
                    sample_rate, subtype="PCM_16",
                )
                result_rows[index] = {**row, "audio_clip": str(clip_path.resolve()), "clip_start_s": clip_start, "clip_end_s": clip_end}
            except (IndexError, KeyError, TypeError, ValueError) as error:
                failures.append({"row": index, "song_id": song_id, "error": type(error).__name__})
                result_rows[index] = {**row, "audio_clip": "", "clip_start_s": "", "clip_end_s": ""}
    complete = [row for row in result_rows if row is not None]
    with (output / "taxonomy_adjudication_with_audio.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(complete[0]))
        writer.writeheader(); writer.writerows(complete)
    report = {
        "schema_version": 1,
        "queue_rows": len(rows),
        "clips": sum(bool(row.get("audio_clip")) for row in complete),
        "failures": failures,
        "data_egress": False,
        "purpose": "human adjudication only",
    }
    write_json(output / "clip_report.json", report)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--golden", type=Path, default=Path("eval/golden"))
    parser.add_argument("--queue", type=Path, default=Path("eval/runs/taxonomy_ensemble/taxonomy_adjudication_queue.csv"))
    parser.add_argument("--output", type=Path, default=Path("eval/runs/taxonomy_adjudication"))
    args = parser.parse_args()
    print(json.dumps(run(args.golden.resolve(), args.queue.resolve(), args.output.resolve()), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
