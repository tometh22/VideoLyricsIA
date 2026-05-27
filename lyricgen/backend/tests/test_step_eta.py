"""Tests para step_eta.compute_eta_s y format_eta_es."""
from __future__ import annotations

import sys
import os

BACKEND = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)

import pytest  # noqa: E402

from step_eta import (  # noqa: E402
    compute_eta_s,
    format_eta_es,
    STEP_TYPICAL_DURATIONS_S,
    STEP_ORDER,
    STEP_PROGRESS_RANGES,
    STEP_USER_TEXT_ES,
)


# ──────────────────────────────────────────────────────────────────────
# compute_eta_s — happy path per step
# ──────────────────────────────────────────────────────────────────────

def test_eta_for_starting_step():
    """At the very start, ETA should be ~sum of all steps."""
    eta = compute_eta_s("starting", 1)
    total = sum(STEP_TYPICAL_DURATIONS_S.values())
    assert total - 5 <= eta <= total


def test_eta_for_background_step_at_start():
    """Entering background, full background duration + subsequent steps."""
    eta = compute_eta_s("background", 22)
    expected_min = (
        STEP_TYPICAL_DURATIONS_S["background"]
        + STEP_TYPICAL_DURATIONS_S["validation"]
        + STEP_TYPICAL_DURATIONS_S["video"]
        + STEP_TYPICAL_DURATIONS_S["short"]
        + STEP_TYPICAL_DURATIONS_S["thumbnail"]
        + STEP_TYPICAL_DURATIONS_S["upload"]
    )
    assert eta == expected_min


def test_eta_decreases_as_progress_advances():
    """While the worker crawls progress inside background, ETA drops."""
    eta_at_start = compute_eta_s("background", 22)
    eta_at_mid = compute_eta_s("background", 31)
    eta_at_near_end = compute_eta_s("background", 38)
    assert eta_at_start > eta_at_mid > eta_at_near_end


def test_eta_for_upload_last_step():
    """At the last step, ETA only includes the remaining of that step."""
    eta_start = compute_eta_s("upload", 95)
    eta_mid = compute_eta_s("upload", 97)
    assert eta_start == STEP_TYPICAL_DURATIONS_S["upload"]
    assert eta_mid < eta_start


def test_eta_returns_none_for_unknown_step():
    assert compute_eta_s("astral_projection", 50) is None
    assert compute_eta_s("", 50) is None
    assert compute_eta_s(None, 50) is None


def test_eta_clamps_out_of_range_progress():
    """Progress beyond step end clamps to step end (0 remaining in step)."""
    eta_at_50_in_starting = compute_eta_s("starting", 50)  # past end of "starting" (1-5)
    # Should not be negative; only subsequent steps' durations.
    expected_subsequent = sum(
        STEP_TYPICAL_DURATIONS_S[s] for s in STEP_ORDER[1:]
    )
    assert eta_at_50_in_starting == expected_subsequent


def test_eta_handles_none_progress():
    """If progress is None, treat as 'just started this step'."""
    eta = compute_eta_s("background", None)
    # Should equal full typical of background + subsequent.
    assert eta == compute_eta_s("background", 22)


def test_eta_handles_garbage_progress_value():
    """Non-int progress doesn't crash, treated as start of step."""
    eta = compute_eta_s("background", "nope")  # type: ignore[arg-type]
    assert eta == compute_eta_s("background", 22)


# ──────────────────────────────────────────────────────────────────────
# format_eta_es — display formatting
# ──────────────────────────────────────────────────────────────────────

def test_format_none_returns_none():
    assert format_eta_es(None) is None


def test_format_zero_says_almost_done():
    assert format_eta_es(0) == "Casi listo…"
    assert format_eta_es(-5) == "Casi listo…"


def test_format_under_90s_shows_seconds():
    assert format_eta_es(45) == "~45 seg restantes"
    assert format_eta_es(89) == "~89 seg restantes"


def test_format_over_90s_shows_minutes_rounded():
    # Formula: minutes = (s + 30) // 60 → round-half-up to nearest minute.
    # 91s → (91+30)//60 = 2; 119s → 2; 120s → 2.
    assert format_eta_es(91) == "~2 min restantes"
    assert format_eta_es(119) == "~2 min restantes"
    assert format_eta_es(120) == "~2 min restantes"
    # 180s → (180+30)//60 = 3.
    assert format_eta_es(180) == "~3 min restantes"


def test_format_large_eta_rounding():
    """Verify the rounding formula: minutes = (s + 30) // 60."""
    # 270s → (270+30)//60 = 5 → "~5 min restantes"
    assert format_eta_es(270) == "~5 min restantes"
    # 300s → (300+30)//60 = 5 → "~5 min restantes"
    assert format_eta_es(300) == "~5 min restantes"
    # 330s → (330+30)//60 = 6 → "~6 min restantes"
    assert format_eta_es(330) == "~6 min restantes"


# ──────────────────────────────────────────────────────────────────────
# Sanity: dictionaries are well-formed
# ──────────────────────────────────────────────────────────────────────

def test_all_steps_have_durations():
    """Every step in STEP_ORDER must have a typical duration."""
    for step in STEP_ORDER:
        assert step in STEP_TYPICAL_DURATIONS_S, f"missing duration for {step!r}"
        assert STEP_TYPICAL_DURATIONS_S[step] > 0


def test_all_steps_have_progress_range():
    """Every step in STEP_ORDER must have a progress range."""
    for step in STEP_ORDER:
        assert step in STEP_PROGRESS_RANGES
        p_start, p_end = STEP_PROGRESS_RANGES[step]
        assert 0 <= p_start <= 100
        assert p_start <= p_end <= 100


def test_all_steps_have_user_text():
    for step in STEP_ORDER:
        assert step in STEP_USER_TEXT_ES
        assert STEP_USER_TEXT_ES[step]


def test_progress_ranges_dont_overflow():
    """All step start progresses should be ≤ end. Allow overlap (e.g.
    validation 38-40 overlaps background 22-40) — those are short
    sub-steps within the bar."""
    for step in STEP_ORDER:
        p_start, p_end = STEP_PROGRESS_RANGES[step]
        assert p_start <= p_end


# ──────────────────────────────────────────────────────────────────────
# Specific regression: the "638" / "8 min restantes" scenario
# ──────────────────────────────────────────────────────────────────────

def test_638_scenario_eta_changes_visibly():
    """The user's "638" job in background should have a CHANGING ETA as
    the sub-progress crawl ticks. Before this PR the ETA was hardcoded
    to "~8 min restantes". Now compute_eta_s + the worker's crawl
    produce different values at different progress points."""
    eta_just_entered = compute_eta_s("background", 22)
    eta_halfway = compute_eta_s("background", 30)
    eta_almost_done = compute_eta_s("background", 38)
    assert eta_just_entered > eta_halfway > eta_almost_done
    # Display strings should also differ.
    assert format_eta_es(eta_just_entered) != format_eta_es(eta_almost_done)
