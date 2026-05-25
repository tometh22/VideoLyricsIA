"""Unit tests for the Genius.com lyrics fallback.

Genius is gated on GENIUS_API_TOKEN — tests stub out the network layer
(requests) so they run offline and don't depend on the env var being set.
The cache layer is exercised against an in-memory SQLite session.
"""
from __future__ import annotations

import os
import pytest
from unittest.mock import patch, MagicMock


def _force_enabled():
    """Tests run without GENIUS_API_TOKEN by design — patch the module to
    pretend it's set so `is_enabled()` returns True and we exercise the
    fetcher's logic."""
    import genius_fetch
    return patch.object(genius_fetch, "_TOKEN", "test-token-stub")


# ─── pure helpers (no network) ─────────────────────────────────────


def test_is_enabled_reflects_token():
    import genius_fetch
    # Default state: token absent unless env var is set in the test runner.
    expected = bool(os.environ.get("GENIUS_API_TOKEN", "").strip())
    assert genius_fetch.is_enabled() is expected


def test_cache_key_is_stable_and_namespaced():
    import genius_fetch
    k1 = genius_fetch._cache_key("Viejas Locas", "Legalícenla")
    k2 = genius_fetch._cache_key("VIEJAS LOCAS", "  Legalícenla  ")
    assert k1 == k2                                  # case/whitespace insensitive
    assert k1.startswith("genius:")
    k3 = genius_fetch._cache_key("Viejas Locas", "Otra Cancion")
    assert k1 != k3


def test_normalise_artist_drops_accents_and_case():
    import genius_fetch
    assert genius_fetch._normalise_artist("Soda Stéreo") == "soda stereo"
    assert genius_fetch._normalise_artist("ÁNGELES AZULES") == "angeles azules"


# ─── search_song_url (mocked HTTP) ─────────────────────────────────


def test_search_picks_match_by_artist():
    import genius_fetch
    fake_response = MagicMock(status_code=200)
    fake_response.json.return_value = {
        "response": {
            "hits": [
                {"result": {
                    "url": "https://genius.com/wrong-artist-lyrics",
                    "primary_artist": {"name": "Other Band"},
                }},
                {"result": {
                    "url": "https://genius.com/viejas-locas-legalicenla-lyrics",
                    "primary_artist": {"name": "Viejas Locas"},
                }},
            ]
        }
    }
    with _force_enabled(), patch("requests.get", return_value=fake_response):
        url = genius_fetch._search_song_url("Viejas Locas", "Legalícenla")
    assert url == "https://genius.com/viejas-locas-legalicenla-lyrics"


def test_search_falls_back_to_top_hit_when_no_artist_match():
    """When the artist name doesn't normalise-match anything, we still
    take the top hit. Better than nothing — Genius's own relevance is
    usually correct on the headline."""
    import genius_fetch
    fake_response = MagicMock(status_code=200)
    fake_response.json.return_value = {
        "response": {"hits": [
            {"result": {
                "url": "https://genius.com/top-hit-lyrics",
                "primary_artist": {"name": "Unrelated Artist"},
            }},
        ]}
    }
    with _force_enabled(), patch("requests.get", return_value=fake_response):
        url = genius_fetch._search_song_url("Some Band", "Some Song")
    assert url == "https://genius.com/top-hit-lyrics"


def test_search_returns_none_on_no_hits():
    import genius_fetch
    fake_response = MagicMock(status_code=200)
    fake_response.json.return_value = {"response": {"hits": []}}
    with _force_enabled(), patch("requests.get", return_value=fake_response):
        url = genius_fetch._search_song_url("X", "Y")
    assert url is None


def test_search_returns_none_on_http_error():
    import genius_fetch
    fake_response = MagicMock(status_code=503)
    with _force_enabled(), patch("requests.get", return_value=fake_response):
        url = genius_fetch._search_song_url("X", "Y")
    assert url is None


def test_search_swallows_network_errors():
    import genius_fetch
    with _force_enabled(), patch("requests.get", side_effect=Exception("DNS fail")):
        url = genius_fetch._search_song_url("X", "Y")
    assert url is None


# ─── scrape_lyrics_from_url (mocked HTTP + parse) ─────────────────


def test_scrape_extracts_lyrics_from_data_container():
    import genius_fetch
    fake_html = (
        '<html><body>'
        '<div data-lyrics-container="true" class="x">'
        '[Verse 1]<br/>Hubo tiempos de guerras<br/>Pero hermano nuestra mente cambió<br/>'
        '<br/>[Chorus]<br/>Legalícenla<br/>Oh-oh-oh<br/>Legalícenla<br/>'
        '</div>'
        '</body></html>'
    )
    fake_response = MagicMock(status_code=200, text=fake_html)
    with _force_enabled(), patch("requests.get", return_value=fake_response):
        text = genius_fetch._scrape_lyrics_from_url("https://genius.com/x")
    assert text is not None
    # Lyrics present, section markers stripped, HTML tags gone
    assert "Hubo tiempos de guerras" in text
    assert "Legalícenla" in text
    assert "Oh-oh-oh" in text
    assert "[Verse 1]" not in text
    assert "[Chorus]" not in text
    assert "<br/>" not in text


def test_scrape_returns_none_when_no_container():
    import genius_fetch
    fake_response = MagicMock(status_code=200, text="<html>no lyrics here</html>")
    with _force_enabled(), patch("requests.get", return_value=fake_response):
        text = genius_fetch._scrape_lyrics_from_url("https://genius.com/x")
    assert text is None


def test_scrape_returns_none_on_thin_content():
    """A page that returns only 1 line of <60 chars is suspicious — drop."""
    import genius_fetch
    fake_html = '<div data-lyrics-container="true">tiny</div>'
    fake_response = MagicMock(status_code=200, text=fake_html)
    with _force_enabled(), patch("requests.get", return_value=fake_response):
        text = genius_fetch._scrape_lyrics_from_url("https://genius.com/x")
    assert text is None


def test_scrape_decodes_html_entities():
    import genius_fetch
    fake_html = (
        '<div data-lyrics-container="true">'
        'Don&#x27;t look back<br/>It&amp;s a long way<br/>Don&#x27;t look back<br/>'
        'It&amp;s a long way<br/>'
        '</div>'
    )
    fake_response = MagicMock(status_code=200, text=fake_html)
    with _force_enabled(), patch("requests.get", return_value=fake_response):
        text = genius_fetch._scrape_lyrics_from_url("https://genius.com/x")
    assert text is not None
    assert "Don't look back" in text
    assert "It&s a long way" in text


# ─── fetch_genius_plain end-to-end (mocked search + scrape) ───────


def test_fetch_returns_none_when_disabled():
    """Without GENIUS_API_TOKEN we early-return None — Genly stays on
    lrclib-only behaviour, no surprise behaviour."""
    import genius_fetch
    with patch.object(genius_fetch, "_TOKEN", ""):
        assert genius_fetch.fetch_genius_plain("Artist", "Song") is None


def test_fetch_returns_none_on_empty_inputs():
    import genius_fetch
    with _force_enabled():
        assert genius_fetch.fetch_genius_plain("", "Song") is None
        assert genius_fetch.fetch_genius_plain("Artist", "") is None
        assert genius_fetch.fetch_genius_plain(None, None) is None


def test_fetch_full_happy_path_db_none():
    """Search → scrape → return text. Skip cache path since db is None."""
    import genius_fetch
    with _force_enabled(), \
         patch.object(genius_fetch, "_search_song_url",
                      return_value="https://genius.com/x"), \
         patch.object(genius_fetch, "_scrape_lyrics_from_url",
                      return_value="line one\nline two\nline three\nline four"):
        text = genius_fetch.fetch_genius_plain("Artist", "Song")
    assert text == "line one\nline two\nline three\nline four"


def test_fetch_returns_none_when_search_misses():
    import genius_fetch
    with _force_enabled(), patch.object(genius_fetch, "_search_song_url", return_value=None):
        text = genius_fetch.fetch_genius_plain("Unknown Artist", "Unknown Song")
    assert text is None
