#!/usr/bin/env python3
"""Assemble the confidence/pruning/MSS replay without hiding blocked gates."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from eval.canonical import read_json, write_json


BASELINE_SECONDS_PER_SONG = 127.1
TARGET_SECONDS_PER_SONG = 50.0
TARGET_RECALL = 0.93


def build(selector_path: Path, pruning_path: Path, mss_path: Path, output: Path) -> dict[str, Any]:
    selector = read_json(selector_path)
    pruning = read_json(pruning_path)
    mss = read_json(mss_path) if mss_path.is_file() else None
    selected = (pruning.get("operating_points") or {}).get("recall_93") or {}
    selector_complete = (selector.get("cohort_gate") or {}).get("status") == "COMPLETE"
    pruning_complete = (pruning.get("gate") or {}).get("status") != "BLOCKED_INCOMPLETE_TIMING_SELECTOR"
    mss_complete = bool(mss and (mss.get("comparison") or {}).get("gate", {}).get("status") in {"GO_PRODUCT", "NO_GO"})
    mss_downstream_applied = bool(mss and mss.get("downstream_flag_replay_applied"))
    complete = selector_complete and pruning_complete and mss_complete and mss_downstream_applied
    after = float(selected.get("queue_seconds_per_song") or 0.0) if complete else None
    recall = float(selected.get("correction_recall") or 0.0) if complete else None
    mss_gate = ((mss or {}).get("comparison") or {}).get("gate", {}).get("status", "MISSING")
    gate = bool(
        complete and after is not None and after <= TARGET_SECONDS_PER_SONG
        and recall is not None and recall >= TARGET_RECALL and mss_gate == "GO_PRODUCT"
    )
    report = {
        "schema_version": 1,
        "objective": "total reviewer seconds per song",
        "baseline_seconds_per_song": BASELINE_SECONDS_PER_SONG,
        "after_seconds_per_song": after,
        "target_seconds_per_song": TARGET_SECONDS_PER_SONG,
        "correction_recall": recall,
        "parts": {
            "stems": {
                "status": "COMPLETE" if selector_complete else "BLOCKED",
                "selector_cohort": (selector.get("cohort_gate") or {}).get("status"),
            },
            "timing_selector": {
                "status": (selector.get("gate") or {}).get("status"),
                "operating_points": selector.get("operating_points"),
                "ztlr": selector.get("ztlr"),
            },
            "flag_pruning": {
                "status": (pruning.get("gate") or {}).get("status"),
                "recall_93_point": selected,
            },
            "mss_alt": {
                "status": mss_gate,
                "downstream_flag_replay_applied": mss_downstream_applied,
                "comparison": (mss or {}).get("comparison"),
            },
        },
        "gate": {
            "requirements": "seconds/song <= 50; recall >= 0.93; all 41 stems; MSS gate passed and propagated through downstream flag replay",
            "status": "GO_PREPARE_STAGING_FLAGS" if gate else "BLOCKED_INCOMPLETE_REPLAY" if not complete else "NO_GO",
        },
        "staging_mutated": False,
        "decisions_for_tomi": {
            "selector_precision": "PENDING_COMPLETE_41_SONG_CURVE" if not complete else "choose one of 90/93/95",
            "pruning_cutoff": "PENDING_COMPLETE_ENSEMBLE" if not complete else "choose one of recall 90/93/95/min-cost",
        },
    }
    output.mkdir(parents=True, exist_ok=True)
    write_json(output / "report.json", report)
    after_label = f"{after:.1f}" if after is not None else "PENDIENTE"
    lines = [
        f"**Segundos de revisión/canción: {BASELINE_SECONDS_PER_SONG:.1f} → {after_label}.**",
        "",
        "| Parte | Estado | Resultado |",
        "|---|---|---|",
        f"| Stems 41/41 | {'OK' if selector_complete else 'BLOQUEADO'} | cohorte selector: {(selector.get('cohort_gate') or {}).get('status')} |",
        f"| Selector timing | {(selector.get('gate') or {}).get('status')} | ZTLR medido: {(selector.get('ztlr') or {}).get('measured_with_correctly_resolved_timing_only', 'pendiente')} |",
        f"| Poda flags | {(pruning.get('gate') or {}).get('status')} | recall: {selected.get('correction_recall', 'pendiente')}; falsos: {selected.get('false_flags', 'pendiente')} |",
        f"| MSS-ALT | {mss_gate} | cohorte completa requerida |",
        "",
        f"**Gate conjunto:** {report['gate']['status']}. Staging no fue modificado.",
        "",
        "Decisiones de Tomi: umbral del selector y corte de poda quedan cerradas recién con la curva completa de 41 canciones.",
    ]
    (output / "REPORT.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--selector", type=Path, default=Path("eval/runs/timing_confidence/report.json"))
    parser.add_argument("--pruning", type=Path, default=Path("eval/runs/pruned_review_flags/report.json"))
    parser.add_argument("--mss", type=Path, default=Path("eval/runs/mss_alt/large-v3-turbo/report.json"))
    parser.add_argument("--output", type=Path, default=Path("eval/runs/review_block"))
    args = parser.parse_args()
    report = build(args.selector.resolve(), args.pruning.resolve(), args.mss.resolve(), args.output.resolve())
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
