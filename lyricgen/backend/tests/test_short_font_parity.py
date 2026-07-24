"""Paridad de tipografía video/short (incidente UMG Chile 2026-06-11:
"les está cambiando la letra en los shorts").

Causa: con tipografía en Auto, generate_lyric_video elegía la fuente
ADENTRO y el short recibía font=None → moviepy caía a la stencil default
de ImageMagick. Fix: _pick_concrete_font elige UNA vez (operador o pool),
persiste el id en render_params, y el short tiene cinturón anti-None.
"""
import pytest

import pipeline
from pipeline import _pick_concrete_font, _FONT_CATALOGUE


def test_operator_font_resolves_without_persisting(monkeypatch, tmp_path):
    persisted = {}
    monkeypatch.setattr("jobs.merge_render_params",
                        lambda job_id, params: persisted.update(params))
    path = _pick_concrete_font("anton", "j1", str(tmp_path), deterministic=False)
    assert path and path.endswith("Anton-Regular.ttf")
    # El operador eligió: no hay nada que persistir.
    assert persisted == {}


def test_auto_pick_persists_and_is_deterministic_for_umg(monkeypatch, tmp_path):
    persisted = {}
    monkeypatch.setattr("jobs.merge_render_params",
                        lambda job_id, params: persisted.update(params))
    job_dir = str(tmp_path / "job_x")
    p1 = _pick_concrete_font("", "j2", job_dir, deterministic=True)
    p2 = _pick_concrete_font("", "j2", job_dir, deterministic=True)
    assert p1 and p1 == p2  # mismo job_dir → misma fuente, siempre
    # El pick se persistió con un id del catálogo
    assert persisted.get("font") in {e["id"] for e in _FONT_CATALOGUE}


def test_auto_pick_random_still_from_pool(monkeypatch, tmp_path):
    monkeypatch.setattr("jobs.merge_render_params", lambda job_id, params: None)
    p = _pick_concrete_font("", "j3", str(tmp_path), deterministic=False)
    assert p in pipeline._FONT_POOL


def test_short_never_receives_none_font_regression():
    """Regresión textual: el cinturón del short debe existir — si alguien
    lo borra, font=None vuelve a llegar a moviepy y reaparece la stencil."""
    import inspect
    src = inspect.getsource(pipeline.generate_short)
    assert "if not font and _FONT_POOL:" in src
    assert "fallback determinístico del pool" in src


def test_pipeline_uses_single_pick_for_video_and_short():
    """run_pipeline y run_edit_pipeline deben elegir vía _pick_concrete_font
    (la elección única que fluye a video + short + persistencia)."""
    import inspect
    for fn in (pipeline.run_pipeline, pipeline.run_edit_pipeline):
        src = inspect.getsource(fn)
        assert "_pick_concrete_font(" in src, fn.__name__
