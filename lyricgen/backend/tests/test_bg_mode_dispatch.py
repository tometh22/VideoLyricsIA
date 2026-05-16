"""Background generation mode dispatch tests.

2026-05-16: cabled the previously-dead Imagen-4 path so the operator
can pick "imagen" (Imagen + Ken Burns) instead of the default "veo"
(Veo 3.1 cinematic video) when regenerating a background. These tests
pin the wiring so a future refactor that drops the bg_mode parameter
from any of the three layers (API → pipeline → background generator)
fails CI.

Why these tests are source-inspecting rather than runtime: the actual
Imagen / Veo API calls are network-bound and not safely mockable end-
to-end from a unit test. We trade live coverage for structural
guarantees that the dispatch logic is wired correctly through the
three layers.
"""
import inspect

import pipeline


def test_ensure_background_has_imagen_branch():
    """`_ensure_background` must branch on bg_mode to dispatch Imagen-4
    when the operator picks "imagen". The Imagen branch should call
    _generate_imagen_image (produces .jpg) and _ken_burns_image_to_mp4
    (turns the still into an animated MP4 the rest of the pipeline can
    palindrome-loop like a Veo output).
    """
    src = inspect.getsource(pipeline._ensure_background)

    # Signature includes bg_mode
    assert "bg_mode" in src.split("def ")[0] or "bg_mode" in src[:600], (
        "_ensure_background must accept a bg_mode parameter"
    )

    # Branch exists
    assert 'bg_mode == "imagen"' in src or "bg_mode==\"imagen\"" in src, (
        "_ensure_background must branch on bg_mode == 'imagen'. "
        "Without this, the operator's mode pick is silently ignored "
        "and Veo runs regardless."
    )

    # Imagen call site exists
    assert "_generate_imagen_image(" in src, (
        "Imagen branch must invoke _generate_imagen_image to produce "
        "the still .jpg"
    )

    # Ken Burns wrapper turns the still into MP4 matching downstream's
    # palindrome-loop contract
    assert "_ken_burns_image_to_mp4(" in src, (
        "Imagen branch must invoke _ken_burns_image_to_mp4 to convert "
        "the still into an MP4 — without this, the cached bg layer "
        "(bg_r2_key_cached) breaks for subsequent typography/lyrics "
        "edits that assume .mp4 cache files."
    )


def test_imagen_branch_uses_imagen_aware_prompt():
    """The Imagen branch must call _get_unique_prompt with
    for_provider="imagen" so Gemini strips motion descriptors. Without
    this, the same Veo-style prompt (camera moves, action verbs) goes
    to Imagen-4 which renders frozen-mid-action poses that look weird
    under local Ken Burns animation.
    """
    src = inspect.getsource(pipeline._ensure_background)

    # Find the Imagen branch and verify it carries for_provider=imagen.
    imagen_idx = src.find('bg_mode == "imagen"')
    assert imagen_idx > 0, "Imagen branch must exist"

    # The branch's _get_unique_prompt call should be within ~600 chars
    # of the branch marker.
    branch_block = src[imagen_idx:imagen_idx + 600]
    assert "_get_unique_prompt(" in branch_block, (
        "Imagen branch must call _get_unique_prompt"
    )
    assert 'for_provider="imagen"' in branch_block, (
        "Imagen branch's _get_unique_prompt call MUST pass "
        "for_provider='imagen' so Gemini emits a still-image-optimized "
        "prompt (no motion words). Without it, the prompt is "
        "Veo-flavored and Imagen renders awkward frozen action."
    )


def test_run_edit_pipeline_propagates_background_mode():
    """`run_edit_pipeline` must extract background_mode from edit_params
    and forward it to _ensure_background. The default when unset is
    'veo' so pre-2026-05-16 edits that never carried this field still
    work identically.
    """
    src = inspect.getsource(pipeline.run_edit_pipeline)

    # Extract from edit_params with veo default
    assert 'edit_params.get("background_mode")' in src, (
        "run_edit_pipeline must read background_mode from edit_params"
    )
    assert '"veo"' in src and "background_mode" in src, (
        "background_mode must default to 'veo' when missing from "
        "edit_params (backward compat with pre-2026-05-16 enqueued jobs)"
    )

    # Forward to _ensure_background as bg_mode kwarg
    assert "bg_mode=background_mode" in src, (
        "run_edit_pipeline's _ensure_background call MUST pass "
        "bg_mode=background_mode. Without this, the operator's mode "
        "pick reaches edit_params but is silently dropped before "
        "dispatch — defeats the whole feature."
    )


def test_get_unique_prompt_accepts_for_provider():
    """`_get_unique_prompt` must accept and forward for_provider so the
    Imagen branch's prompt synthesis differs from the Veo branch's.
    Without this, prompts come out identical regardless of target.
    """
    sig = inspect.signature(pipeline._get_unique_prompt)
    assert "for_provider" in sig.parameters, (
        "_get_unique_prompt must accept a for_provider parameter "
        "('veo' | 'imagen')"
    )
    default = sig.parameters["for_provider"].default
    assert default == "veo", (
        f"for_provider default must be 'veo' for backward compat; "
        f"got {default!r}"
    )

    src = inspect.getsource(pipeline._get_unique_prompt)
    assert "for_provider=for_provider" in src or "for_provider = for_provider" in src, (
        "_get_unique_prompt must forward for_provider to "
        "_analyze_lyrics_for_background. Without this, the Imagen-aware "
        "system prompt addendum never fires."
    )


def test_ken_burns_image_to_mp4_writes_file():
    """`_ken_burns_image_to_mp4` is the wrapper that satisfies the
    contract `_ensure_background` returns to its callers (a path to
    an .mp4 file). Pin the function exists and writes via moviepy.
    """
    # Function exists
    assert hasattr(pipeline, "_ken_burns_image_to_mp4"), (
        "_ken_burns_image_to_mp4 wrapper must exist to bridge the "
        "Imagen still → Ken Burns animation → downstream MP4 contract"
    )

    src = inspect.getsource(pipeline._ken_burns_image_to_mp4)

    # Uses the existing _ken_burns_clip to produce the animation
    assert "_ken_burns_clip(" in src, (
        "_ken_burns_image_to_mp4 must reuse _ken_burns_clip rather than "
        "reimplementing the animation"
    )

    # Writes via moviepy's write_videofile
    assert ".write_videofile(" in src, (
        "_ken_burns_image_to_mp4 must call write_videofile to flush "
        "the moviepy clip to disk as an .mp4"
    )


def test_analyze_lyrics_adds_imagen_addendum():
    """When for_provider='imagen', _analyze_lyrics_for_background must
    inject an addendum to the system prompt that tells Gemini to strip
    motion descriptors. The addendum lives in the function body, only
    appended when for_provider == "imagen".
    """
    src = inspect.getsource(pipeline._analyze_lyrics_for_background)

    assert 'for_provider == "imagen"' in src, (
        "_analyze_lyrics_for_background must branch on "
        "for_provider == 'imagen' to inject the Imagen system addendum"
    )
    assert "PROVIDER OVERRIDE" in src or "Imagen-4" in src, (
        "Addendum should clearly mark itself in the system prompt so "
        "Gemini understands the override (and so future debugging "
        "can spot the addendum in logged prompts)"
    )
