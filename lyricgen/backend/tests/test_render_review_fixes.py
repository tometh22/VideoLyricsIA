"""Regression tests for the bugs the adversarial review found (2026-06-03) in
the render changes. _ensure_background / _static_image_to_mp4 do heavy
ffmpeg/Imagen I/O, so these pin the fixes by inspecting the relevant source.
"""
import inspect
import pipeline


def _ensure_bg_call(fn):
    """The _ensure_background(...) argument block inside fn's source."""
    src = inspect.getsource(fn)
    i = src.index("_ensure_background(")
    return src[i:i + 800]


def test_effect_forwarded_from_run_pipeline():
    assert "effect=effect" in _ensure_bg_call(pipeline.run_pipeline)


def test_effect_forwarded_from_run_edit_pipeline():
    assert "effect=effect" in _ensure_bg_call(pipeline.run_edit_pipeline)


def test_darkening_uses_operator_effect_not_forced_light():
    src = inspect.getsource(pipeline._ensure_background)
    # captured before the anti-dead-frame "light" default
    assert "_operator_effect = (effect or" in src
    assert "_darken_prompt_for_effect(result[\"prompt\"], _operator_effect)" in src
    # the darken call must NOT pass the (possibly force-lit) `effect` var
    assert "_darken_prompt_for_effect(result[\"prompt\"], effect)" not in src


def test_darkening_skipped_for_verbatim_prompt():
    src = inspect.getsource(pipeline._ensure_background)
    assert "_verbatim_bg = bool(bg_verbatim and background_hint" in src
    assert "result[\"prompt\"] if _verbatim_bg" in src


def test_static_bg_renders_short_sample_not_full_duration():
    src = inspect.getsource(pipeline._ensure_background)
    assert "_static_image_to_mp4(image_path, bg_path, duration=60.0)" in src
    assert "duration=_bg_dur" not in src  # no second full-length encode


def test_static_image_timeout_is_900():
    src = inspect.getsource(pipeline._static_image_to_mp4)
    assert "timeout=900" in src
    assert "timeout=300" not in src
