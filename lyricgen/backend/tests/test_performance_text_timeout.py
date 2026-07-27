"""Cobertura para el fix del PR #730: _one_pass envuelve cada llamada a
Gemini con un timeout de 45s (pipeline._call_with_timeout).

Antes (auditado 25/06): performance_text.py llamaba a
client.models.generate_content() sin ningún timeout por llamada. Un
chunk de Gemini colgado bloqueaba el worker entero hasta el timeout
externo de 5 minutos (systemic-jobs-pipeline, mayo 2026). El PR agregó
el wrapper pero no llegó test dedicado — este archivo lo cierra.

No se puede importar performance_text.py de punta a punta en CI (trae
librosa/soundfile pesados a nivel de módulo para transcribe_performance,
pero _one_pass en sí solo necesita `client`/`genai` inyectables) así que
mockeamos ambos y monkeypatcheamos pipeline._call_with_timeout — el
import es LOCAL dentro de _one_pass (`from pipeline import
_call_with_timeout`), por lo que el patch debe ir sobre el módulo
`pipeline`, no sobre `performance_text`.
"""
import sys
import types

import numpy as np
import pytest


@pytest.fixture(autouse=True)
def _fake_soundfile(monkeypatch):
    """_one_pass hace `import soundfile as sf` y `sf.write(...)` para
    empaquetar el clip como WAV antes de mandarlo a Gemini — no nos
    importa el contenido real del buffer, solo que no truene."""
    fake_sf = types.ModuleType("soundfile")
    fake_sf.write = lambda buf, clip, sr, format="WAV": buf.write(b"RIFF....WAVEfake")
    monkeypatch.setitem(sys.modules, "soundfile", fake_sf)


def _fake_genai_and_client(response_text="[00:01.0] línea de prueba"):
    """Mínimo doble de `google.genai` + client que _one_pass necesita:
    Part.from_bytes/from_text, GenerateContentConfig, ThinkingConfig, y
    client.models.generate_content(...) -> objeto con .text ."""
    genai = types.SimpleNamespace()
    genai.types = types.SimpleNamespace(
        Part=types.SimpleNamespace(
            from_bytes=lambda data, mime_type: ("bytes", mime_type),
            from_text=lambda text: ("text", text),
        ),
        GenerateContentConfig=lambda **kw: kw,
        ThinkingConfig=lambda **kw: kw,
    )
    client = types.SimpleNamespace()
    client.models = types.SimpleNamespace(
        generate_content=lambda **kw: types.SimpleNamespace(text=response_text))
    return genai, client


def test_gemini_call_wrapped_with_45s_timeout(monkeypatch):
    """El call real (client.models.generate_content) debe pasar por
    pipeline._call_with_timeout con timeout_s=45.0 — no directo."""
    import performance_text as pt
    import pipeline

    calls = []

    def fake_call_with_timeout(fn, timeout_s, label=""):
        calls.append((timeout_s, label))
        return fn()  # camino feliz: ejecuta la llamada real (mockeada)

    monkeypatch.setattr(pipeline, "_call_with_timeout", fake_call_with_timeout)

    genai, client = _fake_genai_and_client()
    y = np.zeros(16000 * 10, dtype="float32")  # 10s de audio silencioso
    out = pt._one_pass(client, genai, y, sr=16000, dur=10.0, who="test")

    assert len(calls) == 1
    timeout_s, label = calls[0]
    assert timeout_s == 45.0
    assert "chunk" in label and "0-10" in label
    assert out == [(1.0, "línea de prueba", 0)]


def test_hung_chunk_times_out_and_counts_as_failure(monkeypatch):
    """Si pipeline._call_with_timeout dispara el timeout real (chunk
    colgado), _one_pass debe tratarlo como un fallo de ventana —no
    propagar la excepción y tumbar el worker— y seguir contando fails."""
    import performance_text as pt
    import pipeline

    def hangs_then_times_out(fn, timeout_s, label=""):
        raise TimeoutError(f"{label} exceeded {timeout_s}s")

    monkeypatch.setattr(pipeline, "_call_with_timeout", hangs_then_times_out)

    genai, client = _fake_genai_and_client()
    y = np.zeros(16000 * 10, dtype="float32")
    # dur=10s < CHUNK_S=30s → una sola ventana; con fails<4 la función
    # NO relanza, sigue el loop normalmente y devuelve items vacíos.
    out = pt._one_pass(client, genai, y, sr=16000, dur=10.0, who="test")
    assert out == []  # la ventana falló, pero el worker no se cuelga


def test_repeated_timeouts_stop_after_four_failures(monkeypatch):
    """El guard fails>=4 preexistente sigue vivo con el wrapper nuevo:
    4 chunks colgados en fila cortan el loop en vez de seguir sondeando
    Gemini para siempre."""
    import performance_text as pt
    import pipeline

    calls = []

    def always_times_out(fn, timeout_s, label=""):
        calls.append(label)
        raise TimeoutError("hung")

    monkeypatch.setattr(pipeline, "_call_with_timeout", always_times_out)

    genai, client = _fake_genai_and_client()
    # dur grande para que, sin el guard, siguiera pidiendo chunks
    # indefinidamente — con el guard corta a las 4 llamadas.
    y = np.zeros(16000 * 600, dtype="float32")
    out = pt._one_pass(client, genai, y, sr=16000, dur=600.0, who="test")
    assert out == []
    assert len(calls) == 4
