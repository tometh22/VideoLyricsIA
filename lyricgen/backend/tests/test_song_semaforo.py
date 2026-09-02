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
                    "is_live": False, "language": "es"},
    }
    for key, value in over.items():
        if key == "metrics":
            base["metrics"].update(value)
        else:
            base[key] = value
    return base


def test_green_requires_low_disagreement_full_coverage_no_windows_not_live():
    m = _load()
    assert m.song_verdict(_quality())["color"] == "green"


def test_live_is_never_green():
    m = _load()
    v = m.song_verdict(_quality(metrics={"is_live": True}))
    assert v["color"] == "red" and "live_never_green" in v["reasons"]


def test_high_disagreement_is_red_and_ambiguous_band_is_yellow():
    m = _load()
    assert m.song_verdict(_quality(metrics={"difficulty_router": {"score": 0.09}}))["color"] == "red"
    v = m.song_verdict(_quality(metrics={"difficulty_router": {"score": 0.05}}))
    assert v["color"] == "yellow" and "disagreement_ambiguous" in v["reasons"]


def test_missing_signals_degrade_never_promote():
    m = _load()
    assert m.song_verdict({})["color"] == "red"
    assert m.song_verdict(_quality(metrics={"difficulty_router": {}}))["color"] == "red"


def test_unsafe_windows_need_complete_replay_and_bounded_count():
    m = _load()
    windows = [{"id": str(i)} for i in range(3)]
    v = m.song_verdict(_quality(decision="review_required", unsafe_windows=windows))
    assert v["color"] == "yellow" and "unsafe_windows_3" in v["reasons"]
    v = m.song_verdict(_quality(decision="review_required", unsafe_windows=windows, analysis_status="pending"))
    assert v["color"] == "red" and "replay_not_complete" in v["reasons"]
    many = [{"id": str(i)} for i in range(11)]
    assert m.song_verdict(_quality(decision="review_required", unsafe_windows=many))["color"] == "red"


def test_low_coverage_is_red_partial_is_yellow():
    m = _load()
    assert m.song_verdict(_quality(metrics={"audio_coverage": 0.85}))["color"] == "red"
    v = m.song_verdict(_quality(metrics={"audio_coverage": 0.95}))
    assert v["color"] == "yellow" and "coverage_partial" in v["reasons"]
