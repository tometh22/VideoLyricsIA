#!/usr/bin/env python3
"""Cuánto costaría (y cuánto ahorraría) poner reglas de lifecycle en R2.

Contexto
--------
El bucket de R2 no tiene reglas de expiración: todo lo que se sube queda para
siempre. Medido en ago-2026: **2.692 GB**, de los cuales **1.772 GB (66%) son
masters ProRes** de ~3 GB cada uno. R2 cobra por GB-mes promedio, así que el
costo del video #1 se vuelve a pagar todos los meses.

Es la única línea de la factura que **sólo sube**: a 400 videos/mes suma
~1.000 GB nuevos por mes, o sea ~+$15/mes cada mes, acumulativo. Al quinto mes
del contrato con Universal la línea de R2 sola sería ~8x lo que es hoy.

Por qué esto es un reporte y no un script que borra
---------------------------------------------------
Borrar masters es **irreversible** y el cliente los descarga de verdad (203
descargas de `umg_master` en un mes). Este script NO borra nada: mide qué
pasaría con cada ventana de retención para que la decisión se tome con
números. Aplicar la regla es un paso manual en el panel de Cloudflare.

Dato clave para elegir la ventana: el ProRes se **regenera solo** desde el MP4
(`prores.ensure_prores_exists`). Expirarlo no pierde el entregable — el costo
de equivocarse es un re-transcode de 60-300 s en la próxima descarga, no un
archivo perdido.

Uso
---
    export R2_ENDPOINT_URL=... R2_ACCESS_KEY_ID=... R2_SECRET_ACCESS_KEY=...
    export R2_BUCKET=genly-deliverables
    python scripts/r2_lifecycle_report.py
"""

import os
import re
import sys
from collections import defaultdict
from datetime import datetime, timezone

import boto3

GB = 1024 ** 3
USD_PER_GB_MONTH = float(os.environ.get("R2_USD_PER_GB_MONTH", "0.015"))
FREE_GB = float(os.environ.get("R2_FREE_GB", "10"))

# Ventanas a evaluar. 0 = "borrar apenas se sube" (no tiene sentido, sirve de
# cota inferior teórica).
WINDOWS_DAYS = (7, 14, 30, 60, 90, 180)

# Nombres determinísticos que produce el transcode ProRes. La extensión sola
# no alcanza: el operador también puede subir un MOV como fondo y ese input no
# es regenerable desde el MP4 final.
REGENERABLE_PRORES_NAMES = frozenset({"umg_master.mov", "umg_short.mov"})


# Los snapshots de versiones anteriores se guardan como `{key}.v1`, `.v2`…
# (pipeline._snapshot_previous_deliverables). Clasificar sólo por la extensión
# final los mandaba a "otros" y subestimaba ProRes en 808 GB — un 45% del
# total. Son además los MEJORES candidatos a expirar: nadie descarga la v1 de
# un video que ya se re-renderizó.
_VERSION_SUFFIX = re.compile(r"\.v\d+$")


def _base_ext(key: str) -> str:
    """Extensión real, ignorando el sufijo de versión `.vN`."""
    low = key.lower()
    low = _VERSION_SUFFIX.sub("", low)
    _, _, ext = low.rpartition(".")
    return ext


def _classify(key: str) -> str:
    low = key.lower()
    versionado = bool(_VERSION_SUFFIX.search(low))
    base_key = _VERSION_SUFFIX.sub("", low)
    filename = base_key.rsplit("/", 1)[-1]
    ext = _base_ext(low)
    if filename in REGENERABLE_PRORES_NAMES:
        return ("master ProRes VERSIÓN VIEJA" if versionado
                else "master ProRes (regenerable)")
    if "/inputs/" in low or low.startswith("inputs/"):
        return "input original (FUENTE — no expirar)"
    if ext == "mov":
        return "MOV no reconocido (NO expirar)"
    if ext == "mp4":
        return ("MP4 versión vieja" if versionado
                else "video MP4 (FUENTE — no expirar)")
    if ext in ("jpg", "jpeg", "png"):
        return "miniatura"
    return "otros"


def main() -> int:
    for var in ("R2_ENDPOINT_URL", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY",
                "R2_BUCKET"):
        if not os.environ.get(var):
            print(f"ERROR: falta {var}", file=sys.stderr)
            return 2

    s3 = boto3.client(
        "s3", endpoint_url=os.environ["R2_ENDPOINT_URL"],
        aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
        region_name="auto",
    )
    bucket = os.environ["R2_BUCKET"]
    now = datetime.now(timezone.utc)

    by_kind = defaultdict(lambda: {"n": 0, "bytes": 0})
    ages: list[tuple[int, int, str]] = []   # (edad_dias, bytes, kind)
    total_bytes = 0

    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket):
        for obj in page.get("Contents", []):
            size, key = obj["Size"], obj["Key"]
            kind = _classify(key)
            by_kind[kind]["n"] += 1
            by_kind[kind]["bytes"] += size
            total_bytes += size
            age = (now - obj["LastModified"]).days
            ages.append((age, size, kind))

    print("=" * 70)
    print(f"BUCKET {bucket} — {total_bytes / GB:,.0f} GB en {len(ages):,} objetos")
    print("=" * 70)
    print(f"{'tipo':<34}{'objetos':>9}{'GB':>9}{'$/mes':>9}")
    print("-" * 70)
    for kind, v in sorted(by_kind.items(), key=lambda kv: -kv[1]["bytes"]):
        g = v["bytes"] / GB
        print(f"{kind:<34}{v['n']:>9,}{g:>9,.0f}{g * USD_PER_GB_MONTH:>9,.2f}")
    print("-" * 70)
    g_tot = total_bytes / GB
    print(f"{'TOTAL':<34}{len(ages):>9,}{g_tot:>9,.0f}"
          f"{max(0, g_tot - FREE_GB) * USD_PER_GB_MONTH:>9,.2f}")

    print()
    print("=" * 70)
    print("SI SE EXPIRARAN LOS MASTERS ProRes DESPUÉS DE N DÍAS")
    print("=" * 70)
    print(f"{'retención':>10}{'objetos':>10}{'GB liberados':>14}"
          f"{'ahorro/mes':>12}{'ahorro/año':>12}")
    print("-" * 70)
    for days in WINDOWS_DAYS:
        # `startswith("master ProRes")` cubre las dos categorías: el master
        # vigente y las versiones viejas `.vN`.
        freed = sum(sz for age, sz, kind in ages
                    if age > days and kind.startswith("master ProRes"))
        n = sum(1 for age, sz, kind in ages
                if age > days and kind.startswith("master ProRes"))
        g = freed / GB
        saving = g * USD_PER_GB_MONTH
        print(f"{days:>8}d{n:>10,}{g:>14,.0f}{saving:>12,.2f}{saving * 12:>12,.2f}")

    print()
    print("Notas para decidir la ventana:")
    print("  · El ProRes se regenera del MP4 (prores.ensure_prores_exists).")
    print("    Expirarlo NO pierde el entregable: la próxima descarga paga un")
    print("    re-transcode de 60-300 s y el cliente ve un 202 + Retry-After.")
    print("  · El MP4 es la FUENTE de esa regeneración — nunca expirarlo.")
    print("  · Medido: 203 descargas de umg_master en un mes sobre 68 entregas")
    print("    (~3 por entrega), casi todas en los días posteriores a publicar.")
    print("  · Este script no borra nada. La regla se aplica en el panel de")
    print("    Cloudflare (R2 → bucket → Settings → Object lifecycle rules).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
