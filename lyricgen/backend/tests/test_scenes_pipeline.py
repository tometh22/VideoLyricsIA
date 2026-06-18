"""Tests de la orquestación multi-escena en pipeline.py.

Verificables sin Veo/Gemini reales: el fallback determinista de la biblia
visual y la degradación elegante de la generación de clips (si una escena
falla, se sustituye por una válida en vez de tumbar el video).
"""
import os

import pipeline


def test_visual_bible_fallback_when_gemini_unavailable(monkeypatch):
    def boom():
        raise RuntimeError("no gemini en CI")
    monkeypatch.setattr(pipeline, "_get_genai_client", boom)
    bible = pipeline._build_visual_bible(
        "una letra cualquiera", "Artista", style="neon", genre="rock")
    # Siempre devuelve las 5 claves del look book, no-vacías.
    assert set(bible) == {"world", "palette", "texture", "camera", "motif"}
    assert all(isinstance(v, str) and v.strip() for v in bible.values())
    # La paleta del fallback refleja el style elegido.
    assert "neon" in bible["palette"].lower()


def test_parse_json_object_tolerant():
    assert pipeline._parse_json_object('{"a": 1}') == {"a": 1}
    assert pipeline._parse_json_object('```json\n{"a": 2}\n```') == {"a": 2}
    assert pipeline._parse_json_object("basura sin json") is None
    assert pipeline._parse_json_object("") is None


def test_scene_clips_graceful_degradation(monkeypatch, tmp_path):
    import veo_breaker
    monkeypatch.setattr(veo_breaker, "is_open", lambda: False)

    def fake_veo(prompt, output_path, **kw):
        # verso_2 falla; el resto genera bien (escribe el archivo).
        if "verso_2" in (kw.get("cache_namespace") or ""):
            raise RuntimeError("veo boom")
        with open(output_path, "w") as f:
            f.write("clip")
        return output_path

    monkeypatch.setattr(pipeline, "_generate_veo_video", fake_veo)
    plan = {"scenes": [
        {"recurrence_key": "coro_1", "prompt": "p", "movement_style": "dinamico"},
        {"recurrence_key": "verso_1", "prompt": "p", "movement_style": "sutil"},
        {"recurrence_key": "verso_2", "prompt": "p", "movement_style": "sutil"},
    ]}
    clip_for_key = pipeline._generate_scene_clips(
        plan, str(tmp_path), artist="A", song_title="S", job_id=None)
    # Todas las escenas quedan mapeadas (la fallida reusa un clip válido).
    assert set(clip_for_key) == {"coro_1", "verso_1", "verso_2"}
    assert all(os.path.exists(p) for p in clip_for_key.values())
    # La escena fallida quedó marcada y apunta a un clip existente.
    failed = next(s for s in plan["scenes"] if s["recurrence_key"] == "verso_2")
    assert failed["status"] == "failed"


def test_scene_clips_all_fail_raises(monkeypatch, tmp_path):
    import veo_breaker
    monkeypatch.setattr(veo_breaker, "is_open", lambda: False)

    def fake_veo(prompt, output_path, **kw):
        raise RuntimeError("veo caído")

    monkeypatch.setattr(pipeline, "_generate_veo_video", fake_veo)
    plan = {"scenes": [{"recurrence_key": "coro_1", "prompt": "p", "movement_style": "x"}]}
    # Si ninguna escena genera, levanta → run_pipeline cae al fondo único.
    import pytest
    with pytest.raises(RuntimeError):
        pipeline._generate_scene_clips(plan, str(tmp_path), artist="A", song_title="S")


def test_scene_clips_breaker_open_raises(monkeypatch, tmp_path):
    import veo_breaker
    monkeypatch.setattr(veo_breaker, "is_open", lambda: True)
    plan = {"scenes": [{"recurrence_key": "coro_1", "prompt": "p", "movement_style": "x"}]}
    import pytest
    with pytest.raises(RuntimeError):
        pipeline._generate_scene_clips(plan, str(tmp_path), artist="A", song_title="S")
