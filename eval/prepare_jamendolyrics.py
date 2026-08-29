#!/usr/bin/env python3
"""Prepare a license-filtered JamendoLyrics research manifest for LoRA."""

from __future__ import annotations

import argparse
import csv
import json
import posixpath
from pathlib import Path

from huggingface_hub import hf_hub_download

from eval.prepare_lora import _chunks


REPO = "jamendolyrics/jamendolyrics"
# Exclude NonCommercial and NoDerivatives tracks from this engineering smoke.
ALLOW_LICENSES = {"CC BY", "CC BY-SA", "BY", "BY-SA"}


def _download_following_link(cache: Path, repository_path: str) -> Path:
    local = Path(hf_hub_download(
        REPO, repository_path, repo_type="dataset", local_dir=cache,
    ))
    if local.stat().st_size <= 256:
        value = local.read_text(encoding="utf-8").strip()
        if value.startswith("../"):
            target = posixpath.normpath(posixpath.join(posixpath.dirname(repository_path), value))
            return Path(hf_hub_download(
                REPO, target, repo_type="dataset", local_dir=cache,
            ))
    return local


def run(cache: Path, output: Path, language: str | None, maximum_songs: int) -> dict:
    metadata_path = Path(hf_hub_download(
        REPO, "JamendoLyrics.csv", repo_type="dataset", local_dir=cache,
    ))
    metadata = list(csv.DictReader(metadata_path.open(encoding="utf-8")))
    selected = [
        row for row in metadata
        if str(row.get("LicenseType") or "").strip() in ALLOW_LICENSES
        and (language is None or str(row.get("Language") or "").casefold() == language.casefold())
    ][:maximum_songs]
    samples = []
    attribution = []
    for row in selected:
        filename = row["Filepath"]
        audio_path = _download_following_link(cache, f"mp3/{filename}")
        annotation_name = Path(filename).with_suffix(".csv").name
        annotation_path = Path(hf_hub_download(
            REPO, f"annotations/lines/{annotation_name}", repo_type="dataset", local_dir=cache,
        ))
        lines = [
            {
                "start_s": float(line["start_time"]),
                "end_s": float(line["end_time"]),
                "text": line["lyrics_line"],
            }
            for line in csv.DictReader(annotation_path.open(encoding="utf-8"))
        ]
        song_id = Path(filename).stem
        for index, chunk in enumerate(_chunks(lines)):
            samples.append({
                "sample_id": f"jamendo-{song_id}-{index:03d}",
                "song_id": f"jamendo-{song_id}",
                "audio_path": str(audio_path.resolve()),
                "language": row["Language"],
                "license": str(row["LicenseType"]).strip(),
                "source_url": row["URL"],
                **chunk,
            })
        attribution.append({
            "song_id": f"jamendo-{song_id}", "artist": row["Artist"],
            "title": row["Title"], "license": str(row["LicenseType"]).strip(),
            "source_url": row["URL"],
        })
    output.mkdir(parents=True, exist_ok=True)
    with (output / "samples.jsonl").open("w", encoding="utf-8") as handle:
        for sample in samples:
            handle.write(json.dumps(sample, ensure_ascii=False) + "\n")
    (output / "ATTRIBUTION.json").write_text(
        json.dumps(attribution, ensure_ascii=False, indent=2), encoding="utf-8",
    )
    report = {
        "schema_version": 1,
        "repository": REPO,
        "songs": len(selected),
        "samples": len(samples),
        "license_allowlist": sorted(ALLOW_LICENSES),
        "excluded_license_markers": ["NC", "ND"],
        "language_filter": language,
        "data_egress": "download_only; no client data uploaded",
    }
    (output / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache", type=Path, default=Path("eval/cache/jamendolyrics"))
    parser.add_argument("--output", type=Path, default=Path("eval/runs/lora_jamendo_prep"))
    parser.add_argument("--language", default="Spanish")
    parser.add_argument("--maximum-songs", type=int, default=3)
    args = parser.parse_args()
    print(json.dumps(run(args.cache.resolve(), args.output.resolve(), args.language, args.maximum_songs), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
