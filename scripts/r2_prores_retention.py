#!/usr/bin/env python3
"""Retención de ProRes en R2 — reporta y (opcionalmente) libera storage.

## Por qué

Medición del bucket de producción (17-ago-2026):

    TOTAL                      2.889,8 GB   $43,35/mes
      ProRes (.mov)            1.887,0 GB   $28,31/mes   <- 65% del costo
      versionados (.vN)          909,0 GB   $13,63/mes
      MP4                         74,2 GB    $1,11/mes
      audio de entrada            19,3 GB    $0,29/mes

    umg_master.mov   311 obj   prom 5,49 GB c/u
    umg_short.mov    310 obj   prom 0,58 GB c/u   -> ~6,07 GB por video

El bucket crece ~750 GB/mes al volumen actual (~60 entregas/mes) = ~12,5 GB por
video entregado. A 400 videos/mes (contrato UMG) son ~5 TB/mes, +$75/mes CADA
mes: a 12 meses el storage solo sería ~$900/mes. Es la única línea de costo que
únicamente sube, y hoy el bucket no tiene ninguna regla de expiración.

## Qué se puede liberar, y por qué es seguro

1. **ProRes versionados** (`umg_master.mov.v3`, `umg_short.mov.v1`, ...):
   860 GB / $12,91 mes. Son copias SUPERADAS: cada edición sube una versión
   nueva y la vieja queda para siempre. Nadie las sirve — el portal entrega
   siempre la vigente. Es lo más seguro de borrar.

2. **ProRes vigentes viejos** (`umg_master.mov` con >N días): 1.238 GB /
   $18,57 mes a 30 días. Seguro SÓLO si el MP4 hermano existe, porque
   `prores.ensure_prores_exists()` regenera el .mov desde el MP4 on-demand
   cuando alguien lo descarga (cuesta 60-120 s de ffmpeg la primera vez, no
   se pierde nada). Medido: **304 de 312 (97,4%) tienen su MP4 fuente**; los
   8 restantes NO son regenerables y este script los excluye SIEMPRE.

Nunca toca: MP4/short/thumbnail (son la fuente de regeneración y pesan poco),
`inputs/` (audio original del cliente), ni ningún .mov sin MP4 hermano.

## Uso

    # sólo reporta (default, no borra nada)
    python scripts/r2_prores_retention.py

    # borra ProRes versionados (superados) — lo más seguro
    python scripts/r2_prores_retention.py --versions --apply

    # además, ProRes vigentes de más de 90 días QUE SEAN REGENERABLES
    python scripts/r2_prores_retention.py --versions --current --age-days 90 --apply

Requiere R2_ENDPOINT_URL / R2_ACCESS_KEY_ID / R2_SECRET_ACCESS_KEY / R2_BUCKET.
"""
from __future__ import annotations

import argparse
import datetime as dt
import os
import re
import sys

GB = 1024 ** 3
R2_USD_PER_GB_MONTH = 0.015

# Un .mov versionado termina en `.vN` (lo escribe el pipeline al re-renderizar
# una edición). El vigente no lleva sufijo.
VERSIONED_MOV = re.compile(r"\.mov\.v\d+$", re.IGNORECASE)
# El MP4 desde el cual `ensure_prores_exists` puede regenerar cada máster.
PRORES_SOURCES = {
    "umg_master.mov": "lyric_video.mp4",
    "umg_short.mov": "short.mp4",
}


def _client():
    try:
        import boto3
    except ImportError:
        sys.exit("Falta boto3: pip install boto3")
    missing = [v for v in ("R2_ENDPOINT_URL", "R2_ACCESS_KEY_ID",
                           "R2_SECRET_ACCESS_KEY", "R2_BUCKET")
               if not os.environ.get(v)]
    if missing:
        sys.exit(f"Faltan variables de entorno: {', '.join(missing)}")
    return boto3.client(
        "s3",
        endpoint_url=os.environ["R2_ENDPOINT_URL"],
        aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
        region_name="auto",
    ), os.environ["R2_BUCKET"]


def _scan(s3, bucket):
    """Un solo listado completo: devuelve (objetos, set de claves)."""
    objects, keys = [], set()
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket):
        for o in page.get("Contents", []):
            objects.append((o["Key"], o["Size"], o["LastModified"]))
            keys.add(o["Key"])
    return objects, keys


def _is_regenerable(key: str, keys: set) -> bool:
    """True si el MP4 desde el que se regenera este .mov sigue en el bucket.

    Sin el MP4, expirar el .mov PIERDE el máster para siempre — por eso este
    chequeo es la guarda dura del script, no una heurística.
    """
    base = key.split("/")[-1]
    stem = base.split(".v")[0] if ".mov.v" in base.lower() else base
    source = PRORES_SOURCES.get(stem)
    if not source:
        return False
    return f"{key.rsplit('/', 1)[0]}/{source}" in keys


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--versions", action="store_true",
                    help="incluir ProRes versionados (.vN) — copias superadas")
    ap.add_argument("--current", action="store_true",
                    help="incluir ProRes vigentes más viejos que --age-days")
    ap.add_argument("--age-days", type=int, default=90,
                    help="antigüedad mínima para --current (default: 90)")
    ap.add_argument("--apply", action="store_true",
                    help="BORRA de verdad. Sin este flag sólo reporta.")
    args = ap.parse_args()

    s3, bucket = _client()
    now = dt.datetime.now(dt.timezone.utc)
    objects, keys = _scan(s3, bucket)

    total = sum(sz for _, sz, _ in objects)
    movs = [(k, sz, lm) for k, sz, lm in objects if k.lower().endswith(".mov")
            or VERSIONED_MOV.search(k)]
    mov_total = sum(sz for _, sz, _ in movs)

    print(f"Bucket {bucket}: {len(objects):,} objetos · {total / GB:,.1f} GB "
          f"· ${total / GB * R2_USD_PER_GB_MONTH:,.2f}/mes")
    print(f"  ProRes (.mov y .mov.vN): {len(movs):,} obj · {mov_total / GB:,.1f} GB "
          f"· ${mov_total / GB * R2_USD_PER_GB_MONTH:,.2f}/mes "
          f"({mov_total / max(total, 1) * 100:.0f}% del storage)")

    doomed, skipped_not_regenerable = [], []
    for key, size, last_modified in movs:
        versioned = bool(VERSIONED_MOV.search(key))
        age_days = (now - last_modified).days
        if versioned:
            if not args.versions:
                continue
        elif args.current and age_days >= args.age_days:
            pass
        else:
            continue
        if not _is_regenerable(key, keys):
            skipped_not_regenerable.append((key, size))
            continue
        doomed.append((key, size, age_days, versioned))

    freed = sum(sz for _, sz, _, _ in doomed)
    n_versioned = sum(1 for *_, v in doomed if v)
    print(f"\nSeleccionados para liberar: {len(doomed):,} objetos · "
          f"{freed / GB:,.1f} GB · ${freed / GB * R2_USD_PER_GB_MONTH:,.2f}/mes"
          f"  (versionados: {n_versioned:,} · vigentes: {len(doomed) - n_versioned:,})")
    if skipped_not_regenerable:
        omitted = sum(sz for _, sz in skipped_not_regenerable)
        print(f"PRESERVADOS por no ser regenerables (sin MP4 fuente): "
              f"{len(skipped_not_regenerable):,} obj · {omitted / GB:,.1f} GB")
        for key, _ in skipped_not_regenerable[:5]:
            print(f"    {key}")

    if not doomed:
        print("\nNada que hacer. (Probá --versions y/o --current.)")
        return 0
    if not args.apply:
        print("\nDRY-RUN: no se borró nada. Repetí con --apply para ejecutar.")
        for key, size, age, versioned in doomed[:10]:
            tag = "versionado" if versioned else f"{age}d"
            print(f"    [{tag}] {key} ({size / GB:.2f} GB)")
        if len(doomed) > 10:
            print(f"    … y {len(doomed) - 10:,} más")
        return 0

    deleted = 0
    for i in range(0, len(doomed), 1000):
        batch = [{"Key": k} for k, *_ in doomed[i:i + 1000]]
        resp = s3.delete_objects(Bucket=bucket, Delete={"Objects": batch,
                                                       "Quiet": True})
        deleted += len(batch) - len(resp.get("Errors", []) or [])
        for err in (resp.get("Errors") or [])[:5]:
            print(f"  ERROR {err.get('Key')}: {err.get('Message')}")
    print(f"\nBorrados {deleted:,} objetos · liberados {freed / GB:,.1f} GB "
          f"· ahorro ~${freed / GB * R2_USD_PER_GB_MONTH:,.2f}/mes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
