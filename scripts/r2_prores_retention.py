#!/usr/bin/env python3
"""Reporte de retención de ProRes en R2 — SOLO LECTURA, no borra nada.

## Por qué existe

Medición del bucket de producción (17-ago-2026): 2.896 GB / $43,44 mes, de los
cuales ProRes (.mov y .mov.vN) son **2.754 GB = 95% del storage**. `umg_master.mov`
promedia 5,49 GB y `umg_short.mov` 0,58 GB → ~6,07 GB por video, y el doble si
hubo una edición (la versión vieja queda para siempre).

El bucket **no tiene ninguna regla de expiración** y crece ~750 GB/mes al volumen
actual (~60 entregas/mes) = ~12,5 GB por video entregado. A 400 videos/mes son
~5 TB/mes, +$75/mes CADA mes: a 12 meses el storage solo sería ~$900/mes. Es la
única línea de costo que únicamente sube, y hoy nadie la está mirando.

## Por qué NO borra

Una versión anterior de este script borraba. Una revisión adversarial encontró
que no era seguro, y la capacidad se removió a propósito:

* Los ProRes **vigentes** los sirve el portal de UMG firmando la key directo, sin
  readiness ni prewarm, y el export a Drive hace rclone contra ella.
  `ensure_prores_exists()` sólo cubre `GET /download/{id}/umg_master`, así que
  borrarlos deja links rotos en el portal. Peor: el tamaño queda cacheado en
  Redis 30 días, con lo cual el portal sigue mostrando `available: true` con un
  link que devuelve un error de R2, y **no se auto-cura nunca**.
* Los ProRes **versionados** (`.vN`) son el rollback manual documentado en
  `Job.previous_versions` ("bajar la key .vN de R2 a mano" tras un re-sync malo).
  Ningún código los lee automáticamente, pero borrarlos elimina esa red de
  seguridad: es una decisión de producto, no una limpieza.
* La guarda "es regenerable si existe el MP4 hermano" sólo mira el bucket.
  `ensure_prores_exists` además exige la fila del Job con `umg_spec` y
  `s3_keys['video']`; un objeto huérfano en R2 (clase de bug ya ocurrida) se
  vería regenerable sin serlo.

Una limpieza segura necesita: cruzar contra la tabla `deliveries`, invalidar
`dlsize:<key>` en Redis, darle al portal un fallback que encole `prewarm`, y
verificar la fila del Job en Postgres (abortando si la DB no se puede leer, como
ya hace `storage._active_input_keys()`). Nada de eso existe hoy.

## Uso

    python scripts/r2_prores_retention.py            # reporte
    python scripts/r2_prores_retention.py --age-days 30

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
    ap.add_argument("--age-days", type=int, default=90,
                    help="umbral de antigüedad para el desglose (default: 90)")
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

    now_versioned, now_current_old = [], []
    for key, size, last_modified in movs:
        age_days = (now - last_modified).days
        if VERSIONED_MOV.search(key):
            now_versioned.append((key, size, age_days))
        elif age_days >= args.age_days:
            now_current_old.append((key, size, age_days))

    def _line(label, items):
        total_bytes = sum(sz for _, sz, _ in items)
        regenerables = sum(1 for k, _, _ in items if _is_regenerable(k, keys))
        print(f"  {label:34} {len(items):>5,} obj  {total_bytes / GB:>8,.1f} GB  "
              f"${total_bytes / GB * R2_USD_PER_GB_MONTH:>7,.2f}/mes  "
              f"(regenerables desde su MP4: {regenerables}/{len(items)})")

    print("\nDesglose de lo que HOY podría considerarse liberable:")
    _line("versionados (.vN, superados)", now_versioned)
    _line(f"vigentes con más de {args.age_days} días", now_current_old)

    print(
        "\nEste script NO borra nada — es solo-reporte a propósito.\n"
        "Borrar ProRes requiere infraestructura que hoy no existe:\n"
        "  * Los VIGENTES los sirve el portal de UMG firmando la key directo\n"
        "    (sin readiness ni prewarm) y el export a Drive hace rclone contra\n"
        "    ella. `ensure_prores_exists()` solo cubre GET /download/{id}/...,\n"
        "    así que borrarlos deja links rotos; peor: el tamaño queda cacheado\n"
        "    en Redis 30 días, el portal sigue diciendo `available: true` y NO\n"
        "    se auto-cura. Haría falta: cruzar contra `deliveries`, invalidar\n"
        "    `dlsize:<key>` y darle al portal un fallback que encole prewarm.\n"
        "  * Los VERSIONADOS (.vN) son el rollback manual documentado en\n"
        "    `Job.previous_versions` (\"bajar la key .vN de R2 a mano\" tras un\n"
        "    re-sync malo). Nadie los lee automáticamente, pero borrarlos\n"
        "    elimina esa red de seguridad: es una decisión de producto.\n"
        "  * La guarda `_is_regenerable` sólo mira el bucket. `ensure_prores_\n"
        "    exists` además exige la fila del Job con `umg_spec` y\n"
        "    `s3_keys['video']`; un objeto huérfano en R2 (clase de bug ya\n"
        "    ocurrida) se vería regenerable y no lo es.\n"
        "\nEl valor accionable de este reporte es la TRAYECTORIA: ver arriba\n"
        "cuánto pesa hoy y contrastarlo mes a mes."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
