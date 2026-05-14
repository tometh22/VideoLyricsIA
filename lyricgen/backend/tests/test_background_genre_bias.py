"""Regression tests for the "callejón / alley" bias in background generation.

UMG 2026-05-14: ~80% of rock tracks were rendering with neon-lit alleyways
as the background. Root causes:
  - _GENRE_SCENE_GUIDE["rock"] was 100% urban/alley vocabulary
  - The "rock example" in Gemini's system prompt also led with neon streets
  - Auto-classification block repeated the same urban-only mapping
  - safety prompt for Veo did NOT prohibit alleys
  - lyrics_text truncated at 600 chars + thinking_budget=0 meant Gemini
    often never read the lyrics deep enough to find a visual subject and
    fell back to the genre-vocab default

These tests pin the fix so the bias doesn't silently regress on a future
prompt-engineering tweak.
"""

import inspect

import pipeline


def test_rock_genre_guide_is_not_alley_dominant():
    """_GENRE_SCENE_GUIDE['rock'] must offer multiple non-alley settings.

    Before fix: 'Urban industrial streets, neon-lit alleyways, gritty rain
    on asphalt, smoke rising past dim streetlamps, ...'. After fix: a mix
    of concert smoke, stormy highway, mountain storms, vintage amps, etc.
    """
    rock_vocab = pipeline._GENRE_SCENE_GUIDE["rock"].lower()
    diverse_keywords = (
        "concert", "stage", "highway", "mountain", "lightning", "storm",
        "amplifier", "guitar", "arena", "plains",
    )
    matches = [k for k in diverse_keywords if k in rock_vocab]
    assert len(matches) >= 4, (
        f"rock vocab should contain at least 4 non-alley settings, "
        f"found only: {matches}. Full vocab: {rock_vocab[:200]}"
    )
    # Sanity: alley is still allowed as a marginal option, but the vocab
    # must explicitly call out that it is NOT the default.
    assert "not the default" in rock_vocab or "one option among many" in rock_vocab, (
        "rock vocab must explicitly de-prioritize alleyways"
    )


def test_system_prompt_rock_example_is_not_alley():
    """Gemini imitates the first matching example when it's unsure. The
    'rock' example must not be a neon-lit alleyway; otherwise prompt-bleed
    pushes most rock tracks toward callejón.

    We extract the rock example block specifically (between
    'Example for rock' and the next 'Example for') — other parts of the
    source may legitimately mention 'rain-slicked' (the GENRE-TONE
    COHERENCE guardrail explicitly DOES, telling Gemini NOT to default
    to it, and _CONCEPT_SCENE_GUIDE['urbano'] also uses it on purpose).
    """
    src = inspect.getsource(pipeline._analyze_lyrics_for_background)

    # Slice from the rock example header to the next "Example for".
    rock_idx = src.find("Example for rock")
    assert rock_idx >= 0, "system prompt should still contain a rock example"
    next_idx = src.find("Example for", rock_idx + 1)
    rock_block = src[rock_idx:next_idx if next_idx > rock_idx else rock_idx + 400]

    assert "neon-lit rain-slicked" not in rock_block.lower(), (
        f"rock example still uses 'neon-lit rain-slicked'. Block: {rock_block[:300]}"
    )
    assert "rain-slicked streets" not in rock_block.lower(), (
        f"rock example still uses 'rain-slicked streets'. Block: {rock_block[:300]}"
    )
    # And the new vocabulary should be in the rock example (highway / lightning).
    assert "highway" in rock_block.lower() or "lightning" in rock_block.lower(), (
        f"rock example should mention highway/lightning (the rebalanced "
        f"scene). Block: {rock_block[:300]}"
    )


def test_auto_classification_rock_line_is_rebalanced():
    """The auto-classification block (shown to Gemini when genre and
    concept are both empty) repeats the genre vocab for each label. The
    'rock' line previously said 'urban industrial streets, neon alleyways
    ...' verbatim. The fix turns it into a diverse list."""
    src = inspect.getsource(pipeline._analyze_lyrics_for_background)
    # The line appears twice (match_lyrics True/False branches). Both must
    # have been rebalanced.
    assert src.count(
        "rock     → urban industrial streets, neon alleyways"
    ) == 0, "old rock → urban alley line still present in auto-class block"
    # And the new vocabulary keywords should be present.
    assert "concert stage smoke" in src or "stormy highways" in src, (
        "auto-class rock line should mention diverse settings (concert "
        "stage smoke, stormy highways, etc)"
    )


def test_safe_prompt_avoids_alley_when_concept_not_urbano():
    """When the operator did NOT pick concept='urbano', the safe prompt
    wrapper for Veo must include 'avoid ... alleyway' as a last-resort
    filter. Conversely, if concept='urbano', the negative is dropped so
    the operator can legitimately get a callejón when they ask for it.

    Tests this by inspecting the source of _generate_veo_video — the
    no_alley clause is a string, easy to verify without running the
    actual auth + Veo HTTP chain (which we don't want in unit tests).
    """
    src = inspect.getsource(pipeline._generate_veo_video)

    # The conditional must compare against "urbano" and produce a
    # non-empty avoid-alleyway string when concept != "urbano".
    assert "normalized_concept" in src, (
        "_generate_veo_video must accept normalized_concept param"
    )
    assert 'normalized_concept == "urbano"' in src or "normalized_concept=='urbano'" in src, (
        "no_alley logic must depend on normalized_concept != 'urbano'"
    )
    assert "Avoid generic narrow alleyway" in src or "avoid generic narrow alleyway" in src.lower(), (
        "safe_prompt should include an explicit 'Avoid ... alleyway' clause"
    )
    assert "callejón" in src or "callejon" in src, (
        "negative prompt should also mention 'callejón' (Spanish spelling)"
    )

    # And the safe_prompt for both branches (animado + photoreal) must
    # f-string the no_alley variable.
    assert src.count("{no_alley}") >= 2, (
        f"safe_prompt should interpolate no_alley in both animated and "
        f"photoreal branches; found {src.count('{no_alley}')} occurrences"
    )


def test_normalized_concept_propagated_from_caller():
    """run_pipeline → _generate_veo_video must pass normalized_concept
    so the bias-buster can fire. If the caller forgets, the no_alley
    string stays empty and the filter never applies."""
    # The caller is _generate_dynamic_video (or whatever wraps the Veo
    # call). Easier: scan the whole pipeline.py for the call site that
    # passes normalized_concept to _generate_veo_video.
    src = open(pipeline.__file__).read()
    # The call site uses _normalize_concept(concept) so a free-text input
    # like "Urban" still hits the "urbano" branch.
    assert "normalized_concept=_normalize_concept(concept)" in src, (
        "caller of _generate_veo_video should pass "
        "normalized_concept=_normalize_concept(concept)"
    )


def test_gemini_call_uses_thinking_and_wider_lyrics_window():
    """Two coupled fixes that improve Gemini's ability to find the
    primary visual subject before falling back to genre vocab:

    1. lyrics_text[:1800] (was 600 — too short to reach the chorus of a
       3-min song after the first verse + bridge)
    2. thinking_budget=512 (was 0 — without chain-of-thought Gemini
       skipped the 'STEP 0: read lyrics' instruction)

    Verifying the source of _analyze_lyrics_for_background is the
    quickest way to pin these without setting up the full Vertex client.
    """
    src = inspect.getsource(pipeline._analyze_lyrics_for_background)

    assert "lyrics_text[:600]" not in src, (
        "lyrics window still truncated at 600 chars — should be 1800"
    )
    assert "lyrics_text[:1800]" in src, (
        "lyrics window must be widened to 1800 chars"
    )

    assert "thinking_budget=0" not in src, (
        "thinking_budget is still 0 — chain-of-thought disabled, Gemini "
        "skips the STEP 0 'read lyrics' instruction"
    )
    assert "thinking_budget=512" in src, (
        "thinking_budget should be 512 (cheap chain-of-thought for "
        "visual-subject extraction)"
    )
