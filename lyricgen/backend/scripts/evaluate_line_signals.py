#!/usr/bin/env python3
"""¿Qué señales por línea marcan las líneas que el operador corrigió?

Solo lectura. Toma las líneas que arma ``calibrate_display_timing.py``
(``--lines-out``) y mide, para cada señal ya guardada en el segmento y
para combinaciones OR de dos o tres señales, qué parte de las líneas con
error real queda marcada (cobertura) y qué parte de las líneas correctas se
marca de más.

Error real, separado:

- **texto**: la línea de máquina se borró o su texto normalizado
  (minúsculas, sin puntuación) cambió;
- **timing**: mismo texto, pero el inicio o el fin aprobados quedan a más
  de ``--timing-error-ms`` (150 ms) del timing *calibrado*: el pipeline se
  re-simula con ``--timing-params`` antes de comparar, así un corrimiento
  global que la calibración ya absorbe no cuenta como error de la línea.
  Las líneas sin palabras no se pueden re-simular y se comparan con su
  timing de máquina.

Las ``unsafe_windows`` se leen de la evidencia congelada antes de la
revisión (``editor_documents.machine_evidence.decisions.quality``). Las del
job (``jobs.transcription_quality``) se recalculan después de que edita el
operador (motivo ``operator_edited_segment``) y filtrarían la respuesta.

Uso:
    DATABASE_URL=... python3.11 scripts/evaluate_line_signals.py \\
        --lines /tmp/lines.pkl --json-out /tmp/signals.json
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import itertools
import json
import os
from pathlib import Path
import pickle
import sys

import numpy as np

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

_spec = importlib.util.spec_from_file_location(
    "calibrate_display_timing", BACKEND / "scripts" / "calibrate_display_timing.py",
)
calibrate = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(calibrate)

NUMERIC = ("ctc_min_score", "ctc_mean_score", "recognition_score", "alignment_score",
           "provider_min_score")
BOOLEAN = ("review", "timing_validation", "unsafe_window")
SIGNALS = NUMERIC + BOOLEAN
QUANTILES = tuple(range(5, 100, 5))
COMBO_QUANTILES = (10, 20, 30, 40, 50, 60, 70)
TARGET_RECALL = 0.85
MAX_OVERFLAG = 0.30


def _number(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if np.isfinite(number) else None


def line_signals(segment: dict, *, start: float, end: float, windows: list[dict] | None,
                 index: int) -> dict:
    provider = segment.get("provider_evidence") if isinstance(segment.get("provider_evidence"), dict) else {}
    in_window = None
    if windows is not None:
        in_window = False
        for window in windows:
            if index in (window.get("segment_indices") or []):
                in_window = True
                break
            w_start, w_end = _number(window.get("start")), _number(window.get("end"))
            if w_start is not None and w_end is not None and start < w_end and end > w_start:
                in_window = True
                break
    return {
        "ctc_min_score": _number(segment.get("ctc_min_score")),
        "ctc_mean_score": _number(segment.get("ctc_mean_score")),
        "recognition_score": _number(segment.get("recognition_score")),
        "alignment_score": _number(segment.get("alignment_score")),
        "provider_min_score": _number(provider.get("min_score")),
        "review": bool(segment.get("review")),
        "timing_validation": bool(segment.get("timing_validation")),
        "unsafe_window": in_window,
    }


def frozen_windows(conn, job_ids: list[str]) -> dict[str, list[dict]]:
    cur = conn.cursor()
    cur.execute(
        """
        select job_id, machine_evidence->'decisions'->'quality'->'unsafe_windows'
          from editor_documents where job_id = any(%s)
        """,
        (job_ids,),
    )
    return {job_id: windows for job_id, windows in cur.fetchall() if isinstance(windows, list)}


def build_table(lines: list[dict], windows: dict[str, list[dict]], *, params: dict,
                timing_error_s: float) -> dict:
    sim = calibrate.Simulator(lines)
    sim_start, sim_end = sim.run(**params)
    rows = []
    for i, r in enumerate(lines):
        if not r["alignment_complete"]:
            continue
        text_error = bool(r["deleted"] or r["text_changed"])
        timing_error = False
        if r["matched"] and not r["text_changed"]:
            timing_error = (
                abs(sim_start[i] - r["approved_start"]) > timing_error_s
                or abs(sim_end[i] - r["approved_end"]) > timing_error_s
            )
        rows.append({
            "dataset": r["dataset"], "job_id": r["job_id"], "role": r["role"],
            "live": r["live"], "origin": r["origin"],
            "text_error": text_error, "timing_error": bool(timing_error),
            "signals": line_signals(
                r["segment"], start=r["machine_start"], end=r["machine_end"],
                windows=windows.get(r["job_id"]), index=r["index"],
            ),
        })
    return {"rows": rows}


def _split(job_id: str) -> str:
    """Partición fija por job para staging (no está en el registro de roles)."""
    return "A" if int(hashlib.sha256(job_id.encode()).hexdigest(), 16) % 2 == 0 else "B"


class Table:
    def __init__(self, rows: list[dict]):
        self.rows = rows
        self.text = np.array([r["text_error"] for r in rows], dtype=bool)
        self.timing = np.array([r["timing_error"] for r in rows], dtype=bool)
        self.any = self.text | self.timing
        self.values = {
            name: np.array([
                np.nan if r["signals"][name] is None else float(r["signals"][name])
                for r in rows
            ])
            for name in SIGNALS
        }
        self.groups = {"all": np.ones(len(rows), dtype=bool)}
        for key in ("dataset", "role", "live", "origin"):
            for value in sorted({str(r[key]) for r in rows}):
                self.groups[f"{key}={value}"] = np.array([str(r[key]) == value for r in rows])
        staging = np.array([r["dataset"] == "staging" for r in rows])
        for half in ("A", "B"):
            self.groups[f"staging_split={half}"] = staging & np.array(
                [_split(r["job_id"]) == half for r in rows])

    def flag(self, name: str, threshold: float | None) -> np.ndarray:
        values = self.values[name]
        if name in BOOLEAN:
            return values == 1.0
        return ~np.isnan(values) & (values < threshold)

    def metrics(self, flagged: np.ndarray, group: str = "all") -> dict:
        g = self.groups[group]
        out = {"lines": int(g.sum()), "flag_rate": round(float((flagged & g).sum()) / max(1, int(g.sum())), 4)}
        for target_name, target in (("text", self.text), ("timing", self.timing), ("any", self.any)):
            errors = target & g
            correct = ~target & g
            out[target_name] = {
                "errors": int(errors.sum()),
                "recall": round(float((flagged & errors).sum()) / max(1, int(errors.sum())), 4),
                "overflag": round(float((flagged & correct).sum()) / max(1, int(correct.sum())), 4),
            }
        return out

    def thresholds(self, name: str, quantiles=QUANTILES) -> list:
        if name in BOOLEAN:
            return [None]
        values = self.values[name]
        values = values[~np.isnan(values)]
        if not len(values):
            return []
        return sorted({round(float(np.percentile(values, q)), 4) for q in quantiles})


def coverage(table: Table) -> dict:
    out = {}
    for group in ("all", "dataset=gold", "dataset=staging"):
        g = table.groups.get(group)
        if g is None:
            continue
        out[group] = {
            name: round(float((~np.isnan(table.values[name]) & g).sum()) / max(1, int(g.sum())), 4)
            for name in SIGNALS
        }
        out[group]["true_rate"] = {
            name: round(float(((table.values[name] == 1.0) & g).sum()) / max(1, int(g.sum())), 4)
            for name in BOOLEAN
        }
    return out


def single_sweeps(table: Table) -> dict:
    out = {}
    for name in SIGNALS:
        out[name] = [
            {"threshold": t, **table.metrics(table.flag(name, t))}
            for t in table.thresholds(name)
        ]
    return out


def combo_search(table: Table, *, fit_group: str) -> list[dict]:
    options = []
    for name in SIGNALS:
        for t in table.thresholds(name, COMBO_QUANTILES):
            options.append((name, t))
    results = []
    for size in (1, 2, 3):
        for combo in itertools.combinations(options, size):
            if len({name for name, _ in combo}) < size:
                continue
            flagged = np.zeros(len(table.rows), dtype=bool)
            for name, t in combo:
                flagged |= table.flag(name, t)
            results.append({
                "combo": [{"signal": n, "threshold": t} for n, t in combo],
                "fit": table.metrics(flagged, fit_group),
                "_flagged": flagged,
            })
    return results


def best_combos(table: Table, results: list[dict], *, target: str, fit_group: str,
                eval_groups: list[str]) -> dict:
    meets = [r for r in results if r["fit"][target]["recall"] >= TARGET_RECALL
             and r["fit"][target]["overflag"] < MAX_OVERFLAG]
    by_recall_under_cap = sorted(
        (r for r in results if r["fit"][target]["overflag"] < MAX_OVERFLAG),
        key=lambda r: (-r["fit"][target]["recall"], r["fit"][target]["overflag"]),
    )[:5]
    by_overflag_at_recall = sorted(
        (r for r in results if r["fit"][target]["recall"] >= TARGET_RECALL),
        key=lambda r: (r["fit"][target]["overflag"], -r["fit"][target]["recall"]),
    )[:5]

    def project(r):
        return {
            "combo": r["combo"],
            "fit": {"group": fit_group, **r["fit"][target], "flag_rate": r["fit"]["flag_rate"]},
            "eval": {g: {**table.metrics(r["_flagged"], g)[target],
                         "flag_rate": table.metrics(r["_flagged"], g)["flag_rate"]}
                     for g in eval_groups if g in table.groups},
        }

    return {
        "meets_target": [project(r) for r in sorted(
            meets, key=lambda r: r["fit"][target]["overflag"])[:5]],
        "best_recall_with_overflag_below_30": [project(r) for r in by_recall_under_cap],
        "lowest_overflag_with_recall_85": [project(r) for r in by_overflag_at_recall],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--lines", required=True, help="pickle de calibrate_display_timing --lines-out")
    parser.add_argument("--timing-params", default=json.dumps(
        calibrate.CANDIDATES["single_lead_80"]), help="JSON de parámetros calibrados")
    parser.add_argument("--timing-error-ms", type=float, default=150.0)
    parser.add_argument("--json-out", default="")
    args = parser.parse_args()

    data = pickle.loads(Path(args.lines).read_bytes())
    lines = data["lines"]
    windows: dict[str, list[dict]] = {}
    if os.environ.get("DATABASE_URL"):
        import psycopg2

        conn = psycopg2.connect(
            os.environ["DATABASE_URL"],
            options="-c default_transaction_read_only=on -c statement_timeout=300000",
        )
        try:
            windows = frozen_windows(conn, sorted({r["job_id"] for r in lines}))
        finally:
            conn.close()
    params = json.loads(args.timing_params)
    table = Table(build_table(lines, windows, params=params,
                              timing_error_s=args.timing_error_ms / 1000)["rows"])

    eval_groups = [g for g in table.groups if g.startswith(("dataset=", "role=", "live=", "staging_split="))]
    report = {
        "timing_params": params,
        "timing_error_ms": args.timing_error_ms,
        "lines": len(table.rows),
        "jobs_with_frozen_windows": len(windows),
        "errors": {
            group: {
                "lines": int(g.sum()),
                "text_errors": int((table.text & g).sum()),
                "timing_errors": int((table.timing & g).sum()),
                "any_errors": int((table.any & g).sum()),
            }
            for group, g in table.groups.items()
        },
        "coverage": coverage(table),
        "single_signal": single_sweeps(table),
    }
    combos = combo_search(table, fit_group="all")
    report["combos_all"] = {
        target: best_combos(table, combos, target=target, fit_group="all", eval_groups=eval_groups)
        for target in ("text", "timing", "any")
    }
    # Sobreajuste: elegir en la mitad A de staging, medir en la B y en el gold.
    fit = combo_search(table, fit_group="staging_split=A")
    report["combos_fit_staging_A"] = {
        target: best_combos(table, fit, target=target, fit_group="staging_split=A",
                            eval_groups=eval_groups)
        for target in ("text", "timing", "any")
    }
    text = json.dumps(report, ensure_ascii=False, indent=1, default=str)
    if args.json_out:
        Path(args.json_out).write_text(text)
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
