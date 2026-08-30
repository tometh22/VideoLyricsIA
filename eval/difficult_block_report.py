#!/usr/bin/env python3
"""Half-page status for the difficult-song block, with blocked gates visible."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Sequence

from eval.canonical import read_json, write_json


def _optional(path: Path) -> dict[str, Any] | None:
    return read_json(path) if path.is_file() else None


def _corpus_wer(rows: Sequence[dict[str, Any]]) -> float | None:
    words = sum(int(row.get("reference_words") or 0) for row in rows)
    errors = sum(int(row.get("word_errors") or 0) for row in rows)
    return errors / words if words else None


def build(
    cohort_path: Path, code_switch_path: Path, router_path: Path, heavy_path: Path,
    vocalization_path: Path, prior_path: Path, output: Path,
) -> dict[str, Any]:
    cohort = read_json(cohort_path)
    code_switch, router = _optional(code_switch_path), _optional(router_path)
    heavy, vocalization, prior = _optional(heavy_path), _optional(vocalization_path), _optional(prior_path)
    difficult = [row for row in cohort["cases"] if row.get("difficult_gold")]
    easy = [row for row in cohort["cases"] if row.get("difficult_gold") is False]
    hard_before = _corpus_wer(difficult)
    hard_after = ((heavy or {}).get("difficult_queue_wer") or {}).get("after")
    prior_after_seconds = (prior or {}).get("after_seconds_per_song")
    report = {
        "schema_version": 1,
        "north_star": "WER and measured editor minutes on difficult songs",
        "cohort": {
            "comparable": len(difficult) + len(easy), "difficult": len(difficult), "easy": len(easy),
            "difficult_baseline_wer": hard_before, "easy_baseline_wer": _corpus_wer(easy),
        },
        "first_line": {"difficult_wer_before": hard_before, "difficult_wer_after": hard_after},
        "second_line": {
            "easy_minutes_before": None, "easy_minutes_after": None,
            "difficult_minutes_before_operation_range": [20.0, 30.0],
            "difficult_minutes_after": ((heavy or {}).get("difficult_minutes_projection") or {}).get("after_wer_ratio_model"),
            "timer_status": "PENDING_EDITOR_TIMER_SPLIT",
            "prior_unsplit_queue_seconds_per_song": 127.1,
            "prior_ensemble_after_seconds_per_song": prior_after_seconds,
        },
        "gates": {
            "code_switch": ((code_switch or {}).get("gate") or {}).get("status", "PENDING_REPLAY"),
            "difficulty_router": ((router or {}).get("gate") or {}).get("status", "PENDING_REPLAY"),
            "heavy_pipeline": ((heavy or {}).get("gate") or {}).get("status", "PENDING_REPLAY"),
            "vocalizations": ((vocalization or {}).get("gate") or {}).get("status", "PENDING_HUMAN_GOLD"),
            "mss_alt_prior_block": (((prior or {}).get("parts") or {}).get("mss_alt") or {}).get("status", "PENDING_15_STEMS"),
        },
        "policy": {
            "uncertain_routes_heavy": True, "live_tier2_human_always": True,
            "staging_requires_replay_gate": True,
        },
        "closed_questions_for_tomi": [
            {
                "question": "RunPod 15 missing stems",
                "status": "WAITING_PRIVATE_CREDENTIAL_INJECTION",
                "action": "/Users/tomi/conductor/workspaces/VideoLyricsIA-main/riyadh/.context/bin/runpodctl doctor; then reply listo",
            },
            {
                "question": "Spanglish statistical gate",
                "status": "NEEDS_TWO_MORE_HUMAN-GOLD_SONGS",
                "action": "implementation may run now; no product claim until >=3 songs",
            },
        ],
        "staging_mutated": False,
    }
    output.mkdir(parents=True, exist_ok=True)
    write_json(output / "report.json", report)
    before_label = f"{hard_before:.3f}" if hard_before is not None else "PENDIENTE"
    after_label = f"{hard_after:.3f}" if hard_after is not None else "PENDIENTE"
    difficult_after_minutes = report["second_line"]["difficult_minutes_after"]
    difficult_minutes_label = f"{difficult_after_minutes:.1f}" if difficult_after_minutes is not None else "PENDIENTE"
    prior_label = f"{prior_after_seconds:.1f}" if prior_after_seconds is not None else "PENDIENTE"
    lines = [
        f"**WER cola difícil: {before_label} → {after_label}.**",
        f"**Minutos/canción: fácil PENDIENTE TIMER; difícil 20–30 → {difficult_minutes_label} proyectados.**",
        "", f"Cohorte: {len(difficult)} difíciles y {len(easy)} fáciles sobre 41 comparables.", "",
        "| Gate | Estado |", "|---|---|",
        *[f"| {name} | {status} |" for name, status in report["gates"].items()],
        "", f"Cierre bloque anterior: 127,1 s → {prior_label} s. Vivo continúa siempre en Tier 2.", "",
        "Pendientes de Tomi: credencial RunPod por prompt privado; sumar dos canciones spanglish al gold para un gate estadístico defendible.",
        "Staging no fue modificado.",
    ]
    (output / "REPORT.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cohort", type=Path, default=Path("eval/runs/difficult_cohort/report.json"))
    parser.add_argument("--code-switch", type=Path, default=Path("eval/runs/code_switch_score/report.json"))
    parser.add_argument("--router", type=Path, default=Path("eval/runs/difficulty_router/report.json"))
    parser.add_argument("--heavy", type=Path, default=Path("eval/runs/difficult_pipeline/score.json"))
    parser.add_argument("--vocalization", type=Path, default=Path("eval/runs/vocalization_resolver/report.json"))
    parser.add_argument("--prior", type=Path, default=Path("eval/runs/review_block/report.json"))
    parser.add_argument("--output", type=Path, default=Path("eval/runs/difficult_block"))
    args = parser.parse_args()
    report = build(*(getattr(args, name).resolve() for name in ("cohort", "code_switch", "router", "heavy", "vocalization", "prior", "output")))
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
