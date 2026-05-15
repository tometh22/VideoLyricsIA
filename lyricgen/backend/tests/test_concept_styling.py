"""Regression tests for the lyrics-anchor / concept-as-styling priority.

Background context (2026-05-15): the operator-controlled `concept` selector
used to act as a hard override in the prompt sent to Gemini. When concept
was set, the system_prompt declared "The concept choice is binding — do
NOT drift to a different visual category" and the lyrics were demoted to
"infuse details" inside the concept's vocabulary. That model wasted the
product's unique asset: a lyric video tool is the only generative video
product that has the song's actual lyrics, and we were burying them
under a generic concept vocabulary whenever concept was selected.

The fix inverts the relationship inside the `match_lyrics=True` branch:
LYRICS anchor the scene's subject, CONCEPT controls the visual styling
(palette, texture, atmosphere, register). The strict-concept opt-out
(match_lyrics=False) is unchanged — operators who want concept-only
visuals (covers, instrumentals, ironic juxtaposition) still have that
lever.

These tests pin the new priority by inspecting the source of
`_analyze_lyrics_for_background`. Source-inspection rather than full
Gemini integration tests — same pattern as `test_background_genre_bias.py`
and `test_background_hint_retry.py`.
"""

import inspect

import pipeline


def _branch_source(start_marker: str, end_marker: str) -> str:
    """Slice the source of `_analyze_lyrics_for_background` between two
    markers. Used to scope assertions to one specific branch of the
    if/elif chain.
    """
    src = inspect.getsource(pipeline._analyze_lyrics_for_background)
    start = src.find(start_marker)
    assert start >= 0, f"Could not find marker {start_marker!r} in source"
    end = src.find(end_marker, start + 1)
    assert end > start, (
        f"Could not find end marker {end_marker!r} after {start_marker!r}"
    )
    return src[start:end]


def test_concept_match_lyrics_branch_anchors_on_lyrics():
    """When concept is set AND match_lyrics is True, the system prompt
    must instruct Gemini to derive the scene SUBJECT from the lyrics and
    use the concept as the STYLING layer — not the other way around.
    """
    # Slice the `if normalized_concept:` block up to the `else:` (which
    # marks the start of the genre-only branch). The match_lyrics=True
    # branch lives inside this slice; the match_lyrics=False branch
    # (concept-only / strict mode) also lives here, but it's handled by
    # `test_concept_strict_branch_unchanged`.
    branch = _branch_source("if normalized_concept:", "elif normalized_genre:")

    # The new prompt language must reference lyrics as subject and
    # concept as styling. These exact phrases are pinned because they
    # are what makes the priority unambiguous to Gemini.
    assert "PRIMARY VISUAL SUBJECT" in branch, (
        "match_lyrics=True branch must instruct Gemini to identify the "
        "PRIMARY VISUAL SUBJECT from the lyrics. The previous 'SOUL of "
        "the song' framing left the choice of subject ambiguous and let "
        "Gemini fall back to concept vocabulary."
    )
    assert "lyrics control WHAT the scene shows" in branch, (
        "match_lyrics=True branch must include the WHAT/HOW separator "
        "line that pins lyrics to subject and concept to styling."
    )
    assert "concept controls HOW it looks" in branch, (
        "match_lyrics=True branch must explicitly state that the concept "
        "controls HOW the scene looks (not what it is)."
    )

    # The prior 'binding override' language must be gone from this branch.
    # We allow it to remain in the match_lyrics=False branch, which is
    # asserted separately below.
    pre_else, _, _ = branch.partition("        else:")
    assert "concept vocabulary is the hard boundary" not in pre_else, (
        "match_lyrics=True branch should no longer treat the concept "
        "vocabulary as a hard boundary. Letting that line survive would "
        "contradict the new STYLING role."
    )
    assert "firmly within the" not in pre_else, (
        "match_lyrics=True branch should not say the scene is 'firmly "
        "within' the concept vocabulary — that's the old binding model."
    )


def test_concept_strict_branch_unchanged():
    """The `match_lyrics=False` branch (operator opt-out: ignore lyrics)
    must keep the binding/strict language. It's the explicit opt-out
    lever for covers, instrumentals, or ironic juxtaposition.
    """
    # The match_lyrics=False branch sits between the `else:` after the
    # match_lyrics=True block and the next top-level branch
    # (`elif normalized_genre:`).
    src = inspect.getsource(pipeline._analyze_lyrics_for_background)

    # Locate the strict concept block: it's the `else:` directly inside
    # the `if normalized_concept:` block.
    concept_block_start = src.find("if normalized_concept:")
    next_top_branch = src.find("elif normalized_genre:", concept_block_start)
    concept_block = src[concept_block_start:next_top_branch]

    # The inner else: handles match_lyrics=False.
    inner_else = concept_block.find("        else:")
    assert inner_else >= 0, (
        "Could not find the match_lyrics=False branch inside the "
        "concept block. Did the structure change?"
    )
    strict_branch = concept_block[inner_else:]

    assert "Strict concept mode" in strict_branch or "no lyrics influence" in strict_branch, (
        "match_lyrics=False branch should retain its strict-mode comment "
        "so future readers know this is the opt-out path."
    )
    assert "concept choice is binding" in strict_branch, (
        "match_lyrics=False branch must keep the 'concept choice is "
        "binding' language. That's the entire point of the opt-out."
    )


def test_prompt_rules_include_lyrics_concept_hierarchy():
    """`_PROMPT_RULES` is the shared hard-rules block injected into
    every system_prompt branch. It must include the global rule that
    pins the lyrics/concept hierarchy so the rule applies consistently
    across concept+lyrics, genre-only, and auto branches.
    """
    src = inspect.getsource(pipeline._analyze_lyrics_for_background)

    # _PROMPT_RULES is a tuple of strings concatenated; the rule must
    # appear verbatim in the function's source.
    assert "LYRICS dictate the" in src, (
        "_PROMPT_RULES must include the rule pinning that lyrics dictate "
        "the subject when both concept and lyrics are present."
    )
    assert "CONCEPT dictates its visual styling" in src, (
        "_PROMPT_RULES must include the matching half of the hierarchy "
        "rule: concept dictates styling, not subject."
    )
    assert "unless match_lyrics is" in src, (
        "_PROMPT_RULES must reference match_lyrics as the opt-out lever "
        "so Gemini knows the rule has a defined exception."
    )


def test_iceberg_scene_removed_from_combinatorial_fallback():
    """The combinatorial fallback `_BG_SCENES` previously included
    'icebergs floating in arctic blue water' which produced literal
    iceberg backgrounds at 1/22 frequency whenever Gemini's JSON parse
    failed and the fallback fired. Pinned as removed because the iceberg
    scene was implicated in the Enanitos Verdes "Amigos" incident (fogón
    hint → iceberg result) and adds no coverage that 'snow falling
    gently over pine trees' or 'northern lights aurora over a mountain
    lake' don't already provide for cold imagery.
    """
    for scene in pipeline._BG_SCENES:
        assert "iceberg" not in scene.lower(), (
            f"Iceberg scene resurfaced in _BG_SCENES: {scene!r}. The "
            f"combinatorial fallback runs blind to background_hint, so "
            f"any literal iceberg scene risks reproducing the incident."
        )
        assert "arctic" not in scene.lower(), (
            f"Arctic-themed scene in _BG_SCENES: {scene!r}. Same risk "
            f"profile as the original iceberg line."
        )
