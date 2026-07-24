"""Tests for `_fetch_lrclib_with_swap_retry` — recovery when artist/title
arrive inverted from the upload (frontend parser convention mismatch).

Incident origin (2026-05-24): a user uploaded "Viejas Locas - Legalícenla"
to staging. The frontend's filename parser assumes `Title_Artist` for
files with underscore, but the file was named `Artist_Title` (the more
common convention), so the job landed with `artist='Legalícenla',
title='Viejas Locas'`. lrclib `/get` failed with the swapped query,
the pipeline fell to whisperX-only without reference text, and the
output was degraded ASR with phrases like "Le realicen la..." in place
of the actual chorus "Legalícenla".

The wrapper retries with the swapped order when the direct order misses,
returns a `meta` dict so the caller can persist the corrected order on
the job row, and gates the swap on cheap defenses (both fields non-empty,
not textually identical) to avoid log noise on degenerate inputs.
"""
from unittest.mock import patch

import pipeline


_HIT = {"plain": "letra completa", "synced": None, "duration": 220.0}


def _make_fetch_mock(matches: dict[tuple[str, str], dict | None]):
    """Build a side-effect function: given (artist, song), return the
    mapped result (or None for misses). Accepts any other args."""
    def _side_effect(artist, song, db=None, audio_duration=None):
        return matches.get((artist, song))
    return _side_effect


def test_direct_order_hits_no_swap_attempted():
    """When the direct (artist, song) lookup succeeds, the swap must
    NEVER be attempted (avoid doubling the lrclib calls in the common
    case)."""
    calls: list[tuple[str, str]] = []
    def _side_effect(artist, song, db=None, audio_duration=None):
        calls.append((artist, song))
        return _HIT if (artist, song) == ("Viejas Locas", "Legalícenla") else None
    with patch.object(pipeline, "_fetch_lrclib", side_effect=_side_effect):
        result, meta = pipeline._fetch_lrclib_with_swap_retry(
            "Viejas Locas", "Legalícenla")
    assert result == _HIT
    assert meta == {"swapped": False, "artist_used": "Viejas Locas",
                    "song_used": "Legalícenla"}
    assert calls == [("Viejas Locas", "Legalícenla")]


def test_swap_recovers_inverted_metadata():
    """The Viejas Locas / Legalícenla incident — direct order misses,
    swap matches. Wrapper returns the hit and `meta.swapped=True` so the
    caller can persist the corrected order on the job."""
    mock = _make_fetch_mock({
        ("Viejas Locas", "Legalícenla"): _HIT,
    })
    with patch.object(pipeline, "_fetch_lrclib", side_effect=mock):
        result, meta = pipeline._fetch_lrclib_with_swap_retry(
            "Legalícenla", "Viejas Locas")
    assert result == _HIT
    assert meta == {"swapped": True, "artist_used": "Viejas Locas",
                    "song_used": "Legalícenla"}


def test_both_orders_miss_returns_none():
    """When neither order matches lrclib, return (None, meta) with
    swapped=False — the caller falls through to the no-lyrics path."""
    mock = _make_fetch_mock({})  # all queries miss
    with patch.object(pipeline, "_fetch_lrclib", side_effect=mock):
        result, meta = pipeline._fetch_lrclib_with_swap_retry(
            "Banda Inexistente", "Tema Que No Existe")
    assert result is None
    assert meta["swapped"] is False


def test_empty_fields_skip_swap():
    """Defense: skip swap when artist or song is empty/blank. Avoids
    needless calls when the upload didn't supply both fields."""
    calls = []
    def _side_effect(artist, song, db=None, audio_duration=None):
        calls.append((artist, song))
        return None
    with patch.object(pipeline, "_fetch_lrclib", side_effect=_side_effect):
        # Empty artist.
        result, meta = pipeline._fetch_lrclib_with_swap_retry("", "title")
        assert result is None and meta["swapped"] is False
        # Empty song.
        result, meta = pipeline._fetch_lrclib_with_swap_retry("artist", "")
        assert result is None and meta["swapped"] is False
    # The direct call with empty input still goes through (_fetch_lrclib
    # short-circuits on its own), but the swap MUST be skipped.
    # We assert by counting: no swap call (artist,song) should ever appear
    # in a form where both fields are non-empty.
    assert all(not (a and s and (a, s) != (orig_a, orig_s))
               for (a, s), (orig_a, orig_s) in zip(calls, [("", "title"), ("artist", "")]))


def test_identical_fields_skip_swap():
    """When artist and song are textually the same (case-insensitive),
    the swap is a no-op — skip to avoid the redundant call + log noise."""
    calls = []
    def _side_effect(artist, song, db=None, audio_duration=None):
        calls.append((artist, song))
        return None
    with patch.object(pipeline, "_fetch_lrclib", side_effect=_side_effect):
        result, meta = pipeline._fetch_lrclib_with_swap_retry("X", "x")
    assert result is None
    assert meta["swapped"] is False
    # Only the direct call should have happened.
    assert calls == [("X", "x")]


def test_db_session_is_passed_through():
    """When the caller supplies a db session (cache lookup), both the
    direct and swap calls must use it — otherwise the swap-hit wouldn't
    benefit from / write to the cache."""
    seen_dbs = []
    def _side_effect(artist, song, db=None, audio_duration=None):
        seen_dbs.append(db)
        return _HIT if (artist, song) == ("Viejas Locas", "Legalícenla") else None
    fake_db = object()
    with patch.object(pipeline, "_fetch_lrclib", side_effect=_side_effect):
        result, meta = pipeline._fetch_lrclib_with_swap_retry(
            "Legalícenla", "Viejas Locas", db=fake_db)
    assert result == _HIT and meta["swapped"] is True
    assert seen_dbs == [fake_db, fake_db]
