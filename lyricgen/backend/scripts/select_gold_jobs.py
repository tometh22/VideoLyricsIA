#!/usr/bin/env python3
"""Selecciona jobs "gold" para el benchmark desde las correcciones REALES
de los operadores, y calcula el error baseline del pipeline sin re-correr nada.

Contexto (análisis de prod 2026-07-03): los operadores de UMG registraron
miles de correcciones de segmentos (audit_log action="lyrics.segments_diff",
93% de timing, mediana 1,4 s). Cada job corregido y aprobado es ground truth:
`segments_json` = lo que el humano consideró shippable. Este script:

  1. `--write-list` — encuentra esos jobs y llena scripts/benchmark_jobs.txt
     (el input de build_benchmark_dataset.py), en vez de curarlo a mano.
     Evita el sesgo de evaluar con 1-2 canciones elegidas a dedo.

  2. `--baseline` — reconstruye el output ORIGINAL de la máquina rebobinando
     los diffs (cada audit guarda prev_start/prev_end/prev_text por segmento;
     aplicándolos de más nuevo a más viejo se recupera el estado pre-humano)
     y reporta machine-vs-gold por línea: p50/p90 de |Δstart|, % de líneas
     dentro de 0.3 s / 1.0 s, % con texto cambiado. Es el número que
     cualquier mejora (p.ej. CTC_ALIGN_ENABLED) tiene que ganar.

Solo hace SELECTs. Usage:
    cd lyricgen/backend
    export DATABASE_URL='postgresql://...'   # prod (read-only recomendado)
    python scripts/select_gold_jobs.py --write-list
    python scripts/select_gold_jobs.py --baseline
    python scripts/select_gold_jobs.py --baseline --tenant-like 'universal%' --json out.json

Limitaciones del rewind (documentadas, no bloqueantes):
  - El audit trunca a 20 cambios por save (`truncated=true`, ~2% de los
    saves): esos cambios extra no se pueden rebobinar → el baseline
    SUBESTIMA levemente el error real. Se reporta cuántos jobs tienen saves
    truncados.
  - prev_text/new_text vienen capados a 120 chars (solo afecta el flag de
    texto-cambiado en líneas larguísimas, no el timing).
  - Los ids de segmento son posicionales ("idx_N"); en prod no hay reorders
    registrados (0 de 6.343 audits al 2026-07-03), y si aparecieran el
    script los reporta y salta ese job.
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
BACKEND = HERE.parent
sys.path.insert(0, str(BACKEND))

DEFAULT_LIST = HERE / "benchmark_jobs.txt"

# Umbrales de "línea bien sincronizada" para el reporte. 0.3 s es
# imperceptible en karaoke-style; 1.0 s ya se nota pero puede ser tolerable
# en líneas largas.
_TIGHT_S = 0.3
_LOOSE_S = 1.0


# ---------------------------------------------------------------------------
# Rewind de diffs → output original de la máquina
# ---------------------------------------------------------------------------

def rewind_segments(final_segments: list[dict], audits: list[dict]) -> tuple[list[dict], dict]:
    """Reconstruye el estado pre-humano aplicando los diffs en reversa.

    final_segments: job.segments_json (estado aprobado por el operador).
    audits: lista de payloads de lyrics.segments_diff en orden CRONOLÓGICO
            (viejo→nuevo). Se aplican de nuevo→viejo seteando los valores
            prev_* de cada entrada `changed`.

    Returns (machine_segments, info) donde info trae flags de calidad del
    rewind: {"truncated_saves": int, "reorders": int, "out_of_range": int}.
    """
    machine = [dict(s) for s in final_segments]
    info = {"truncated_saves": 0, "reorders": 0, "out_of_range": 0}
    for audit in reversed(audits):
        if audit.get("truncated"):
            info["truncated_saves"] += 1
        if audit.get("reorder"):
            info["reorders"] += len(audit["reorder"])
        for c in audit.get("changed", []):
            raw_id = str(c.get("id", ""))
            if not raw_id.startswith("idx_"):
                info["out_of_range"] += 1
                continue
            try:
                idx = int(raw_id[len("idx_"):])
            except ValueError:
                info["out_of_range"] += 1
                continue
            if not 0 <= idx < len(machine):
                info["out_of_range"] += 1
                continue
            seg = machine[idx]
            if c.get("prev_start") is not None:
                seg["start"] = c["prev_start"]
            if c.get("prev_end") is not None:
                seg["end"] = c["prev_end"]
            if c.get("prev_text") is not None:
                seg["text"] = c["prev_text"]
    return machine, info


def score_machine_vs_gold(machine: list[dict], gold: list[dict]) -> dict:
    """Error por línea entre el output de la máquina y el gold aprobado.

    La identidad es posicional (mismo índice = misma línea; en prod no hay
    reorders). Las líneas que el humano no tocó cuentan como error 0 — el
    operador las aceptó tal cual, que es la definición operativa de
    "correcta".
    """
    n = min(len(machine), len(gold))
    d_starts, d_ends = [], []
    text_changed = 0
    touched = 0
    for i in range(n):
        m, g = machine[i], gold[i]
        ds = abs(float(m.get("start", 0.0)) - float(g.get("start", 0.0)))
        de = abs(float(m.get("end", 0.0)) - float(g.get("end", 0.0)))
        if ds > 1e-9 or de > 1e-9 or (m.get("text") or "") != (g.get("text") or ""):
            touched += 1
        d_starts.append(ds)
        d_ends.append(de)
        # Comparación floja: el audit capa los textos a 120 chars.
        mt = (m.get("text") or "").strip()[:120]
        gt = (g.get("text") or "").strip()[:120]
        if mt != gt:
            text_changed += 1

    def _pct(vals: list[float], q: float) -> float:
        if not vals:
            return 0.0
        vals = sorted(vals)
        k = max(0, min(len(vals) - 1, int(round(q * (len(vals) - 1)))))
        return vals[k]

    return {
        "lines": n,
        "lines_touched": touched,
        "start_p50": round(statistics.median(d_starts), 3) if d_starts else 0.0,
        "start_p90": round(_pct(d_starts, 0.90), 3),
        "start_max": round(max(d_starts), 3) if d_starts else 0.0,
        "end_p50": round(statistics.median(d_ends), 3) if d_ends else 0.0,
        "pct_start_within_tight": round(
            100.0 * sum(1 for d in d_starts if d <= _TIGHT_S) / n, 1) if n else 0.0,
        "pct_start_within_loose": round(
            100.0 * sum(1 for d in d_starts if d <= _LOOSE_S) / n, 1) if n else 0.0,
        "pct_text_changed": round(100.0 * text_changed / n, 1) if n else 0.0,
    }


# ---------------------------------------------------------------------------
# DB
# ---------------------------------------------------------------------------

def _fetch_gold_jobs(db, tenant_like: str, min_diffs: int) -> list[dict]:
    """Jobs done + segments_json + al menos min_diffs audits de corrección.
    Devuelve [{job_id, tenant_id, artist, song_title, input_r2_key,
               segments_json, audits: [detail,...]}] orden cronológico."""
    from sqlalchemy import text as _sql
    rows = db.execute(_sql("""
        SELECT j.job_id, j.tenant_id, j.artist, j.song_title, j.input_r2_key,
               j.segments_json, count(a.id) AS n_diffs
        FROM jobs j
        JOIN audit_log a
          ON a.action = 'lyrics.segments_diff'
         AND (a.detail::jsonb)->>'job_id' = j.job_id
        WHERE j.status = 'done'
          AND j.segments_json IS NOT NULL
          AND j.tenant_id LIKE :tl
        GROUP BY j.job_id, j.tenant_id, j.artist, j.song_title,
                 j.input_r2_key, j.segments_json
        HAVING count(a.id) >= :md
        ORDER BY max(a.created_at) DESC
    """), {"tl": tenant_like, "md": min_diffs}).mappings().all()

    out = []
    for r in rows:
        audits = db.execute(_sql("""
            SELECT detail FROM audit_log
            WHERE action = 'lyrics.segments_diff'
              AND (detail::jsonb)->>'job_id' = :jid
            ORDER BY created_at ASC, id ASC
        """), {"jid": r["job_id"]}).scalars().all()
        segs = r["segments_json"]
        if isinstance(segs, str):
            segs = json.loads(segs)
        out.append({
            "job_id": r["job_id"],
            "tenant_id": r["tenant_id"],
            "artist": r["artist"],
            "song_title": r["song_title"],
            "input_r2_key": r["input_r2_key"],
            "segments_json": segs,
            "audits": [a if isinstance(a, dict) else json.loads(a) for a in audits],
        })
    return out


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--tenant-like", default="universal%",
                   help="filtro SQL LIKE de tenant (default: universal%%)")
    p.add_argument("--min-diffs", type=int, default=3,
                   help="mínimo de audits de corrección para considerar gold (default 3)")
    p.add_argument("--write-list", action="store_true",
                   help=f"escribe los job_ids en {DEFAULT_LIST.name}")
    p.add_argument("--baseline", action="store_true",
                   help="reporta el error machine-vs-gold rebobinando los diffs")
    p.add_argument("--json", type=Path, default=None,
                   help="además del reporte, volcar todo a un JSON")
    args = p.parse_args()

    from database import SessionLocal
    db = SessionLocal()
    try:
        jobs = _fetch_gold_jobs(db, args.tenant_like, args.min_diffs)
    finally:
        db.close()

    if not jobs:
        print("No hay jobs gold con esos filtros.")
        sys.exit(1)

    print(f"{len(jobs)} jobs gold (tenant LIKE {args.tenant_like!r}, "
          f">= {args.min_diffs} correcciones)\n")

    results = []
    for j in jobs:
        row = {
            "job_id": j["job_id"],
            "tenant_id": j["tenant_id"],
            "artist": j["artist"],
            "song": j["song_title"],
            "has_audio": bool(j["input_r2_key"]),
            "n_saves": len(j["audits"]),
        }
        if args.baseline:
            machine, info = rewind_segments(j["segments_json"], j["audits"])
            row["rewind"] = info
            row["metrics"] = score_machine_vs_gold(machine, j["segments_json"])
        results.append(row)

    if args.baseline:
        print(f"{'job_id':13} {'artista':22} {'líneas':>6} {'tocadas':>7} "
              f"{'p50Δs':>7} {'p90Δs':>7} {'≤0.3s%':>7} {'≤1.0s%':>7} {'txtΔ%':>6}")
        agg_p50, agg_tight, agg_loose, agg_text = [], [], [], []
        truncated_jobs = 0
        for r in results:
            m = r["metrics"]
            if r["rewind"]["truncated_saves"]:
                truncated_jobs += 1
            print(f"{r['job_id']:13} {(r['artist'] or '')[:22]:22} "
                  f"{m['lines']:>6} {m['lines_touched']:>7} "
                  f"{m['start_p50']:>7.2f} {m['start_p90']:>7.2f} "
                  f"{m['pct_start_within_tight']:>7.1f} "
                  f"{m['pct_start_within_loose']:>7.1f} "
                  f"{m['pct_text_changed']:>6.1f}")
            agg_p50.append(m["start_p50"])
            agg_tight.append(m["pct_start_within_tight"])
            agg_loose.append(m["pct_start_within_loose"])
            agg_text.append(m["pct_text_changed"])
        print("\n── Agregado (promedio simple entre jobs) ──")
        print(f"p50 |Δstart| por job:  {statistics.mean(agg_p50):.2f} s "
              f"(mediana {statistics.median(agg_p50):.2f} s)")
        print(f"líneas ≤{_TIGHT_S}s:        {statistics.mean(agg_tight):.1f} %")
        print(f"líneas ≤{_LOOSE_S}s:        {statistics.mean(agg_loose):.1f} %")
        print(f"líneas con texto Δ:   {statistics.mean(agg_text):.1f} %")
        if truncated_jobs:
            print(f"⚠ {truncated_jobs} job(s) con saves truncados: el error real "
                  f"es levemente MAYOR al reportado.")
    else:
        for r in results:
            audio = "✓" if r["has_audio"] else "✗ sin audio"
            print(f"  {r['job_id']}  {r['n_saves']:>3} saves  {audio}  "
                  f"{(r['artist'] or '')[:20]} — {(r['song'] or '')[:30]}")

    if args.write_list:
        lines = [
            "# Autogenerado por select_gold_jobs.py — jobs 'done' con",
            "# correcciones reales de operador (ground truth de timing+texto).",
            f"# Filtro: tenant LIKE {args.tenant_like!r}, >= {args.min_diffs} saves.",
        ]
        lines += [r["job_id"] for r in results if r["has_audio"]]
        skipped = [r["job_id"] for r in results if not r["has_audio"]]
        DEFAULT_LIST.write_text("\n".join(lines) + "\n")
        print(f"\n→ {DEFAULT_LIST} escrito con "
              f"{len(results) - len(skipped)} jobs"
              + (f" ({len(skipped)} sin audio en R2, excluidos)" if skipped else ""))

    if args.json:
        args.json.write_text(json.dumps(results, indent=2, ensure_ascii=False))
        print(f"→ detalle en {args.json}")


if __name__ == "__main__":
    main()
