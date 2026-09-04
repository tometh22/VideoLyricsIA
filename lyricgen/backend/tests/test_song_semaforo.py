"""Regla del semáforo v2: rutea por hueco cantado, no por desacuerdo ni por vivo.

Por qué cambió respecto de v1 (medido sobre el holdout el 2026-09-02):

* el desacuerdo LoRA↔base ordenó al revés en vivos — "Eso Es Real" tuvo el
  desacuerdo más alto de las 30 canciones (0,874) y un WER de 0,08, mientras
  que "Pupilas Lejanas" tuvo el más bajo (0,014) y también estaba bien;
* 2 de los 4 vivos estaban bien, así que la etiqueta "vivo" desperdicia la
  mitad del pipeline pesado si se usa para ordenar;
* ``voiced_gap_s`` (voz cantada que ningún cartel reclama, VAD del stem) fue la
  única señal persistida que ordenó correctamente a los cuatro: 62,6 s en el
  único realmente malo contra 8,9 s en el bueno.

LoRA y el router están congelados: el desacuerdo se registra pero no decide.
"""
import importlib.util
from pathlib import Path


def _load():
    path = Path(__file__).parents[1] / "scripts" / "emit_song_semaforo.py"
    spec = importlib.util.spec_from_file_location("emit_song_semaforo", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _quality(**over):
    base = {
        "decision": "pass", "analysis_status": "complete",
        "unsafe_windows": [], "retry": {"windows_resolved": 0},
        "metrics": {"difficulty_router": {"score": 0.02}, "audio_coverage": 0.99,
                    "voiced_gap_s": 0.0, "is_live": False, "language": "es"},
    }
    for key, value in over.items():
        if key == "metrics":
            base["metrics"].update(value)
        else:
            base[key] = value
    return base


def test_green_requires_no_voiced_gap_full_coverage_no_windows_not_live():
    m = _load()
    assert m.song_verdict(_quality())["color"] == "green"


def test_live_is_never_green():
    m = _load()
    v = m.song_verdict(_quality(metrics={"is_live": True}))
    assert v["color"] == "red" and "live_never_green" in v["reasons"]


def test_high_voiced_gap_is_red_and_partial_band_is_yellow():
    m = _load()
    alto = m.song_verdict(_quality(metrics={"voiced_gap_s": 62.56}))
    assert alto["color"] == "red" and "voiced_gap_high" in alto["reasons"]

    medio = m.song_verdict(_quality(metrics={"voiced_gap_s": 8.86}))
    assert medio["color"] == "yellow" and "voiced_gap_partial" in medio["reasons"]


def test_disagreement_no_longer_decides_the_colour():
    """Con LoRA congelado, un desacuerdo altísimo no puede pintar de rojo."""
    m = _load()
    v = m.song_verdict(
        _quality(metrics={"difficulty_router": {"score": 0.874}}),
        {"disagreement": 0.874, "source": "paired_turbo_base_vs_lora_offline"},
    )
    assert v["color"] == "green"
    assert not any("disagreement" in reason for reason in v["reasons"])
    # Pero queda registrado, marcado como informativo.
    assert v["inputs"]["disagreement"] == 0.874
    assert v["inputs"]["disagreement_role"] == "informativo_no_decide"


def test_missing_voiced_gap_degrades():
    m = _load()
    quality = _quality()
    quality["metrics"].pop("voiced_gap_s")
    v = m.song_verdict(quality)
    assert v["color"] == "red" and "voiced_gap_missing" in v["reasons"]


def test_low_coverage_is_red_partial_is_yellow():
    m = _load()
    baja = m.song_verdict(_quality(metrics={"audio_coverage": 0.72}))
    assert baja["color"] == "red" and "coverage_low" in baja["reasons"]

    parcial = m.song_verdict(_quality(metrics={"audio_coverage": 0.95}))
    assert parcial["color"] == "yellow" and "coverage_partial" in parcial["reasons"]


def test_unsafe_windows_need_complete_replay_and_bounded_count():
    m = _load()
    pendiente = m.song_verdict(_quality(
        unsafe_windows=[{"id": "w1"}], analysis_status="pending",
    ))
    assert pendiente["color"] == "red" and "replay_not_complete" in pendiente["reasons"]

    muchas = m.song_verdict(_quality(
        unsafe_windows=[{"id": f"w{i}"} for i in range(12)],
    ))
    assert muchas["color"] == "red" and "too_many_unsafe_windows" in muchas["reasons"]

    pocas = m.song_verdict(_quality(unsafe_windows=[{"id": "w1"}]))
    assert pocas["color"] == "yellow" and "unsafe_windows_1" in pocas["reasons"]


def test_failed_decision_is_red():
    m = _load()
    v = m.song_verdict(_quality(decision="retry_failed"))
    assert v["color"] == "red" and "decision_retry_failed" in v["reasons"]


def test_rank_key_orders_by_voiced_gap_easiest_first():
    m = _load()
    facil = m.song_verdict(_quality(metrics={"voiced_gap_s": 0.0}))["rank_key"]
    dificil = m.song_verdict(_quality(metrics={"voiced_gap_s": 62.56}))["rank_key"]
    assert facil < dificil
    sin_senal = _quality()
    sin_senal["metrics"].pop("voiced_gap_s")
    assert m.song_verdict(sin_senal)["rank_key"] > dificil


def test_rule_version_is_v2():
    m = _load()
    assert m.song_verdict(_quality())["rule_version"] == "semaforo-v2"
    assert m.ACTION == "semaforo.verdict.v2"


def test_persists_risk_derived_score_during_blind_calibration():
    m = _load()
    verdict = m.song_verdict(_quality(risk=0.1234567, score=None))
    assert verdict["score"] == 87.654
    assert verdict["score_source"] == "risk_derived"
    assert verdict["risk"] == 0.123457


def test_missing_audio_hypothesis_is_always_red_and_manual():
    m = _load()
    quality = _quality()
    quality["reference_hypothesis_unavailable"] = True
    verdict = m.song_verdict(quality)
    assert verdict["color"] == "red"
    assert "reference_hypothesis_unavailable" in verdict["reasons"]
