#!/usr/bin/env python3
"""Package deterministic synthetic text corruptions behind the UMG train gate."""

from __future__ import annotations

import argparse
import json
import random
import re
from collections import Counter
from pathlib import Path
from typing import Any

from eval.canonical import read_json, write_json


def _phonetic_token(token: str) -> str | None:
    rules = [("b", "v"), ("v", "b"), ("ll", "y"), ("y", "ll"), ("s", "c"), ("c", "s"), ("z", "s")]
    lowered = token.casefold()
    for left, right in rules:
        if left in lowered:
            changed = re.sub(left, right, lowered, count=1)
            return changed if changed != lowered else None
    if lowered.startswith("h") and len(lowered) > 2:
        return lowered[1:]
    return None


def _variants(text: str) -> list[tuple[str, str]]:
    words = text.split()
    variants = []
    if len(words) >= 3:
        middle = len(words) // 2
        variants.append(("word_omission", " ".join(words[:middle] + words[middle + 1:])))
        variants.append(("interjection_insertion", " ".join(words[:1] + ["oh"] + words[1:])))
    for index, token in enumerate(words):
        replacement = _phonetic_token(re.sub(r"[^\wáéíóúüñ]", "", token))
        if replacement:
            variants.append(("phonetic_substitution", " ".join(words[:index] + [replacement] + words[index + 1:])))
            break
    return [(kind, value) for kind, value in variants if value and value != text]


def prepare(golden: Path, output: Path) -> dict[str, Any]:
    manifest = read_json(golden / "manifest.json")
    rows = []
    counts = Counter()
    for item in manifest["cases"]:
        case = golden / item["path"]
        meta = read_json(case / "meta.json")
        audio_path = case / meta["audio"]["filename"]
        lines = read_json(case / "lines.json")
        normalized_counts = Counter(" ".join(str(line.get("text") or "").casefold().split()) for line in lines)
        for line_index, line in enumerate(lines):
            text = str(line.get("text") or "").strip()
            if not text or float(line["end_s"]) <= float(line["start_s"]):
                continue
            base = {
                "song_id": item["song_id"], "line_idx": line_index,
                "audio_path": str(audio_path.resolve()),
                "start_s": float(line["start_s"]), "end_s": float(line["end_s"]),
                "language": (meta.get("language") or {}).get("value"),
                "group": item["song_id"],
                "repeated_line": normalized_counts[" ".join(text.casefold().split())] > 1,
            }
            rows.append({**base, "candidate_text": text, "label_correct": 1, "corruption": "none"})
            counts["none"] += 1
            for corruption, candidate in _variants(text):
                rows.append({**base, "candidate_text": candidate, "label_correct": 0, "corruption": corruption})
                counts[corruption] += 1
    random.Random(20260829).shuffle(rows)
    output.mkdir(parents=True, exist_ok=True)
    with (output / "samples.jsonl").open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    report = {
        "schema_version": 1,
        "songs": len(manifest["cases"]),
        "samples": len(rows),
        "counts": dict(counts),
        "split_contract": "GroupKFold/leave-one-song-out; evaluated song never supplies training rows",
        "execution_gate": {
            "status": "BLOCKED_UMG_TRAINING_AUTHORIZATION",
            "required_environment": "ALLOW_UMG_TRAINING=1",
            "data_egress_performed": False,
        },
    }
    write_json(output / "report.json", report)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--golden", type=Path, default=Path("eval/golden"))
    parser.add_argument("--output", type=Path, default=Path("eval/runs/t7_corruptions"))
    args = parser.parse_args()
    print(json.dumps(prepare(args.golden.resolve(), args.output.resolve()), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
