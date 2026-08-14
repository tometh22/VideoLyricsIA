"""Regression test: the background quality retry must propagate the operator's hint.

Incident 2026-05-15: Enanitos Verdes "Amigos" — operator typed
"fogón con bosque" in the /edit "Aclarar tipo de fondo" textarea.
The first Veo render scored below the relevance gate (score < 7), so
`_ensure_background` triggered a quality retry. The retry called
`_get_unique_prompt` WITHOUT passing `background_hint`, so Gemini
regenerated the prompt from lyrics/genre alone — and silently dropped
the operator's explicit guidance. The retry landed on an iceberg scene,
which the operator then had to reject as a new edit, wasting one of the
3 allowed edits per job.

This test pins the fix by inspecting the source of `_ensure_background`:
the retry's call to `_get_unique_prompt` must include `background_hint`.
"""

import inspect

import pipeline


def test_quality_retry_propagates_background_hint():
    """The retry path in `_ensure_background` must pass `background_hint`.

    Without this, the operator's free-form "Aclarar tipo de fondo" hint
    is silently dropped on any first-attempt score below 7, and Gemini
    re-rolls from lyrics/genre alone.
    """
    src = inspect.getsource(pipeline._ensure_background)

    # Find the retry block. The marker is the log statement that
    # announces the retry; everything between that and the `continue` is
    # the retry call.
    retry_marker = "Score %s < 7 — generating new prompt and retrying VEO"
    retry_idx = src.find(retry_marker)
    assert retry_idx >= 0, (
        "Could not find the quality-retry block in _ensure_background. "
        "Did the retry message change? Update this test."
    )

    # Slice from the marker to the next `continue` — that brackets the
    # `_get_unique_prompt(...)` call that drives the retry.
    continue_idx = src.find("continue", retry_idx)
    assert continue_idx > retry_idx, (
        "Retry block must end in `continue` — control flow check"
    )
    retry_block = src[retry_idx:continue_idx]

    assert "_get_unique_prompt" in retry_block, (
        "Retry block should call _get_unique_prompt to regenerate the prompt"
    )
    assert "background_hint=background_hint" in retry_block, (
        "Retry call to _get_unique_prompt MUST pass background_hint=background_hint. "
        "Without it, the operator's free-form hint is silently dropped on retry. "
        f"Retry block was:\n{retry_block}"
    )


def test_first_attempt_passes_background_hint():
    """Defense in depth: the FIRST call to _get_unique_prompt must also
    pass the hint. Pins both call sites so a future refactor can't quietly
    regress one of them.
    """
    src = inspect.getsource(pipeline._ensure_background)

    # The first _get_unique_prompt call is the only one OUTSIDE the
    # retry block (which we already inspected above). Find the first
    # occurrence and verify it includes the hint.
    first_call_idx = src.find("_get_unique_prompt(")
    assert first_call_idx >= 0, "_ensure_background should call _get_unique_prompt"

    # The call spans multiple lines — slice ~400 chars forward to capture
    # the full arg list and the closing paren.
    first_call_block = src[first_call_idx:first_call_idx + 400]
    assert "background_hint=background_hint" in first_call_block, (
        "First call to _get_unique_prompt must pass background_hint. "
        f"Block was:\n{first_call_block}"
    )


def test_pipeline_merges_render_params_preserving_hint():
    """Regression: run_pipeline must MERGE render_params, not replace.

    Incident 2026-05-20: Amanda Pujó "Ser Anti". /variant and /edit store
    background_hint into render_params before the worker runs. The worker's
    render_params persistence used update_job(render_params={...}) with a
    dict that omitted background_hint, and update_job does a plain setattr
    (full replace) — so the hint was wiped after the first render and was
    gone on any later reaper-recovery retry. Gemini then reverted to its
    default alley cliché.

    Pin the fix by inspecting run_pipeline's source: it must read the
    existing render_params and merge, and conditionally persist
    background_hint.
    """
    src = inspect.getsource(pipeline.run_pipeline)

    # Must NOT replace wholesale with a literal dict that drops the hint.
    assert "_merged_rp" in src, (
        "run_pipeline must build a merged render_params dict, not replace it"
    )
    assert ".update(_new_rp)" in src, (
        "run_pipeline must merge the new fields over existing render_params"
    )
    # Must conditionally carry background_hint into the persisted params.
    assert 'if background_hint:' in src and '_new_rp["background_hint"] = background_hint' in src, (
        "run_pipeline must persist background_hint into render_params when present"
    )
