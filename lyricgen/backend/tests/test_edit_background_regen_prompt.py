"""BUG-1 / BUG-4: a background regeneration must reproduce the ORIGINAL
creative direction, not fall back to hardcoded defaults.

A movement-only edit or "Generar otra versión" (empty background bucket) sends
no fresh hint, so run_edit_pipeline's background branch used to call
_ensure_background with background_hint=None and match_lyrics defaulting to
True — discarding the persisted operator prompt and silently flipping "Auto"
(match_lyrics=False) jobs to lyrics-mode on every regen.

These are source-inspecting for the same reason as test_bg_mode_dispatch.py:
run_edit_pipeline's background generation is network-bound and not safely
mockable end-to-end. We pin the wiring so a refactor that drops the fallback
fails CI.
"""
import inspect

import pipeline


def test_regen_falls_back_to_persisted_prompt():
    src = inspect.getsource(pipeline.run_edit_pipeline)
    assert (
        "effective_background_hint = background_hint or _persisted_operator_prompt"
        in src
    ), (
        "run_edit_pipeline must fall back to the persisted operator prompt when "
        "this edit carries no fresh hint, else 'Generar otra versión' discards "
        "the original prompt and re-rolls from genre/concept/lyrics."
    )
    # Both the primary and the validation-retry _ensure_background calls must
    # use the effective hint (not the raw fresh-only background_hint).
    assert src.count("background_hint=effective_background_hint") >= 2, (
        "both the primary and retry _ensure_background calls must pass "
        "background_hint=effective_background_hint"
    )
    # allow_people on the primary call must be computed from the effective hint
    # so generation and content-gating agree.
    assert "_compute_allow_people(job_id, effective_background_hint)" in src, (
        "allow_people must be computed from effective_background_hint so a "
        "movement-only regen that reuses a people-prompt is validated WITH the "
        "same permission it generates with"
    )


def test_regen_preserves_persisted_match_lyrics():
    src = inspect.getsource(pipeline.run_edit_pipeline)
    assert 'merged.get("match_lyrics", True)' in src, (
        "run_edit_pipeline must READ the persisted match_lyrics for the regen"
    )
    assert src.count("match_lyrics=effective_match_lyrics") >= 2, (
        "both the primary and retry _ensure_background calls must pass the "
        "persisted match_lyrics; without it the branch defaults to True and "
        "silently converts 'Auto' (match_lyrics=False) jobs to lyrics-mode."
    )


def test_validation_prompt_mirrors_generation_prompt():
    """_operator_prompt_for_edit's background arm must fall back to the
    persisted prompt too, so the safety/validation prompt matches what the
    generation actually uses — no generate-with-intent / validate-without
    split that would hard-fail people-prompt reuses."""
    src = inspect.getsource(pipeline._operator_prompt_for_edit)
    assert "fresh_background_hint or persisted_operator_prompt or None" in src, (
        "the background arm must mirror generation's persisted-prompt fallback"
    )
