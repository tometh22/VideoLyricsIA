"""Scoreboard estructural: la métrica de consistencia del pipeline.

La diferencia real con Rotor no es calidad promedio sino PEOR CASO: sus
errores son chicos y uniformes (una palabra mal oída); los nuestros eran
estructurales (30 s sin letra, líneas corridas, duplicados) — y un error
estructural grita en pantalla mientras uno de palabra susurra. Este script
mide exactamente esa clase de error sobre jobs reales.

Uso:
    DB=postgresql://... python scripts/qa/structural_scoreboard.py JOB_ID [JOB_ID ...]
    DB=... python scripts/qa/structural_scoreboard.py --mapping batch.json \
        [--logs shortworker.log]

`--mapping`: JSON {job_id: {"tag": nombre, ...}} (lo produce el runner de
batch). `--logs`: dump de logs del worker para sumar las métricas del
guardrail C1 (huecos_con_voz del [COVERAGE], circuit breaker) que no están
en la DB. Sin logs, esas columnas quedan en '?'.

Métricas por job (todas estructurales, cero subjetivas):
  n           líneas totales
  pal.med     palabras por cartel (objetivo Rotor: ~5,8)
  huérf       carteles de 1-2 palabras generados por el pipeline
  vacíos      carteles sin texto (el defecto b3a51559)
  dup         líneas adyacentes idénticas
  voz s/letra segundos de canto sin cartel según VAD del stem (de logs)
  breaker     si el circuit breaker marcó el job

Falla ESTRUCTURAL = vacíos>0, dup>0, o voz-sin-letra >= 10 s. El criterio
de promoción de cualquier cambio del pipeline: 0 fallas estructurales en
el batch. Las palabras mal (nivel Rotor) se toleran; los huecos no.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import statistics as st
import sys


def _parse_logs(path: str) -> dict:
    """job_id -> métricas del guardrail extraídas del log del worker."""
    out: dict = {}
    if not path or not os.path.exists(path):
        return out
    rx = re.compile(r"final=(\d+)%.*huecos_con_voz=(\d+) \(([\d.]+)s\)")
    for ln in open(path, encoding="utf-8", errors="replace"):
        m = re.search(r'job[_=]"?([0-9a-f]{12})', ln)
        if not m:
            continue
        jid = m.group(1)
        d = out.setdefault(jid, {})
        m2 = rx.search(ln)
        if m2:
            d["final"] = int(m2.group(1))
            d["voiced_gap_s"] = float(m2.group(3))
        if "CIRCUIT BREAKER" in ln:
            d["breaker"] = True
    return out


def score(rows: list[tuple], tags: dict, logmap: dict) -> int:
    print(f"{'job/canción':32} {'n':>4} {'pal.med':>7} {'huérf':>5} "
          f"{'vacíos':>6} {'dup':>4} {'voz s/letra':>11} {'breaker':>8}")
    print("-" * 84)
    fallas = 0
    for jid, status, sj in rows:
        tag = tags.get(jid, {}).get("tag", jid[:12])
        if isinstance(sj, str):
            try:
                sj = json.loads(sj)
            except ValueError:
                sj = None
        if not isinstance(sj, list) or not sj:
            print(f"{tag:32} SIN SEGMENTS ({status})")
            fallas += 1
            continue
        pal = [len((s.get("text") or "").split())
               for s in sj if isinstance(s, dict)]
        txts = [(s.get("text") or "").strip().lower()
                for s in sj if isinstance(s, dict)]
        huerf = sum(1 for p in pal if 0 < p < 3)
        vac = sum(1 for t in txts if not t)
        dup = sum(1 for a, b in zip(txts, txts[1:]) if a and a == b)
        L = logmap.get(jid, {})
        vg = L.get("voiced_gap_s")
        br = L.get("breaker", False)
        estructural = vac > 0 or dup > 0 or (vg is not None and vg >= 10)
        fallas += 1 if estructural else 0
        print(f"{tag:32} {len(sj):4d} {st.median(pal):7.1f} {huerf:5d} "
              f"{vac:6d} {dup:4d} {str(vg if vg is not None else '?'):>11} "
              f"{'SÍ' if br else 'no':>8}"
              + ("   << ESTRUCTURAL" if estructural else ""))
    print("-" * 84)
    print(f"fallas estructurales: {fallas}/{len(rows)}")
    return fallas


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("job_ids", nargs="*")
    ap.add_argument("--mapping", help="JSON {job_id: {tag: ...}}")
    ap.add_argument("--logs", help="dump de logs del worker (opcional)")
    args = ap.parse_args()

    tags: dict = {}
    ids = list(args.job_ids)
    if args.mapping:
        tags = json.load(open(args.mapping))
        ids += [j for j in tags if j not in ids]
    if not ids:
        ap.error("pasá job_ids o --mapping")

    import psycopg2
    conn = psycopg2.connect(os.environ["DB"])
    cur = conn.cursor()
    cur.execute("select job_id, status, segments_json from jobs "
                "where job_id = any(%s)", (ids,))
    rows = cur.fetchall()
    fallas = score(rows, tags, _parse_logs(args.logs or ""))
    return 1 if fallas else 0


if __name__ == "__main__":
    sys.exit(main())
