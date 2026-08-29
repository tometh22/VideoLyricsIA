"""Publish the half-page zero-touch report from immutable replay outputs."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from eval.canonical import read_json, write_json


def _pct(value: float) -> str:
    return f"{100 * value:.1f}%"


def _realign_summary(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    report = read_json(path)
    aligners = {}
    for name, payload in report["aligners"].items():
        variants = {"acoustic_raw": payload.get("metrics")}
        calibration = payload.get("loo_display_calibration") or {}
        for variant, value in (calibration.get("variants") or {}).items():
            variants[f"display_{variant}"] = value.get("metrics")
        variants = {key: value for key, value in variants.items() if value}
        best_name, best = min(
            variants.items(), key=lambda pair: pair[1]["p90_boundary_abs_ms"]["estimate"],
        ) if variants else (None, None)
        aligners[name] = {
            "best_variant": best_name,
            "best": best,
            "variants": variants,
            "failures": payload.get("failures") or [],
        }
    return {
        "audio_source": report.get("audio_source"),
        "eligible_songs": report["eligible_songs"],
        "audio_available": report.get("audio_available"),
        "aligners": aligners,
    }


def publish(
    ztlr_path: Path, flags_path: Path, mix_path: Path, stem_path: Path,
    mss_path: Path, json_output: Path, markdown_output: Path,
) -> dict[str, Any]:
    ztlr = read_json(ztlr_path)
    flags = read_json(flags_path)
    mix = _realign_summary(mix_path)
    stem = _realign_summary(stem_path)
    mss = read_json(mss_path) if mss_path.is_file() else None
    report = {
        "schema_version": 1,
        "north_star": {
            "ztlr_baseline": ztlr["ztlr"],
            "ztlr_ci": ztlr["ztlr_song_bootstrap_ci"],
            "work_units": ztlr["work_units"],
            "touch_categories": ztlr["category_counts"],
            "historical_minutes": ztlr["minutes"],
        },
        "central_realignment": {"mix": mix, "stem": stem},
        "flag_union": {
            "sources": flags["sources"], "thresholds": flags["thresholds"],
            "metrics": flags["metrics"], "confidence_intervals": flags["confidence_intervals"],
            "reviewer_projection": flags["reviewer_projection"],
        },
        "mss_alt": mss,
        "rules": {
            "gold_leakage": False,
            "approved_timing_visible_to_aligners": False,
            "learned_display_policy": "strict leave-one-song-out",
            "staging_mutation": False,
        },
    }
    write_json(json_output, report)

    central_lines = []
    for label, source in (("mix", mix), ("stem", stem)):
        if not source:
            central_lines.append(f"- **Re-alineación {label}:** pendiente.")
            continue
        for name, payload in source["aligners"].items():
            best = payload["best"]
            if not best:
                continue
            central_lines.append(
                f"- **{label}/{name}/{payload['best_variant']}:** "
                f"{best['songs']} canciones, p50 {best['p50_boundary_abs_ms']['estimate']:.0f} ms, "
                f"p90 {best['p90_boundary_abs_ms']['estimate']:.0f} ms, "
                f"±150 ms en {_pct(best['within_150ms_both']['estimate'])}, "
                f"ZTLR proyectado {_pct(best['projected_ztlr']['estimate'])}; "
                f"**{best['gate']['status']}**."
            )
    mss_line = "- **MSS-ALT:** replay pendiente."
    if mss:
        native = mss["families"]["native"]["wer"]["estimate"]
        vad = mss["families"]["mss_rms_vad"]["wer"]["estimate"]
        relative = (native - vad) / max(native, 1e-9)
        noun = "canción" if mss["completed_songs"] == 1 else "canciones"
        maturity = "piloto; GO_REPLAY" if mss["completed_songs"] < 10 else "replay"
        mss_line = (
            f"- **MSS-ALT ({maturity}):** {mss['completed_songs']} {noun}, "
            f"WER nativo {_pct(native)}, RMS-VAD {_pct(vad)}, mejora relativa {_pct(relative)}."
        )
    markdown = f"""# Zero-touch report

## Métrica norte

- **ZTLR histórico:** {_pct(ztlr['ztlr'])} ({ztlr['zero_touch_lines']}/{ztlr['work_units']}; CI por canción {_pct(ztlr['ztlr_song_bootstrap_ci']['low'])}–{_pct(ztlr['ztlr_song_bootstrap_ci']['high'])}).
- Trabajo residual: {ztlr['category_counts'].get('timing_only', 0)} líneas solo timing, {ztlr['category_counts'].get('text_only', 0)} solo texto y {ztlr['category_counts'].get('text_and_timing', 0)} ambas.
- Minutos históricos: **no medibles**; el sistema viejo no persistía tiempo activo del editor. El before/after real sale del timer nuevo.

## Experimento central: texto confirmado → re-alineación

{chr(10).join(central_lines)}

## Encontrar el residuo

- La unión OOF actual llega a {_pct(flags['metrics']['corrected_line_recall'])} de recall, pero selecciona {int(flags['metrics']['selected_lines'])}/{flags['lines']} líneas, {_pct(flags['metrics']['flagged_audio_fraction'])} del audio.
- Agus escucharía **{flags['reviewer_projection']['seconds_flagged_per_song']:.0f} s/canción** en vez de {flags['reviewer_projection']['full_audio_seconds_per_song']:.0f} s. Aún es demasiado: T7/auto-consistencia/VAD deben reducir falsos flags sin bajar de 95%.

## Reducir errores

{mss_line}
- A2 (large-v2) y A3 (coherencia/rescoring) quedan pendientes hasta cerrar el replay A1 sobre la cohorte.
- Repetición A4 permanece descartada por prerrequisito: el error por palabra en líneas repetidas no supera materialmente al de líneas únicas.

No se entregó timing aprobado a ningún alineador, la calibración perceptual es leave-one-song-out y no hubo cambios en staging/producción.
"""
    markdown_output.parent.mkdir(parents=True, exist_ok=True)
    markdown_output.write_text(markdown, encoding="utf-8")
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    base = Path("eval/reports/baseline-2026-08-29")
    parser.add_argument("--ztlr", type=Path, default=Path("eval/runs/ztlr_baseline/report.json"))
    parser.add_argument("--flags", type=Path, default=Path("eval/runs/flag_union/report.json"))
    parser.add_argument("--mix", type=Path, default=Path("eval/runs/final_text_realign_mix/report.json"))
    parser.add_argument("--stem", type=Path, default=Path("eval/runs/final_text_realign/report.json"))
    parser.add_argument("--mss", type=Path, default=Path("eval/runs/mss_alt/large-v3-turbo/report.json"))
    parser.add_argument("--json-output", type=Path, default=base / "zero_touch_report.json")
    parser.add_argument("--markdown-output", type=Path, default=base / "ZERO_TOUCH_REPORT.md")
    args = parser.parse_args()
    result = publish(args.ztlr, args.flags, args.mix, args.stem, args.mss, args.json_output, args.markdown_output)
    print(json.dumps({"ztlr": result["north_star"]["ztlr_baseline"], "outputs": [str(args.json_output), str(args.markdown_output)]}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
