#!/usr/bin/env python3
"""Independent Gemini-audio candidate family for heavy-song replay.

External client-audio egress is off by default. The generator sees only audio,
pre-transcription metadata and LID; approved lyrics/timing are never loaded.
"""

from __future__ import annotations

import argparse
import io
import json
import os
from pathlib import Path
from typing import Any, Sequence

import numpy as np
import soundfile as sf

from eval.agent_corrector import _gemini_client
from eval.canonical import read_json, write_json
from eval.mss_alt import rms_vad_boundaries


def parse_response(text: str, duration_s: float) -> list[dict[str, Any]]:
    payload = json.loads(text)
    if not isinstance(payload, dict) or not isinstance(payload.get("segments"), list):
        raise ValueError("Gemini response must contain segments[]")
    output = []
    for row in payload["segments"]:
        value = str(row.get("text") or "").strip()
        try:
            start, end = float(row["start"]), float(row["end"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("Gemini segment needs numeric start/end") from exc
        if not value or not 0 <= start < end <= duration_s + .25:
            raise ValueError("Gemini segment outside supplied clip")
        output.append({"start": max(0.0, start), "end": min(duration_s, end), "text": value})
    return output


def _wav_bytes(audio: np.ndarray, sample_rate: int) -> bytes:
    handle = io.BytesIO()
    sf.write(handle, audio, sample_rate, format="WAV", subtype="PCM_16")
    return handle.getvalue()


def _offset(rows: Sequence[dict[str, Any]], left: float) -> list[dict[str, Any]]:
    return [{**row, "start": float(row["start"]) + left, "end": float(row["end"]) + left} for row in rows]


def run(
    golden: Path, routes_path: Path, stems: Path, lid_root: Path, output: Path,
    model: str, limit: int | None,
) -> dict[str, Any]:
    if os.environ.get("ALLOW_EXTERNAL_CLIENT_AUDIO_HEAVY_REPLAY") != "1":
        raise RuntimeError("client-audio egress blocked; set ALLOW_EXTERNAL_CLIENT_AUDIO_HEAVY_REPLAY=1 explicitly")
    from google import genai
    import csv
    import whisper

    with routes_path.open(newline="", encoding="utf-8") as handle:
        routes = [row for row in csv.DictReader(handle) if int(row["route_heavy"])]
    if limit is not None:
        routes = routes[:limit]
    manifest = {row["song_id"]: row for row in read_json(golden / "manifest.json")["cases"]}
    client = _gemini_client()
    completed, failures = [], []
    prompt = (
        "Transcribe only the sung or compositional spoken lyrics audible in this audio clip. "
        "Preserve Spanish/English code-switching, repetitions and melodic interjections. "
        "Never infer missing lyrics from song knowledge and do not translate. "
        "Return JSON {segments:[{start,end,text}]} with seconds relative to the clip; "
        "use an empty array when uncertain or when there are no lyrics."
    )
    for position, route in enumerate(routes, 1):
        song_id, item = route["song_id"], manifest[route["song_id"]]
        destination = output / song_id / "hypothesis.json"
        if destination.is_file():
            completed.append(song_id)
            continue
        case = golden / item["path"]
        meta = read_json(case / "meta.json")
        stem = stems / song_id / "vocals.wav"
        if not stem.is_file():
            failures.append({"song_id": song_id, "reason": "missing_full_stem"})
            continue
        audio = whisper.load_audio(str(case / meta["audio"]["filename"]))
        lid_path = lid_root / "cases" / f"{song_id}.json"
        lid = read_json(lid_path) if lid_path.is_file() else {}
        languages = []
        if lid.get("input_source") == "full_vocal_stem":
            languages = lid.get("persistent_languages") or lid.get("confirmed_languages") or []
        song_prompt = prompt + (
            f" Acoustic LID candidates: {', '.join(languages)}; treat these only as hints."
            if languages else " Acoustic LID is uncertain; preserve the language you hear."
        )
        boundaries, segments = rms_vad_boundaries(stem), []
        for chunk_index, (left, right) in enumerate(boundaries):
            chunk_path = output / ".chunks" / song_id / f"{chunk_index:03d}.json"
            if chunk_path.is_file():
                rows = read_json(chunk_path)["segments"]
            else:
                chunk = audio[int(left * 16000):int(right * 16000)]
                response = client.models.generate_content(
                    model=model,
                    contents=[
                        genai.types.Part.from_text(text=song_prompt),
                        genai.types.Part.from_bytes(data=_wav_bytes(chunk, 16000), mime_type="audio/wav"),
                    ],
                    config=genai.types.GenerateContentConfig(
                        temperature=0, max_output_tokens=3000, response_mime_type="application/json",
                        thinking_config=genai.types.ThinkingConfig(thinking_budget=0),
                    ),
                )
                rows = parse_response(response.text, right - left)
                write_json(chunk_path, {
                    "schema_version": 1, "song_id": song_id, "chunk_index": chunk_index,
                    "start_s": left, "end_s": right, "model": model, "segments": rows,
                    "approved_text_visible": False,
                })
            segments.extend(_offset(rows, left))
        write_json(destination, {
            "schema_version": 1, "song_id": song_id, "family": "gemini_audio",
            "model": model, "approved_text_visible": False, "segments": segments,
        })
        completed.append(song_id)
        print(f"gemini heavy {position}/{len(routes)} {song_id}", flush=True)
    report = {
        "schema_version": 1, "family": "gemini_audio", "model": model,
        "routed": len(routes), "completed": len(completed), "failures": failures,
        "gold_visible_to_generator": False,
    }
    write_json(output / "report.json", report)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--golden", type=Path, default=Path("eval/golden"))
    parser.add_argument("--routes", type=Path, default=Path("eval/runs/difficulty_router/routes.csv"))
    parser.add_argument("--stems", type=Path, default=Path("eval/cache/full_stems"))
    parser.add_argument("--lid-root", type=Path, default=Path("eval/runs/code_switch_lid_full"))
    parser.add_argument("--output", type=Path, default=Path("eval/runs/gemini_heavy_candidates"))
    parser.add_argument("--model", default=os.environ.get("GEMINI_AUDIO_MODEL", "gemini-2.5-pro"))
    parser.add_argument("--limit", type=int)
    args = parser.parse_args()
    result = run(
        args.golden.resolve(), args.routes.resolve(), args.stems.resolve(), args.lid_root.resolve(),
        args.output.resolve(), args.model, args.limit,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
