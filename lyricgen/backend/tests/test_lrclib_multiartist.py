"""Multi-artist lyrics-search coverage + duration guard (fix 2026-06-01).

Incident (job c57c32846d75): the cumbia cover "Luz De Dia" credited to
artist "Coti, Angela Leiva, La K´onga" shipped raw whisperX (scattered
timing, missed outro) because lrclib was queried with the FULL multi-artist
string and found nothing — even though lrclib has the original
"Coti - Luz de Día" (169 s, synced) that a per-artist query hits exactly.

These are pure unit tests (no network) on the three pieces of the fix:
  - _split_multi_artist  → splits collab credits into individual artists.
  - _lrclib_query_variants → now yields per-artist variants (Variant 4).
  - _pick_best_lrclib_candidate → duration guard picks the version that
    matches the uploaded audio and deprioritizes wrong-length records.
  - _lrclib_coverage_enabled → default ON, kill-switch via env.
"""

import pytest

from pipeline import (
    _split_multi_artist,
    _lrclib_query_variants,
    _pick_best_lrclib_candidate,
    _lrclib_coverage_enabled,
)


# ---------------------------------------------------------------------------
# _split_multi_artist
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("artist,expected", [
    ("Coti, Angela Leiva, La K´onga", ["Coti", "Angela Leiva", "La K´onga"]),
    ("J Balvin & Bad Bunny", ["J Balvin", "Bad Bunny"]),
    ("Karol G feat. Shakira", ["Karol G", "Shakira"]),
    ("Daddy Yankee ft Snow", ["Daddy Yankee", "Snow"]),
    ("Coti / Rosario", ["Coti", "Rosario"]),
])
def test_split_multi_artist_splits_collabs(artist, expected):
    assert _split_multi_artist(artist) == expected


@pytest.mark.parametrize("artist", ["Coti", "Bersuit Vergarabat", "La K´onga", ""])
def test_split_multi_artist_single_returns_empty(artist):
    """A single artist (even multi-word) has nothing to split → []."""
    assert _split_multi_artist(artist) == []


def test_split_does_not_split_x_inside_names():
    """Bare 'x' must NOT split (false-positive risk inside real names)."""
    assert _split_multi_artist("Maxx Boy") == []


# ---------------------------------------------------------------------------
# _lrclib_query_variants — Variant 4 (per-artist)
# ---------------------------------------------------------------------------

def test_variants_include_each_individual_artist():
    """The exact regression: 'Coti, Angela Leiva, La K´onga' must yield a
    variant that queries lrclib for just 'Coti' (which has the song)."""
    variants = list(_lrclib_query_variants("Coti, Angela Leiva, La K´onga", "Luz De Dia"))
    artists = {a for a, _ in variants}
    assert "Coti" in artists, f"expected a per-artist 'Coti' variant, got {artists}"
    assert "Angela Leiva" in artists
    assert "La K´onga" in artists
    # every variant keeps the song
    assert all(s for _, s in variants)


def test_variants_single_artist_unchanged_shape():
    """Single artist still yields the legacy variants without crashing."""
    variants = list(_lrclib_query_variants("Coti", "Luz De Dia"))
    assert ("Coti", "Luz De Dia") in variants


# ---------------------------------------------------------------------------
# _pick_best_lrclib_candidate — duration guard
# ---------------------------------------------------------------------------

def _cand(artist, track, duration, _id=1, synced=True):
    return {
        "id": _id,
        "trackName": track,
        "artistName": artist,
        "duration": duration,
        "syncedLyrics": "[00:10.00] x" if synced else None,
        "plainLyrics": "x",
    }


def test_duration_guard_picks_matching_version():
    """Two 'Coti - Luz de Día' records (169 s and 261 s); audio is 169 s →
    must pick the 169 s one, not whichever comes first."""
    cands = [
        _cand("Coti", "Luz de Día", 261.0, _id=1),   # original full version, listed first
        _cand("Coti", "Luz de Día", 169.0, _id=2),   # matches the cumbia audio
    ]
    best = _pick_best_lrclib_candidate(cands, "Coti", "Luz de Día", audio_duration=169.0)
    assert best is not None
    assert best["id"] == 2, "duration guard should pick the 169 s version"


def test_duration_guard_is_preference_not_hard_reject():
    """If ONLY a wrong-length version exists, still return it (better the
    right text than nothing — reconcile uses whisperX's own stamps)."""
    cands = [_cand("Coti", "Luz de Día", 261.0, _id=1)]
    best = _pick_best_lrclib_candidate(cands, "Coti", "Luz de Día", audio_duration=169.0)
    assert best is not None and best["id"] == 1


def test_without_audio_duration_behaviour_unchanged():
    """Back-compat: no audio_duration → picks by artist/song/synced only
    (first max wins), exactly as before the guard existed."""
    cands = [
        _cand("Coti", "Luz de Día", 261.0, _id=1),
        _cand("Coti", "Luz de Día", 169.0, _id=2),
    ]
    best = _pick_best_lrclib_candidate(cands, "Coti", "Luz de Día")
    assert best is not None and best["id"] == 1  # first max, no duration tiebreak


def test_far_off_version_still_loses_to_a_close_match():
    """A 334 s record (very different version) must lose to a 224 s one when
    the audio is 223 s, even if both share artist+title."""
    cands = [
        _cand("Coti", "¿Dónde Están Corazón?", 334.0, _id=1),
        _cand("Coti", "¿Dónde Están Corazón?", 224.0, _id=2),
    ]
    best = _pick_best_lrclib_candidate(cands, "Coti", "¿Dónde Están Corazón?", audio_duration=223.0)
    assert best is not None and best["id"] == 2


# ---------------------------------------------------------------------------
# _lrclib_coverage_enabled — default ON + kill switch
# ---------------------------------------------------------------------------

def test_coverage_enabled_default_on(monkeypatch):
    monkeypatch.delenv("LRCLIB_COVERAGE_BOOST", raising=False)
    assert _lrclib_coverage_enabled() is True


@pytest.mark.parametrize("val", ["0", "false", "no", "off", "OFF"])
def test_coverage_kill_switch(monkeypatch, val):
    monkeypatch.setenv("LRCLIB_COVERAGE_BOOST", val)
    assert _lrclib_coverage_enabled() is False


@pytest.mark.parametrize("val", ["1", "true", "yes", "on", ""])
def test_coverage_stays_on_for_truthy_or_empty(monkeypatch, val):
    monkeypatch.setenv("LRCLIB_COVERAGE_BOOST", val)
    assert _lrclib_coverage_enabled() is True
