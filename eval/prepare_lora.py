#!/usr/bin/env python3
"""Prepare local Whisper LoRA samples and enforce the client-data policy gate."""

from __future__ import annotations

import argparse
import math
import random
from collections import Counter
from pathlib import Path
from typing import Any

from eval.canonical import read_json, write_json


def _chunks(lines: list[dict[str, Any]], maximum_s: float = 25.0) -> list[dict[str, Any]]:
    bounded_lines = []
    for line in lines:
        text_value = str(line.get("text") or "").strip()
        if not text_value:
            continue
        start, end = float(line["start_s"]), float(line["end_s"])
        duration = max(0.0, end - start)
        part_count = max(1, math.ceil(duration / maximum_s))
        words = text_value.split()
        # A single sustained token is not useful enough to justify duplicating
        # its label over multiple training chunks.
        if part_count > len(words):
            continue
        for part in range(part_count):
            word_left = round(part * len(words) / part_count)
            word_right = round((part + 1) * len(words) / part_count)
            bounded_lines.append({
                "start_s": start + duration * part / part_count,
                "end_s": start + duration * (part + 1) / part_count,
                "text": " ".join(words[word_left:word_right]),
            })
    chunks = []
    current = []
    for line in bounded_lines:
        if current and float(line["end_s"]) - float(current[0]["start_s"]) > maximum_s:
            chunks.append({
                "start_s": float(current[0]["start_s"]), "end_s": float(current[-1]["end_s"]),
                "text": " ".join(item["text"] for item in current),
            })
            current = []
        current.append(line)
    if current:
        chunks.append({
            "start_s": float(current[0]["start_s"]), "end_s": float(current[-1]["end_s"]),
            "text": " ".join(item["text"] for item in current),
        })
    return chunks


def prepare(golden: Path, output: Path) -> dict[str, Any]:
    manifest = read_json(golden / "manifest.json")
    songs = []
    for item in manifest["cases"]:
        case = golden / item["path"]
        meta = read_json(case / "meta.json")
        songs.append({
            "song_id": item["song_id"], "artist": str(meta.get("artist") or "unknown"),
            "audio_path": str((case / meta["audio"]["filename"]).resolve()),
            "language": (meta.get("language") or {}).get("value"),
            "chunks": _chunks(read_json(case / "lines.json")),
        })
    generator = random.Random(20260829)
    song_ids = [song["song_id"] for song in songs]
    generator.shuffle(song_ids)
    validation_ids = set(song_ids[: max(1, round(0.20 * len(song_ids)))])
    artist_counts = Counter(song["artist"] for song in songs)
    artists = sorted(artist_counts, key=lambda artist: (-artist_counts[artist], artist.lower()))
    leave_artist_out = set()
    held_out_songs = 0
    for artist in artists:
        if held_out_songs >= round(0.20 * len(songs)):
            break
        leave_artist_out.add(artist)
        held_out_songs += artist_counts[artist]
    samples = []
    for song in songs:
        for chunk_index, chunk in enumerate(song["chunks"]):
            samples.append({
                "sample_id": f"{song['song_id']}-{chunk_index:03d}",
                "song_id": song["song_id"], "artist": song["artist"],
                "audio_path": song["audio_path"], "language": song["language"], **chunk,
                "song_split": "validation" if song["song_id"] in validation_ids else "train",
                "artist_split": "leave_artist_out" if song["artist"] in leave_artist_out else "train",
            })
    output.mkdir(parents=True, exist_ok=True)
    with (output / "samples.jsonl").open("w", encoding="utf-8") as handle:
        import json
        for sample in samples:
            handle.write(json.dumps(sample, ensure_ascii=False) + "\n")
    report = {
        "schema_version": 1, "base_model": "openai/whisper-large-v3-turbo",
        "songs": len(songs), "samples": len(samples),
        "song_split": {
            "train_songs": len(songs) - len(validation_ids), "validation_songs": len(validation_ids),
            "validation_song_ids": sorted(validation_ids),
        },
        "leave_artist_out": {
            "artists": sorted(leave_artist_out), "songs": held_out_songs,
        },
        "execution_gate": {
            "status": "BLOCKED_POLICY_AUTHORIZATION",
            "reason": (
                "docs/GenLy_AI_Compliance_Report.md states that GenLy does not fine-tune on "
                "client data and that audio never leaves GenLy infrastructure. A RunPod upload "
                "would change that representation and requires explicit policy/contract approval."
            ),
            "estimated_gpu_budget_usd": [20, 30],
            "data_egress_performed": False,
        },
    }
    write_json(output / "report.json", report)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--golden", type=Path, default=Path("eval/golden"))
    parser.add_argument("--output", type=Path, default=Path("eval/runs/lora_v1_prep"))
    args = parser.parse_args()
    report = prepare(args.golden.resolve(), args.output.resolve())
    print(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
