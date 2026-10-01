#!/usr/bin/env python3
"""Imprime el rol de cada canción y falla si alguna es de entrenamiento.

Pensado como gate barato para cualquier harness o corrida de evaluación:

    python3.11 scripts/check_song_roles.py --ids 6bd2142c0f6d,02bfe792438b
    python3.11 scripts/check_song_roles.py --manifest .context/universal-batch-manifest.json

Sale con 1 si alguna canción tiene rol ``train`` (no se puede evaluar con ella),
con 2 si alguna no está en el registro y se pidió ``--strict-unknown``.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from song_roles import role_for, role_split  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--ids", default="", help="job_ids o sha256 separados por coma")
    parser.add_argument("--manifest", type=Path, help="manifest de universal_batch (usa sha256/job_id)")
    parser.add_argument("--strict-unknown", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    identifiers = [value.strip() for value in args.ids.split(",") if value.strip()]
    if args.manifest and args.manifest.is_file():
        for entry in json.loads(args.manifest.read_text(encoding="utf-8")).get("entries") or []:
            identifiers.append(str(entry.get("sha256") or entry.get("job_id") or ""))
    identifiers = [value for value in identifiers if value]
    if not identifiers:
        parser.error("nada para chequear: pasá --ids o --manifest")

    rows = [{"id": value, "role": role_for(value) or "unknown"} for value in identifiers]
    summary = role_split(identifiers)
    if args.json:
        print(json.dumps({"rows": rows, "role_split": summary}, ensure_ascii=False, indent=1))
    else:
        for row in rows:
            print(f"{row['role']:13} {row['id']}")
        print(f"-- {summary}")

    if summary.get("train"):
        print(f"FALLA: {summary['train']} canción(es) de entrenamiento no pueden evaluarse", file=sys.stderr)
        return 1
    if args.strict_unknown and summary.get("unknown"):
        print(f"FALLA: {summary['unknown']} canción(es) sin rol asignado", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
