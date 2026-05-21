"""Camera-motion control + de-bias regression tests.

Covers the feature set that removed the built-in camera-motion bias and
gave operators real control:
  - the new "estatico" (locked camera) movement preset + aliases
  - the static-aware camera clause in the Gemini system prompt
  - the static-aware safe_prompt branch + camera-motion negatives in Veo
  - verbatim mode ("usar mi prompt tal cual") bypassing the Gemini rewrite
  - the static Ken Burns variant for the Imagen path
  - the de-biased combinatorial fallback camera pool

Most assertions are on source/structure or pure functions so they run
without network or a real genai client — matching the style of
test_background_genre_bias.py.
"""
import inspect

import pipeline


# ---------------------------------------------------------------------------
# 1. estatico preset + normalization
# ---------------------------------------------------------------------------

def test_estatico_is_a_registered_movement_style():
    assert "estatico" in pipeline._MOVEMENT_STYLE_RULES
    rule = pipeline._MOVEMENT_STYLE_RULES["estatico"].lower()
    # The rule must forbid camera movement explicitly.
    for forbidden in ("no pan", "no zoom", "no dolly", "static"):
        assert forbidden in rule, f"estatico rule missing '{forbidden}'"


def test_normalize_movement_style_maps_static_aliases():
    for alias in ("static", "estatica", "fija", "fixed", "tripod", "locked", "still"):
        assert pipeline._normalize_movement_style(alias) == "estatico", alias
    # Exact key passes through.
    assert pipeline._normalize_movement_style("estatico") == "estatico"
    # Unrelated input still resolves to its own bucket / empty.
    assert pipeline._normalize_movement_style("animated") == "animado"
    assert pipeline._normalize_movement_style("") == ""


# ---------------------------------------------------------------------------
# 2. static-aware camera clause in the Gemini system prompt
# ---------------------------------------------------------------------------

def test_camera_mandate_is_conditional_not_hardcoded():
    """The old code hardcoded '(2) exact camera movement and framing' in
    three places, forcing motion on every prompt. It must now route through
    the _clause2 variable so static/auto can override it."""
    src = inspect.getsource(pipeline._analyze_lyrics_for_background)
    # The static branch wording must exist.
    assert "_static" in src
    assert "LOCKED STATIC" in src
    # The unconditional mandate must no longer appear verbatim in the
    # f-string rule bodies (it now lives only inside the else of _clause2).
    assert src.count("exact camera movement and framing") <= 1, (
        "the camera-movement mandate should only survive as the non-static "
        "fallback inside _clause2, not as repeated hardcoded copies"
    )
    # Both rule blocks must interpolate the computed clause.
    assert "{_clause2}" in src


def test_auto_movement_varies_register_instead_of_forcing_drift():
    src = inspect.getsource(pipeline._analyze_lyrics_for_background)
    assert "_auto_movement" in src
    # The auto example block must instruct varying the register and forbid a
    # constant cinematic drift default (the monotony fix).
    assert "CAMERA REGISTER" in src
    assert "do NOT default to" in src.lower() or "do not default to" in src.lower()


# ---------------------------------------------------------------------------
# 3. static safe_prompt + camera negatives in Veo
# ---------------------------------------------------------------------------

def test_generate_veo_video_has_static_branch_with_camera_negatives():
    src = inspect.getsource(pipeline._generate_veo_video)
    assert '== "estatico"' in src, "Veo must branch on the static register"
    # The static branch must append explicit camera-motion negatives.
    for neg in ("no pan", "no tilt", "no zoom", "no dolly", "no camera movement"):
        assert neg in src, f"static safe_prompt missing negative '{neg}'"
    # And drop the motion-suggesting 'filmed with cinema camera' phrasing for
    # static (locked tripod phrasing instead).
    assert "locked static tripod shot" in src


def test_generate_veo_video_accepts_high_fidelity_and_routes_model():
    sig = inspect.signature(pipeline._generate_veo_video)
    assert "high_fidelity" in sig.parameters
    src = inspect.getsource(pipeline._generate_veo_video)
    # Static / verbatim renders can route to a higher-fidelity model via env.
    assert "VEO_MODEL_STATIC" in src


# ---------------------------------------------------------------------------
# 4. verbatim mode bypasses Gemini
# ---------------------------------------------------------------------------

def test_get_unique_prompt_signature_has_bg_verbatim():
    sig = inspect.signature(pipeline._get_unique_prompt)
    assert "bg_verbatim" in sig.parameters
    assert sig.parameters["bg_verbatim"].default is False


def test_verbatim_returns_hint_unchanged_without_calling_gemini(monkeypatch):
    """bg_verbatim=True must short-circuit before the Gemini analysis and
    return the operator's exact text. This is the fix for hand-written
    'Static tripod, no camera motion…' prompts being paraphrased away."""
    called = {"gemini": False}

    def _boom(*a, **k):
        called["gemini"] = True
        raise AssertionError("_analyze_lyrics_for_background must NOT be called in verbatim mode")

    monkeypatch.setattr(pipeline, "_analyze_lyrics_for_background", _boom)

    hint = ("Decadent surreal mansion at night, empty pool, static tripod, "
            "absolutely no camera motion, no pan, no zoom.")
    result = pipeline._get_unique_prompt(
        lyrics_text="algunas palabras de la canción",
        artist="Test", song_title="Song",
        background_hint=hint, bg_verbatim=True,
    )
    assert result["prompt"] == hint
    assert result["style"] == "video"
    assert called["gemini"] is False


def test_verbatim_imagen_returns_image_style(monkeypatch):
    monkeypatch.setattr(
        pipeline, "_analyze_lyrics_for_background",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("no gemini")),
    )
    result = pipeline._get_unique_prompt(
        background_hint="static mansion", bg_verbatim=True, for_provider="imagen",
    )
    assert result["style"] == "image"
    assert result["prompt"] == "static mansion"


def test_ensure_background_gates_quality_retry_off_for_verbatim():
    src = inspect.getsource(pipeline._ensure_background)
    assert "not bg_verbatim" in src, (
        "quality-retry must be gated off for verbatim, else it re-rolls the "
        "same text → Veo cache hit → wasted scoring pass"
    )


# ---------------------------------------------------------------------------
# 5. Imagen + estatico → static Ken Burns; animado → veo
# ---------------------------------------------------------------------------

def test_ken_burns_clip_accepts_static_flag():
    sig = inspect.signature(pipeline._ken_burns_clip)
    assert "static" in sig.parameters


def test_ensure_background_downgrades_animado_imagen_to_veo():
    src = inspect.getsource(pipeline._ensure_background)
    assert '_norm_move_bg == "animado"' in src
    # Static Imagen path holds the frame.
    assert 'static=(_norm_move_bg == "estatico")' in src


# ---------------------------------------------------------------------------
# 6. de-biased combinatorial fallback
# ---------------------------------------------------------------------------

def test_edit_always_overwrites_bg_verbatim_for_background():
    """Regression: bg_verbatim was a sticky flag — persisted only when True,
    so unchecking the toggle on a later background edit couldn't clear it
    (merged render_params kept the stale True). The handler must ALWAYS write
    the boolean for background edits, symmetric with movement_style."""
    import inspect
    import main
    src = inspect.getsource(main.request_edit) if hasattr(main, "request_edit") else None
    if src is None:
        # Endpoint name may differ; fall back to scanning the module source.
        src = inspect.getsource(main)
    # Must persist bool(body.bg_verbatim), not gate on `and body.bg_verbatim`.
    assert "bool(body.bg_verbatim)" in src
    assert 'if body.edit_type == "background" and body.bg_verbatim:' not in src


def test_retry_whitelist_includes_bg_verbatim():
    """Regression: /retry dropped bg_verbatim, so a reaped verbatim job lost
    it on recovery — defeating the render_params persistence."""
    import inspect
    import main
    src = inspect.getsource(main)
    # bg_verbatim must be forwarded by the retry kwargs whitelist.
    assert '"bg_verbatim"' in src


def test_color_directive_auto_imposes_nothing():
    """Auto / empty palette must NOT constrain colors (scene-natural)."""
    assert pipeline._color_directive("auto", "") == ""
    assert pipeline._color_directive("", "") == ""


def test_color_directive_preset_and_custom():
    # Preset → mood hint.
    d = pipeline._color_directive("oscuro", "")
    assert "COLOR DIRECTION" in d and "purple" in d.lower()
    # Custom colors win over preset and are passed through verbatim.
    d2 = pipeline._color_directive("oscuro", "#ff3366, teal")
    assert "#ff3366, teal" in d2 and "dominant colors" in d2.lower()


def test_color_threads_into_analyze_signature():
    import inspect
    sig = inspect.signature(pipeline._analyze_lyrics_for_background)
    assert "style" in sig.parameters and "custom_colors" in sig.parameters
    # And the Gemini prompt builder actually appends the directive.
    src = inspect.getsource(pipeline._analyze_lyrics_for_background)
    assert "_color_directive(" in src


def test_static_camera_pool_has_no_motion_verbs():
    motion_words = ("dolly", "drone", "tracking", "orbit", "zoom", "pan",
                    "crane", "parallax", "glide", "push-in")
    for cam in pipeline._BG_CAMERAS_STATIC:
        low = cam.lower()
        assert any(w in low for w in ("static", "fixed", "held", "locked")), cam
        assert not any(w in low for w in motion_words), (
            f"static camera entry must not contain a motion verb: {cam!r}"
        )
