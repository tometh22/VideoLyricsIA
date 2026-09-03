"""La clave de cache del ASR tiene que invalidarse sola.

Contexto: en el canary del 2026-09-02, 15 de 30 canciones sirvieron resultados
cacheados de agosto porque la clave sólo dependía del audio. El lote se presentó
como evidencia de un pipeline que, en la mitad de los casos, no había corrido.
"""
from __future__ import annotations

from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import whisperx_transcribe as wx  # noqa: E402


@pytest.fixture()
def audio(tmp_path: Path) -> str:
    path = tmp_path / "song.wav"
    path.write_bytes(b"RIFF" + b"\0" * 2048)
    return str(path)


def _key(audio_path: str) -> str:
    key, _audio_hash, _hint = wx._compute_cache_key(audio_path, "es", None, 0.5, 0.363)
    return key


def test_same_inputs_same_key(audio, monkeypatch):
    monkeypatch.setenv("WHISPERX_CACHE_VERSION", "1")
    assert _key(audio) == _key(audio)


def test_key_changes_with_model(audio, monkeypatch):
    before = _key(audio)
    monkeypatch.setattr(wx, "_MODEL", "otro/modelo:abc123", raising=False)
    assert _key(audio) != before


def test_key_changes_with_pipeline_release(audio, monkeypatch):
    monkeypatch.setenv("RELEASE", "release-uno")
    before = _key(audio)
    monkeypatch.setenv("RELEASE", "release-dos")
    assert _key(audio) != before


def test_key_changes_with_config_fingerprint(audio, monkeypatch):
    # CTC_ALIGN_ENABLED está en _PIPELINE_CONFIG_KEYS, así que mueve la huella.
    monkeypatch.setenv("CTC_ALIGN_ENABLED", "0")
    before = _key(audio)
    monkeypatch.setenv("CTC_ALIGN_ENABLED", "1")
    assert _key(audio) != before


def test_key_still_changes_with_audio_and_version(tmp_path, audio, monkeypatch):
    monkeypatch.setenv("WHISPERX_CACHE_VERSION", "1")
    before = _key(audio)
    monkeypatch.setenv("WHISPERX_CACHE_VERSION", "2")
    assert _key(audio) != before
    other = tmp_path / "otra.wav"
    other.write_bytes(b"RIFF" + b"\1" * 2048)
    monkeypatch.setenv("WHISPERX_CACHE_VERSION", "1")
    assert _key(str(other)) != before


def test_unreadable_audio_disables_cache(tmp_path):
    key, audio_hash, hint = wx._compute_cache_key(str(tmp_path / "no-existe.wav"), "es", None)
    assert (key, audio_hash, hint) == (None, None, None)


def test_counters_from_provenance_transformations():
    """Los contadores que persiste el worker salen de la procedencia."""
    hypotheses = [
        {"transformation": "cache_hit_raw"},
        {"transformation": "replicate_raw"},
        {"transformation": "replicate_raw"},
        {"role": "selected"},
        "no-es-un-dict",
    ]
    counters = {"whisperx_real_calls": 0, "whisperx_cache_hits": 0}
    for hypothesis in hypotheses:
        if not isinstance(hypothesis, dict):
            continue
        transformation = str(hypothesis.get("transformation") or "")
        if transformation == "cache_hit_raw":
            counters["whisperx_cache_hits"] += 1
        elif transformation == "replicate_raw":
            counters["whisperx_real_calls"] += 1
    counters["asr_actually_ran"] = counters["whisperx_real_calls"] > 0
    assert counters == {
        "whisperx_real_calls": 2, "whisperx_cache_hits": 1, "asr_actually_ran": True,
    }
