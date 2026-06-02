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
    # static (locked tripod phrasing instead). Case-insensitive — C2 hardening
    # uppercased the phrase ('LOCKED STATIC TRIPOD shot').
    assert "locked static tripod" in src.lower(), (
        "static branch must use 'locked static tripod' phrasing"
    )


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
    # The animado→veo downgrade is independent of the photo render. (Foto fija
    # 2026-06-02: the imagen path now renders STATIC via ffmpeg, no Ken Burns.)
    assert '_norm_move_bg in ("estatico", "sutil")' in src, (
        "estatico + sutil routing must still exist"
    )


# ---------------------------------------------------------------------------
# 5b. Calm registers → Imagen + deterministic camera (Veo won't hold the camera
#     still — measured 2026-05-22: it pushes in ~half the time even when told
#     not to). estatico=static, sutil=subtle drift, foto-parallax=lateral pan.
# ---------------------------------------------------------------------------

def test_ken_burns_accepts_lateral_and_subtle_flags():
    for fn in (pipeline._ken_burns_clip, pipeline._ken_burns_image_to_mp4):
        params = inspect.signature(fn).parameters
        assert "lateral" in params and "subtle" in params, fn.__name__


def test_ensure_background_routes_foto_to_imagen_static():
    """foto-parallax (a.k.a. "Foto fija") forces the Imagen path. Foto fija
    (2026-06-02): the still is then rendered STATIC via ffmpeg — the moviepy
    Ken Burns pan it used before OOM-killed the worker on long songs. Life/
    motion comes from the composable effect overlay, not a camera move."""
    src = inspect.getsource(pipeline._ensure_background)
    assert '_norm_move_bg == "foto-parallax"' in src
    assert 'bg_mode = "imagen"' in src
    # The imagen render is now a static ffmpeg loop, NOT the moviepy Ken Burns.
    assert "_static_image_to_mp4(" in src
    assert "_ken_burns_image_to_mp4(" not in src, (
        "the imagen background path must NOT call the moviepy Ken Burns render "
        "(it animated ~7,800 frames in Python and OOM-killed the worker)"
    )


def test_ken_burns_pan_modes_have_no_zoom():
    """lateral and subtle are pans over a FIXED inward crop. UMG-style update
    2026-05-25: lateral suma zoom-breath sutil (scale 1.15→1.22 con
    ciclo 32s) para dar profundidad sin advance-forward.

    Fix urgente 2026-05-25 (UMG reporte: 'los últimos videos salieron
    con fotos fijas'): bumpear amplitudes del subtle (0.35→0.65 horizontal,
    0.18→0.32 vertical, scale_amp 0→0.015) para que el drift sea
    PERCEPTIBLE pero todavía calmo. El 'subtle' debe verse vivo, no
    indistinguible de una still photo."""
    src = inspect.getsource(pipeline._ken_burns_clip)
    assert "make_pan_frame" in src
    pan_block = src.split("if not static and (lateral or subtle):")[1].split("\n    if static:")[0]
    # B1 hardening: scale_base + scale_amp en vez de scale fijo.
    assert "scale_base" in pan_block, "lateral must use scale_base (B1 zoom-breath)"
    assert "amp_x, amp_y = 1.0, 0.0" in pan_block, (
        "lateral must traverse full horizontal room (amp_x=1.0)"
    )
    assert "amp_x, amp_y = 0.65, 0.32" in pan_block, (
        "subtle must use bumped amplitudes (UMG fix 2026-05-25). Old values "
        "(0.35, 0.18) producían foto fija percibida — bumpead a (0.65, 0.32) "
        "para que el drift sea visible pero calmo."
    )
    # Continuous zoom-in (would advance forward) NOT allowed.
    assert "zoom_in" not in pan_block


def test_lateral_pan_is_subpixel_smooth():
    """Regression — incidente 2026-05-22: el pan lateral del fondo calmo se
    veía "super cortado" (stepping de 0-1-1-1-2-2-2-3 px/frame) porque la
    closure truncaba `(p * travel_x)` a int. A 24 fps y ~1.25 px/frame de
    travel, la mayoría de los frames consecutivos quedaban idénticos. El
    fix subpixel (AFFINE BILINEAR sobre un crop con 1 px extra) preserva el
    desplazamiento fraccional y produce frames TODOS distintos.

    Test: sample 12 frames a lo largo de un pan de 2 s sobre una imagen de
    franjas finas (cualquier shift subpixel mueve los pesos BILINEAR) y
    verificar que ningún par consecutivo de frames es byte-igual.

    Llamamos al helper `_pan_frame_subpixel` directamente — no requerimos
    moviepy (el conftest lo stubea sin set_fps).
    """
    import numpy as np

    w, h = 1280, 720
    arr = np.zeros((h, w, 3), dtype=np.uint8)
    arr[:, ::2] = 255   # bandas verticales de 1 px cada 2 px
    # Parámetros equivalentes al path lateral (scale=1.18, amp_x=1.0).
    scale = 1.18
    cw = int(w / scale)
    ch = int(h / scale)
    travel_x = w - cw          # full horizontal room
    travel_y = 0
    base_x = (w - cw) // 2 - travel_x // 2
    base_y = (h - ch) // 2
    duration = 2.0
    fps = 24
    frames = []
    for i in range(0, int(duration * fps), 2):
        t = i / fps
        frames.append(pipeline._pan_frame_subpixel(
            arr, t, duration,
            base_x=base_x, base_y=base_y, cw=cw, ch=ch,
            travel_x=travel_x, travel_y=travel_y,
            dir_x=1, dir_y=1,
            out_w=1920, out_h=1080,
        ))
    identical = sum(
        1 for a, b in zip(frames, frames[1:])
        if np.array_equal(a, b)
    )
    assert identical == 0, (
        f"{identical}/{len(frames)-1} pares de frames consecutivos son "
        "byte-iguales — el pan se está cuantizando a int (stepping)."
    )


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


# ---------------------------------------------------------------------------
# 7. forward-travel legibility cap (UMG 2026-05-21: "marean cuando lees la letra")
# ---------------------------------------------------------------------------

def test_motion_camera_pool_has_no_forward_travel():
    """The lyrics are overlaid, so the fallback MOTION pool must never advance
    toward the subject — only lateral / orbit / vertical / parallax moves."""
    forward_words = ("dolly forward", "push-in", "push in", "flyover",
                     "fly-through", "flythrough", "glide forward", "forward")
    for cam in pipeline._BG_CAMERAS_MOTION:
        low = cam.lower()
        assert not any(w in low for w in forward_words), (
            f"motion camera entry must not advance toward the subject: {cam!r}"
        )


def test_veo_else_branch_applies_forward_travel_cap_except_estandar():
    """Non-static, non-cartoon renders (auto/sutil/foto-parallax) must append
    the forward-travel negatives so overlaid lyrics stay readable; the explicit
    'Cinematográfico' (estandar) pick is the one opt-out."""
    src = inspect.getsource(pipeline._generate_veo_video)
    assert "_forward_travel_negative" in src
    for neg in ("no push-in", "no dolly forward", "no forward camera travel",
                "first-person glide forward"):
        assert neg in src, f"forward-travel negative missing '{neg}'"
    # estandar opts out of the cap (operator's conscious cinematic choice).
    assert '== "estandar"' in src and "_legibility_cap" in src


def test_auto_clause_forbids_forward_travel():
    """Auto register guidance must explicitly forbid forward camera travel and
    cap active motion at a gentle lateral / orbit / parallax move."""
    src = inspect.getsource(pipeline._analyze_lyrics_for_background)
    low = src.lower()
    assert "forward" in low
    assert "push-in" in low
    # The lateral cap wording for the active register.
    assert "lateral" in low


def test_verbatim_opts_out_of_debias_rails():
    """'Mi prompt manda' (UMG 2026-05-21): when the operator wrote their own
    prompt, the scene + camera de-bias rails (alley / forward-travel) are
    suppressed; only the legal IP/people rails remain."""
    sig = inspect.signature(pipeline._generate_veo_video)
    assert "verbatim" in sig.parameters
    assert sig.parameters["verbatim"].default is False
    src = inspect.getsource(pipeline._generate_veo_video)
    # Both de-bias rails honour the verbatim opt-out.
    assert 'normalized_concept == "urbano" or verbatim' in src
    assert '_norm_move == "estandar" or verbatim' in src
    # Legal rails must NOT depend on verbatim (always applied).
    assert "_base_negatives = (" in src


def test_ensure_background_threads_true_verbatim_to_veo():
    """verbatim must reflect a prompt actually in use (bg_verbatim AND a
    non-empty hint) — bg_verbatim alone with no hint falls through to Gemini,
    where the rails must still apply."""
    src = inspect.getsource(pipeline._ensure_background)
    assert "_is_verbatim" in src
    assert "bg_verbatim and background_hint" in src
    assert "verbatim=_is_verbatim" in src
