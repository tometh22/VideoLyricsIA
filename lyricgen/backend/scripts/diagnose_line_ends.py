#!/usr/bin/env python3
"""Diagnóstico de los fines de línea antes de la prueba prospectiva.

Solo lectura. Usa los mismos jobs de staging que la fase 1/2 (cargadores
de ``calibrate_display_timing.py``).

a) Cómo termina cada línea de máquina: en la última palabra cantada, en
   última palabra + hold, o estirada hasta "inicio de la siguiente − gap".
b) Ediciones de fin > 500 ms: distribución de (fin aprobado − fin de la
   última palabra) = hold implícito del operador, y aire que deja antes de
   la siguiente línea aprobada.
c) Lo mismo para ediciones de fin entre 200 y 500 ms (y 50-200 ms).
d) Simula la regla "fin = última palabra + H, nunca más allá de inicio de
   la siguiente − G" sobre todas las líneas emparejadas y cuenta cuántas
   ediciones de fin desaparecen y cuántas aparecen.

Uso:
    DATABASE_URL=... python3.11 scripts/diagnose_line_ends.py --json-out /tmp/ends.json
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import importlib.util
import json
from pathlib import Path
import sys

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

_spec = importlib.util.spec_from_file_location(
    "calibrate_display_timing", BACKEND / "scripts" / "calibrate_display_timing.py",
)
calibrate = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(calibrate)

EDIT_THRESHOLD_S = 0.05
CURRENT_GAP_S = 0.01          # lead_in._MIN_GAP_S
CURRENT_HOLD_S = 0.5          # LYRIC_HOLD_S en staging
MATCH_TOLERANCE_S = 0.005     # para clasificar "igual a"
WORD_TOLERANCE_S = 0.02
MIN_LINE_S = 0.1
HOLDS_S = (0.0, 0.1, 0.15, 0.2, 0.25, 0.3, 0.4, 0.5)
GAPS_S = (0.01, 0.1, 0.2, 0.25, 0.3, 0.4)
BANDS = (("gt_500ms", 0.5, float("inf")), ("200_500ms", 0.2, 0.5), ("50_200ms", 0.05, 0.2))


def origin_group(origin: str) -> str:
    if origin == "ctc":
        return "ctc"
    if origin.startswith("whisperx"):
        return "whisperx"
    return "other"


def by_job(lines: list[dict]) -> dict[str, list[dict]]:
    grouped: dict[str, list[dict]] = defaultdict(list)
    for r in lines:
        grouped[r["job_id"]].append(r)
    return grouped


def end_category(r: dict, next_start: float | None, *, gap: float = CURRENT_GAP_S,
                 hold: float = CURRENT_HOLD_S) -> str:
    """Cómo terminó la línea de máquina."""
    end = r["machine_end"]
    if next_start is None:
        return "last_line"
    stretched = abs(end - (next_start - gap)) <= MATCH_TOLERANCE_S
    if not r["has_words"]:
        return "stretched_to_next" if stretched else "no_words"
    last_word = r["raw_end"]
    if abs(end - last_word) <= WORD_TOLERANCE_S:
        return "at_last_word"
    if abs(end - (last_word + hold)) <= WORD_TOLERANCE_S:
        return "last_word_plus_hold"
    if stretched:
        # El hold quería ir más allá de la siguiente: se clampeó.
        return ("stretched_to_next_hold_clamped" if next_start - gap < last_word + hold
                else "stretched_to_next_beyond_hold")
    return "other"


def quantiles(values: list[float]) -> dict:
    if not values:
        return {"n": 0}
    ordered = sorted(values)
    n = len(ordered)
    return {"n": n, **{f"p{int(p * 100)}": round(ordered[int(p * (n - 1))], 3)
                       for p in (0.1, 0.25, 0.5, 0.75, 0.9)}}


def section_a(grouped) -> dict:
    counts: dict[str, Counter] = defaultdict(Counter)
    for rows in grouped.values():
        for k, r in enumerate(rows):
            next_start = rows[k + 1]["machine_start"] if k + 1 < len(rows) else None
            category = end_category(r, next_start)
            for key in (origin_group(r["origin"]), "all"):
                counts[key][category] += 1
                counts[key]["lines"] += 1
    return {
        origin: {
            "lines": c["lines"],
            **{cat: {"n": n, "share": round(n / c["lines"], 4)}
               for cat, n in sorted(c.items()) if cat != "lines"},
        }
        for origin, c in counts.items()
    }


def section_band(grouped, low: float, high: float) -> dict:
    implied: dict[str, list[float]] = defaultdict(list)
    implied_same_text: list[float] = []
    air: dict[str, list[float]] = defaultdict(list)
    direction: dict[str, Counter] = defaultdict(Counter)
    machine_category: dict[str, Counter] = defaultdict(Counter)
    for rows in grouped.values():
        for k, r in enumerate(rows):
            if not r["matched"] or r["approved_end"] is None:
                continue
            delta = r["approved_end"] - r["machine_end"]
            if not (low <= abs(delta) < high):
                continue
            origin = origin_group(r["origin"])
            for key in (origin, "all"):
                direction[key]["trim" if delta < 0 else "extend"] += 1
            next_row = rows[k + 1] if k + 1 < len(rows) else None
            category = end_category(r, next_row["machine_start"] if next_row else None)
            for key in (origin, "all"):
                machine_category[key][category] += 1
            if not r["has_words"]:
                continue
            hold = r["approved_end"] - r["raw_end"]
            implied[origin].append(hold)
            implied["all"].append(hold)
            if not r["text_changed"]:
                implied_same_text.append(hold)
            if next_row is not None and next_row["approved_start"] is not None:
                gap = next_row["approved_start"] - r["approved_end"]
                air[origin].append(gap)
                air["all"].append(gap)
    return {
        "direction": {k: dict(v) for k, v in direction.items()},
        "machine_end_category": {k: dict(v) for k, v in machine_category.items()},
        "implied_hold_s": {k: quantiles(v) for k, v in implied.items()},
        "implied_hold_same_text_s": quantiles(implied_same_text),
        "air_to_next_approved_s": {k: quantiles(v) for k, v in air.items()},
    }


def rule_end(r: dict, next_start: float | None, *, hold: float, gap: float) -> float:
    """fin = última palabra + hold, nunca más allá de inicio siguiente − gap."""
    # Como lead_in.apply_hold: la última línea queda intacta.
    if not r["has_words"] or next_start is None:
        return r["machine_end"]
    end = min(r["raw_end"] + hold, next_start - gap)
    return max(end, r["machine_start"] + MIN_LINE_S)


def simulate_rule(grouped, *, hold: float, gap: float, tolerance: float | None = None) -> dict:
    """Ediciones de fin (>= 50 ms) hoy vs. con la regla, en líneas emparejadas.

    ``tolerance``: una línea que hoy no se editó solo cuenta como edición
    nueva si la regla la aleja más que eso de lo que el operador aceptó.
    """
    now = sim = removed = created = lines = 0
    by_origin: dict[str, Counter] = defaultdict(Counter)
    for rows in grouped.values():
        for k, r in enumerate(rows):
            if not r["matched"] or r["approved_end"] is None:
                continue
            next_start = rows[k + 1]["machine_start"] if k + 1 < len(rows) else None
            new_end = rule_end(r, next_start, hold=hold, gap=gap)
            edited_now = abs(r["machine_end"] - r["approved_end"]) >= EDIT_THRESHOLD_S
            off = abs(new_end - r["approved_end"])
            edited_sim = off >= EDIT_THRESHOLD_S
            if tolerance is not None and not edited_now:
                edited_sim = off >= tolerance
            lines += 1
            now += edited_now
            sim += edited_sim
            removed += edited_now and not edited_sim
            created += edited_sim and not edited_now
            c = by_origin[origin_group(r["origin"])]
            c["now"] += edited_now
            c["sim"] += edited_sim
            c["removed"] += edited_now and not edited_sim
            c["created"] += edited_sim and not edited_now
    return {
        "hold_s": hold, "gap_s": gap, "lines": lines,
        "end_edits_now": now, "end_edits_sim": sim,
        "removed": removed, "created": created, "net": sim - now,
        "by_origin": {k: dict(v) for k, v in by_origin.items()},
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--roles", default=str(BACKEND / "data" / "song_roles.json"))
    parser.add_argument("--staging-limit", type=int, default=100)
    parser.add_argument("--tenant", action="append", default=[])
    parser.add_argument("--json-out", default="")
    args = parser.parse_args()
    args.tenant = args.tenant or ["universal_music"]
    args.gold_dir = ""

    _jobs, lines = calibrate.collect(args)
    grouped = by_job([r for r in lines if r["dataset"] == "staging" and r["alignment_complete"]])

    report = {
        "a_machine_end_categories": section_a(grouped),
        "bands": {name: section_band(grouped, low, high) for name, low, high in BANDS},
    }
    # Valores de la regla tomados de los datos: hold implícito y aire en la
    # banda 200-500 ms (la que (b)/(c) muestran como recortes de presentación).
    band = report["bands"]["200_500ms"]
    data_hold = max(0.0, band["implied_hold_same_text_s"].get("p50") or 0.0)
    data_gap = band["air_to_next_approved_s"].get("all", {}).get("p25") or CURRENT_GAP_S
    report["d_rule_from_data"] = {
        "hold_s": data_hold, "gap_s": data_gap,
        "strict": simulate_rule(grouped, hold=data_hold, gap=data_gap),
        "tolerant_100ms": simulate_rule(grouped, hold=data_hold, gap=data_gap, tolerance=0.10),
    }
    report["d_current_rule_check"] = simulate_rule(grouped, hold=CURRENT_HOLD_S, gap=CURRENT_GAP_S)
    report["d_sweep"] = [
        {**{k: v for k, v in simulate_rule(grouped, hold=h, gap=g).items() if k != "by_origin"},
         "tolerant_100ms_net": simulate_rule(grouped, hold=h, gap=g, tolerance=0.10)["net"]}
        for h in HOLDS_S for g in GAPS_S
    ]
    text = json.dumps(report, ensure_ascii=False, indent=1)
    if args.json_out:
        Path(args.json_out).write_text(text)
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
