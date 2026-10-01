"""Unit tests for the local Whisper forced aligner.

Fixtures come from the real replay of staging job 18dc85ecd8d6 ("Navidad de
Aimogasta", 2026-09-15) over the same mix, with stable-ts ``base``:

  | text force-aligned onto that audio | median word prob | crammed lines |
  |------------------------------------|------------------|---------------|
  | the song's own 40-line lyric       | 0.45             | 0/40          |
  | another song (Color Esperanza)     | 0.005            | 20/38         |
  | the same lyric duplicated (80)     | 0.028            | 35/80         |

The probability floor lives far below the healthy case and above both
negative controls; the structural verdict in the caller catches them anyway.
"""
import sys
import types

import pytest

import lyrics_local_forced_align as lfa


class _FakeResult:
    def __init__(self, segments):
        self._segments = segments

    def to_dict(self):
        return {"segments": self._segments}


class _FakeModel:
    def __init__(self, segments, *, calls=None):
        self._segments = segments
        self.calls = calls if calls is not None else []

    def align(self, audio, text, **kwargs):
        self.calls.append({"audio": audio, "text": text, **kwargs})
        return _FakeResult(self._segments)


def _word(word, start, end, prob):
    return {"word": word, "start": start, "end": end, "probability": prob}


def _segment(text, start, end, probs):
    span = (end - start) / max(len(probs), 1)
    return {
        "start": start, "end": end, "text": f" {text}",
        "words": [
            _word(w, start + i * span, start + (i + 1) * span, probs[i])
            for i, w in enumerate(text.split())
        ],
    }


HEALTHY = [
    _segment("vengan pastores del campo", 11.4, 13.5, [0.9, 0.8, 0.7, 0.6]),
    _segment("que el rey de los reyes", 13.7, 15.4, [0.5, 0.6, 0.4, 0.7, 0.5, 0.6]),
    _segment("ha nacido ya", 15.5, 17.4, [0.4, 0.5, 0.45]),
]
LINES = ["vengan pastores del campo", "que el rey de los reyes", "ha nacido ya"]


@pytest.fixture
def audio(tmp_path):
    path = tmp_path / "mix.wav"
    path.write_bytes(b"RIFF")
    return str(path)


def _install(monkeypatch, model):
    module = types.ModuleType("stable_whisper")
    module.load_model = lambda name, device=None: model
    monkeypatch.setitem(sys.modules, "stable_whisper", module)
    monkeypatch.setattr(lfa, "_MODEL", None)


def test_aligns_every_line_with_word_scores(monkeypatch, audio):
    model = _FakeModel(HEALTHY)
    _install(monkeypatch, model)

    out = lfa.local_forced_align(audio, LINES, language="es", job_id="j")

    assert [segment["text"] for segment in out] == LINES
    assert [round(segment["start"], 2) for segment in out] == [11.4, 13.7, 15.5]
    # Whisper's per-word probability becomes the cascade's shared `score`, so
    # _apply can flag low-confidence lines for review exactly as it does for
    # the CTC and Whisper-DP engines.
    assert out[0]["words"][0]["score"] == 0.9
    # A forced alignment interpolates nothing — ANCHOR_MAX_INTERPOLATED_FRAC
    # is satisfied by construction, not by padding.
    assert all("interpolated" not in segment for segment in out)
    # The operator's line breaks must survive as segment boundaries.
    assert model.calls[0]["original_split"] is True
    assert model.calls[0]["language"] == "es"


def test_a_different_line_count_is_a_decline(monkeypatch, audio):
    _install(monkeypatch, _FakeModel(HEALTHY[:2]))
    assert lfa.local_forced_align(audio, LINES, language="es") is None


def test_noise_below_the_probability_floor_is_a_decline(monkeypatch, audio):
    """Color Esperanza over Aimogasta: 0.005 median. Whisper will force any
    text onto any audio, so near-zero probabilities mean it sang none of it."""
    noise = [
        _segment(text, 10.0 + i, 10.4 + i, [0.005] * len(text.split()))
        for i, text in enumerate(LINES)
    ]
    _install(monkeypatch, _FakeModel(noise))
    assert lfa.local_forced_align(audio, LINES, language="es") is None


def test_the_floor_is_env_tuneable_and_defaults_below_the_healthy_case(monkeypatch, audio):
    monkeypatch.delenv("ANCHOR_LOCAL_ALIGN_MIN_MED_PROB", raising=False)
    assert lfa.min_median_prob() == 0.05
    monkeypatch.setenv("ANCHOR_LOCAL_ALIGN_MIN_MED_PROB", "no-float")
    assert lfa.min_median_prob() == 0.05
    monkeypatch.setenv("ANCHOR_LOCAL_ALIGN_MIN_MED_PROB", "0.9")
    _install(monkeypatch, _FakeModel(HEALTHY))
    assert lfa.local_forced_align(audio, LINES, language="es") is None


def test_missing_audio_or_lines_never_loads_a_model(monkeypatch, audio, tmp_path):
    def _boom(*_a, **_kw):
        raise AssertionError("no model may be loaded without audio and text")

    module = types.ModuleType("stable_whisper")
    module.load_model = _boom
    monkeypatch.setitem(sys.modules, "stable_whisper", module)
    monkeypatch.setattr(lfa, "_MODEL", None)

    assert lfa.local_forced_align(str(tmp_path / "missing.wav"), LINES) is None
    assert lfa.local_forced_align(audio, []) is None
    assert lfa.local_forced_align(audio, ["   "]) is None


def test_an_unavailable_aligner_declines_instead_of_raising(monkeypatch, audio):
    module = types.ModuleType("stable_whisper")

    def _raise(*_a, **_kw):
        raise RuntimeError("no weights on this box")

    module.load_model = _raise
    monkeypatch.setitem(sys.modules, "stable_whisper", module)
    monkeypatch.setattr(lfa, "_MODEL", None)

    assert lfa.local_forced_align(audio, LINES, language="es") is None


def test_the_kill_switch_stops_the_stage_before_any_work(monkeypatch, audio):
    monkeypatch.setenv("ANCHOR_LOCAL_ALIGN_ENABLED", "0")
    assert lfa.is_enabled() is False
    assert lfa.local_forced_align(audio, LINES, language="es") is None
    monkeypatch.setenv("ANCHOR_LOCAL_ALIGN_ENABLED", "1")
    assert lfa.is_enabled() is True


def test_only_approved_model_identities_are_loadable(monkeypatch):
    monkeypatch.delenv("ANCHOR_LOCAL_ALIGN_MODEL", raising=False)
    assert lfa.model_name() == "base"
    monkeypatch.setenv("ANCHOR_LOCAL_ALIGN_MODEL", "small")
    assert lfa.model_name() == "small"
    monkeypatch.setenv("ANCHOR_LOCAL_ALIGN_MODEL", "/tmp/whatever.pt")
    with pytest.raises(RuntimeError):
        lfa.model_name()


def test_the_loaded_model_is_reused_across_calls(monkeypatch, audio):
    loads = []

    class _Counting(_FakeModel):
        pass

    model = _Counting(HEALTHY)
    module = types.ModuleType("stable_whisper")

    def _load(name, device=None):
        loads.append(name)
        return model

    module.load_model = _load
    monkeypatch.setitem(sys.modules, "stable_whisper", module)
    monkeypatch.setattr(lfa, "_MODEL", None)

    lfa.local_forced_align(audio, LINES, language="es")
    lfa.local_forced_align(audio, LINES, language="es")
    assert loads == ["base"]

    # Switching the model identity reloads instead of serving the old one.
    monkeypatch.setenv("ANCHOR_LOCAL_ALIGN_MODEL", "small")
    lfa.local_forced_align(audio, LINES, language="es")
    assert loads == ["base", "small"]
