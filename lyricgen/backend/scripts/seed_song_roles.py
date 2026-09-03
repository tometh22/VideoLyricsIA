#!/usr/bin/env python3
"""Siembra `data/song_roles.json` desde los artefactos de investigación.

Reglas de la siembra (en este orden de prioridad, la primera que aplica gana):

1. Cohorte canónica (23) y cualquier fila con ``eval_only`` → ``eval_holdout``.
2. Las canciones del holdout del sprint (CSV del Rol 1) → ``eval_holdout``.
3. ``song_split == "train"`` en el manifiesto del LoRA → ``train``.
4. ``song_split == "validation"`` → ``val``.

Los SHA-256 salen del manifest del batch (que los calcula sobre el WAV real).
Cuando una canción sólo se conoce por job_id, la entrada queda con
``needs_sha256: true``: no se inventan hashes.

Los archivos de entrada son artefactos locales de investigación (no viven en el
repo); por eso las rutas son argumentos. El script es idempotente.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
from pathlib import Path
import re
import sys
import unicodedata

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from song_roles import assign_role, registry_path, summary  # noqa: E402

RESEARCH = Path(
    os.environ.get(
        "GENLY_RESEARCH_CONTEXT",
        "/Users/tomi/conductor/workspaces/VideoLyricsIA-main/riyadh/.context",
    )
)


def norm_title(value: str) -> str:
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = "".join(c for c in text if not unicodedata.combining(c)).lower()
    text = re.sub(r"\s*\(.*?\)\s*", " ", text)
    text = re.sub(r"[^a-z0-9 ]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def load_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--samples", type=Path, default=RESEARCH / "lora-v1-runpod-flac-bundle/samples.jsonl")
    parser.add_argument("--canonical", type=Path, default=RESEARCH / "lora-v1-runpod-flac-bundle/canonical_cohort.json")
    parser.add_argument("--benchmark", type=Path, default=RESEARCH / "benchmarks/umg-gold-v1/manifest.json")
    parser.add_argument("--canary-manifest", type=Path, default=RESEARCH / "universal-batch-manifest-canary-20260902.json")
    parser.add_argument("--holdout-csv", type=Path, default=RESEARCH / "sprint-72h/rol1-bitacora-10-canciones.csv")
    parser.add_argument("--output", type=Path, default=None, help="por defecto backend/data/song_roles.json")
    parser.add_argument("--assigned-at", default="2026-09-03")
    args = parser.parse_args()

    target = args.output or registry_path()

    samples = load_jsonl(args.samples) if args.samples.is_file() else []
    canonical = set(json.load(open(args.canonical)) if args.canonical.is_file() else [])
    cases = (json.load(open(args.benchmark)).get("cases") if args.benchmark.is_file() else []) or []
    canary = (json.load(open(args.canary_manifest)).get("entries") if args.canary_manifest.is_file() else []) or []

    meta_by_job = {c["job_id"]: (c.get("title", ""), c.get("artist", "")) for c in cases if c.get("job_id")}
    title_to_job = {norm_title(t): j for j, (t, _a) in meta_by_job.items()}

    # SHA-256 y job_id de staging desde el manifest del canary (nombre de archivo
    # "Titulo_Artista_ISRC.wav"), enlazados al golden por título normalizado.
    sha_by_title: dict[str, str] = {}
    staging_job_by_title: dict[str, str] = {}
    for entry in canary:
        title = norm_title(str(entry.get("filename", "")).split("_")[0])
        if not title:
            continue
        if entry.get("sha256"):
            sha_by_title[title] = str(entry["sha256"]).lower()
        if entry.get("job_id"):
            staging_job_by_title[title] = str(entry["job_id"])

    holdout_jobs: set[str] = set()
    if args.holdout_csv.is_file():
        with args.holdout_csv.open() as handle:
            holdout_jobs = {r["job_id"] for r in csv.DictReader(handle) if r.get("job_id")}

    split_by_job: dict[str, tuple[str, bool]] = {}
    for row in samples:
        song = str(row.get("song_id") or "")
        if song and song not in split_by_job:
            split_by_job[song] = (str(row.get("song_split") or ""), bool(row.get("eval_only")))

    decided: dict[str, dict] = {}

    def decide(job_id: str, role: str, reason: str) -> None:
        title, artist = meta_by_job.get(job_id, ("", ""))
        key = norm_title(title) or job_id
        sha = sha_by_title.get(key)
        jobs = [{"env": "production_or_staging_gold", "job_id": job_id}]
        staging_job = staging_job_by_title.get(key)
        if staging_job and staging_job != job_id:
            jobs.append({"env": "staging_canary", "job_id": staging_job})
        decided[job_id] = {
            "sha256": sha, "role": role, "reason": reason,
            "title": title, "artist": artist, "job_ids": jobs,
        }

    for job_id in sorted(set(meta_by_job) | set(split_by_job)):
        split, eval_only = split_by_job.get(job_id, ("", False))
        title_key = norm_title(meta_by_job.get(job_id, ("", ""))[0])
        staging_job = staging_job_by_title.get(title_key)
        if job_id in canonical or eval_only:
            decide(job_id, "eval_holdout", "cohorte canónica / eval_only del LoRA v1")
        elif staging_job and staging_job in holdout_jobs:
            decide(job_id, "eval_holdout", "holdout del sprint 2026-09-03 (Rol 1)")
        elif split == "train":
            decide(job_id, "train", "song_split=train en lora-v1-prep")
        elif split == "validation":
            decide(job_id, "val", "song_split=validation en lora-v1-prep")

    for job_id, payload in decided.items():
        assign_role(
            payload["sha256"], payload["role"], payload["reason"],
            title=payload["title"], artist=payload["artist"],
            job_ids=payload["job_ids"], path=target, assigned_at=args.assigned_at,
        )

    result = summary(target)
    print(json.dumps({**result, "registry": str(target)}, ensure_ascii=False))
    unclassified = sorted(set(meta_by_job) - set(decided))
    if unclassified:
        print(f"sin rol asignado ({len(unclassified)}): {unclassified[:12]}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
