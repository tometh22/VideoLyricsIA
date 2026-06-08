#!/usr/bin/env python3
"""Ingest a Rotor ground-truth (exported from the Rotor editor's console) into
the benchmark dataset so `score_benchmark.py` can score our output against it.

Rotor's export is a JSON list of objects shaped:
    {"s": "39.64", "t": "Tengo una mala noticia[39.64, 41.83]"}
where `t` is the line text with a trailing "[start, end]" appended. We parse
that into the bare `[{start, end, text}]` (seconds) that score_benchmark expects.

Usage:
    cd lyricgen/backend
    python scripts/ingest_rotor_gt.py \
        --raw benchmark/rotor_gt/rotor_nada_fue.json \
        --slug rotor_nada_fue \
        --audio "/Users/tomi/Downloads/agus_wavs/Nada Fue Un Error (En Vivo).wav" \
        --artist Coti --title "Nada Fue Un Error (En Vivo)"
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
from pathlib import Path

HERE = Path(__file__).resolve().parent
DATASET = HERE.parent / "benchmark" / "dataset"

# "texto ... [12.34, 56.78]"  (tolerant: spaces, ints or floats)
_BRACKET = re.compile(r"^(?P<text>.*?)\s*\[\s*(?P<start>\d+(?:\.\d+)?)\s*,\s*(?P<end>\d+(?:\.\d+)?)\s*\]\s*$", re.DOTALL)


def parse_rotor(raw: list[dict]) -> list[dict]:
    out: list[dict] = []
    for item in raw:
        t = (item.get("t") or "").strip()
        m = _BRACKET.match(t)
        if m:
            text = m.group("text").strip()
            start = float(m.group("start"))
            end = float(m.group("end"))
        else:
            # No bracket — fall back to "s" for start, no end.
            text = t
            start = float(item.get("s", 0.0))
            end = start
        if text:
            out.append({"start": start, "end": end, "text": text})
    out.sort(key=lambda s: s["start"])
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--raw", required=True, type=Path, help="Rotor export JSON ([{s,t}, ...])")
    ap.add_argument("--slug", required=True, help="dataset folder name, e.g. rotor_nada_fue")
    ap.add_argument("--audio", required=True, type=Path, help="path to the source audio")
    ap.add_argument("--artist", default="")
    ap.add_argument("--title", default="")
    args = ap.parse_args()

    raw = json.loads(args.raw.read_text())
    gt = parse_rotor(raw)
    if len(gt) < 2:
        raise SystemExit(f"[ERR] parsed only {len(gt)} lines from {args.raw}")

    dest = DATASET / args.slug
    dest.mkdir(parents=True, exist_ok=True)
    (dest / "ground_truth.json").write_text(json.dumps(gt, ensure_ascii=False, indent=2))

    ext = args.audio.suffix.lower() or ".wav"
    shutil.copy2(args.audio, dest / f"audio{ext}")

    (dest / "metadata.json").write_text(json.dumps({
        "artist": args.artist,
        "song_title": args.title,
        "source": "rotor_ground_truth",
        "gt_lines": len(gt),
        "gt_start_s": gt[0]["start"],
        "gt_end_s": gt[-1]["end"],
    }, ensure_ascii=False, indent=2))

    print(f"✓ {args.slug}: {len(gt)} lines, GT {gt[0]['start']:.2f}s–{gt[-1]['end']:.2f}s, audio → {dest}/audio{ext}")


if __name__ == "__main__":
    main()
