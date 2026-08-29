#!/usr/bin/env python3
"""Publish aggregate diagnostic evidence without client text, audio or paths."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from eval.canonical import read_json, write_json


def _pct(value: float) -> str:
    return f"{100 * value:.1f}%"


def run(root: Path, output: Path) -> dict:
    old_predictor = read_json(output / "error_predictor_report.json")
    t4 = read_json(root / "eval/runs/t4_learned_v2/report.json")
    predictor = read_json(root / "eval/runs/error_predictor_v2/report.json")
    taxonomy = read_json(root / "eval/runs/taxonomy_ensemble/report.json")
    clips = read_json(root / "eval/runs/taxonomy_adjudication/clip_report.json")
    replay = read_json(root / "eval/runs/runtime_suggestions_replay/report.json")
    phase2 = read_json(root / "eval/runs/phase2_status/report.json")
    lora = read_json(root / "eval/runs/lora_jamendo_large_v3_turbo_smoke/report.json")
    t7 = read_json(root / "eval/runs/t7_corruptions/report.json")
    summary = {
        "schema_version": 1,
        "scope": "eval branch only; no staging/production mutation",
        "t4": {
            "old_within_150ms": t4["old_result"]["within_150ms"],
            "intermediate_drag_events_removed": t4["dataset_audit"]["intermediate_drag_events"],
            "classifier_auc": t4["classifier_timing_touched"]["auc"],
            "regressor_within_150ms": t4["regressor_clean_net_delta"]["baselines_and_model"]["lightgbm"]["within_150ms_song_bootstrap_ci"],
            "best_trivial_within_150ms": t4["regressor_clean_net_delta"]["baselines_and_model"]["training_fold_median_corrected_only"]["within_150ms_song_bootstrap_ci"],
            "verdict": t4["verdict"],
        },
        "error_predictor": {
            "old_auc": old_predictor["auc_song_bootstrap_ci"],
            "new_auc": predictor["auc_song_bootstrap_ci"],
            "new_pr_auc": predictor["pr_auc_song_bootstrap_ci"],
            "first_third_real_corrections": predictor["review_queue_efficiency"],
            "verdict": predictor["gate"]["status"],
        },
        "taxonomy": {
            "models": taxonomy["models"],
            "unanimous": taxonomy["unanimous"],
            "disputed": taxonomy["disputed"],
            "audio_clips": clips["clips"],
            "data_egress": False,
        },
        "current_runtime_suggestion_replay": {
            "eligible_songs": replay["eligible_songs"],
            "replayed_songs": replay["replayed_songs"],
            "missing_runtime_stems": replay["missing_runtime_stems"],
            "timing": replay["timing"],
            "text_and_vocalization": replay["text_and_vocalization"],
        },
        "lora_executor": {
            "base_model": lora["base_model"],
            "research_songs": lora["songs"],
            "training_executor_validated": lora["training_executor_validated"],
            "pipeline_validated": lora["pipeline_validated"],
            "remaining_pipeline_gate": lora["remaining_pipeline_gate"],
            "data_egress": lora["data_egress"],
            "umg_training": "BLOCKED_POLICY_AUTHORIZATION",
        },
        "t7_preparation": t7,
        "phase2": phase2,
        "decisions": [
            {
                "id": "taxonomy_human_certificate",
                "question": "¿Completar la validación humana antes de publicar categorías no unánimes?",
                "evidence": f"Solo {taxonomy['unanimous']}/454 son unánimes; {taxonomy['disputed']} requieren adjudicación.",
                "recommendation": "SI; no usar mayoría simple como ground truth.",
            },
            {
                "id": "allow_umg_training",
                "question": "¿Autorizar entrenamiento con las 498 muestras UMG?",
                "evidence": "El ejecutor large-v3-turbo funciona localmente con Jamendo; el contrato publicado hoy dice que no se entrena con datos del cliente.",
                "recommendation": "NO todavía; resolver uso, retención, borrado y titularidad del adaptador.",
            },
            {
                "id": "observe_error_predictor",
                "question": "¿Mostrar el predictor solo como orden sugerido en observación?",
                "evidence": f"Primer tercio concentra {_pct(predictor['review_queue_efficiency']['real_corrections_found'])} de correcciones vs {_pct(predictor['review_queue_efficiency']['current_line_order_corrections_found'])} del orden actual; AUC {_pct(predictor['auc_song_bootstrap_ci']['estimate'])} no pasa 0,80.",
                "recommendation": "SI en observación; NO como router obligatorio.",
            },
        ],
    }
    write_json(output / "diagnostic_replay_summary.json", summary)
    markdown = f"""# Estado diagnóstico y replay — 2026-08-29

Todo este bloque vive en la rama de evaluación; no modificó staging ni producción.

## Resultado ejecutivo

- **T4 aprendido sigue NO_GO.** La autopsia eliminó {t4['dataset_audit']['intermediate_drag_events']} arrastres intermedios mal contados. El clasificador sí predice qué línea será tocada (AUC {_pct(t4['classifier_timing_touched']['auc']['estimate'])}), pero el regresor acierta ±150 ms en solo {_pct(t4['regressor_clean_net_delta']['baselines_and_model']['lightgbm']['within_150ms_song_bootstrap_ci']['estimate'])} y pierde contra la mediana de líneas corregidas ({_pct(t4['regressor_clean_net_delta']['baselines_and_model']['training_fold_median_corrected_only']['within_150ms_song_bootstrap_ci']['estimate'])}).
- **Predictor de errores:** AUC pasó de {_pct(old_predictor['auc_song_bootstrap_ci']['estimate'])} a {_pct(predictor['auc_song_bootstrap_ci']['estimate'])} (CI95 {_pct(predictor['auc_song_bootstrap_ci']['low'])}–{_pct(predictor['auc_song_bootstrap_ci']['high'])}); no cruza 0,80. Como ordenador, revisar el primer tercio captura {_pct(predictor['review_queue_efficiency']['real_corrections_found'])} de las correcciones, contra {_pct(predictor['review_queue_efficiency']['current_line_order_corrections_found'])} en el orden actual.
- **Replay exacto del selector desplegado:** {replay['replayed_songs']}/{replay['eligible_songs']} stems `mdx_extra` disponibles (5 preservados y el resto regenerados localmente). Hubo {replay['timing']['corrections']} correcciones reales, {replay['timing']['proposals']} propuestas y {replay['timing']['correct_proposals']} coincidencias (recall {_pct(replay['timing']['recall'])}, precisión {_pct(replay['timing']['precision'])}). Con bootstrap de 10 canciones, el selector queda **{replay['timing']['gate']['status']}**.
- **Taxonomía:** tres familias locales, cero egreso. {taxonomy['unanimous']}/454 unánimes; {taxonomy['disputed']} disputadas, todas con clip local. La expectativa de una cola de 5–20 quedó refutada.
- **LoRA:** `whisper-large-v3-turbo` completó un paso real sobre JamendoLyrics filtrado a licencias BY/BY-SA/CC BY/CC BY-SA, sin audio UMG ni egreso. Esto valida el ejecutor, no una mejora. Las 498 muestras UMG siguen bloqueadas.
- **T7 preparado:** {t7['samples']} pares en {t7['songs']} canciones ({t7['counts']['word_omission']} omisiones, {t7['counts']['interjection_insertion']} inserciones y {t7['counts']['phonetic_substitution']} sustituciones); entrenamiento bloqueado por la misma autorización UMG.
- **Fase 2:** repetidas {_pct(phase2['repetition_prerequisite']['repeated_error_rate'])} vs únicas {_pct(phase2['repetition_prerequisite']['unique_error_rate'])}; uplift relativo {_pct(phase2['repetition_prerequisite']['relative_uplift'])}, debajo del 20%, por lo que no se implementa votación. Rescoring fonético carece de n-best/posteriors prehumanos; Gemini carece de credencial y autorización de egreso; N=5 espera stems exactos.

## Decisiones de Tomi

1. **Taxonomía:** recomendación **sí** a validación humana; no publicar las {taxonomy['disputed']} disputadas por mayoría.
2. **Entrenamiento UMG:** recomendación **no todavía**; ver `umg_training_egress_analysis.md`.
3. **Predictor:** recomendación **sí solo en observación**, nunca como router obligatorio mientras no pase AUC 0,80.
"""
    (output / "STATUS_DIAGNOSTIC_REPLAY.md").write_text(markdown, encoding="utf-8")
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path("."))
    parser.add_argument("--output", type=Path, default=Path("eval/reports/baseline-2026-08-29"))
    args = parser.parse_args()
    result = run(args.root.resolve(), args.output.resolve())
    print(json.dumps({"published": True, "decisions": len(result["decisions"])}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
