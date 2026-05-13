"""Tests del fallback /api/search en `_fetch_lrclib`.

Caso motivador: Noches Sin Sueño (Rata Blanca, staging 2026-05-13).
lrclib.net devolvió 404 transient en `/api/get`, pero `/api/search`
tenía 4 candidates válidos con synced. Sin fallback, caímos a Gemini
+ Whisper recovery con output basura.

Tests cubren:
- /get falla, /search rescata el mejor candidate
- /search devuelve candidates débiles (score < 0.5) → no rescue
- Pick prefiere exact artist match sobre substring
- Pick prefiere synced sobre plain-only
- /search HTTP error / network failure → graceful None
"""
from unittest.mock import patch, MagicMock

import pytest


def _candidate(artist, track, has_synced=True, has_plain=True, _id=1):
    """Build a fake /api/search result item with the shape lrclib returns."""
    return {
        "id": _id,
        "name": track,
        "trackName": track,
        "artistName": artist,
        "albumName": "Test Album",
        "duration": 240.0,
        "instrumental": False,
        "plainLyrics": "Línea uno\nLínea dos\nLínea tres\nLínea cuatro" if has_plain else None,
        "syncedLyrics": (
            "[00:10.00] Línea uno\n[00:14.00] Línea dos\n"
            "[00:18.00] Línea tres\n[00:22.00] Línea cuatro"
            if has_synced else None
        ),
    }


def _mock_get_then_search(get_status, search_candidates):
    """Returns a side_effect function for requests.get that simulates:
    - First call (/api/get): HTTP status `get_status`, body = candidate-shape or empty
    - Second call (/api/search): HTTP 200 with `search_candidates` list
    """
    calls = {"count": 0}

    def fake_get(url, **kwargs):
        resp = MagicMock()
        if "/api/get" in url:
            resp.status_code = get_status
            resp.json.return_value = {} if get_status == 404 else {"plainLyrics": "..."}
            calls["count"] += 1
            return resp
        if "/api/search" in url:
            resp.status_code = 200
            resp.json.return_value = search_candidates
            calls["count"] += 1
            return resp
        # Unexpected URL
        resp.status_code = 500
        resp.json.return_value = {}
        return resp

    return fake_get, calls


# ─── Happy path: search rescata cuando /get falla ───────────────────

def test_search_rescues_when_get_returns_404():
    """Caso motivador Noches Sin Sueño: /get devuelve 404 transient,
    /search devuelve candidate válido con synced → fetch retorna el
    record parseado."""
    from pipeline import _fetch_lrclib
    candidates = [
        _candidate("Rata Blanca", "Noches Sin Sueño", _id=10965185),
    ]
    fake_get, calls = _mock_get_then_search(404, candidates)
    with patch("requests.get", side_effect=fake_get):
        result = _fetch_lrclib("Rata Blanca", "Noches Sin Sueño", db=None)
    assert result is not None
    assert result["synced"] is not None
    assert "Línea uno" in result["plain"]
    assert calls["count"] == 2  # /get + /search


def test_search_prefers_candidate_with_synced():
    """Si /search devuelve 3 candidates pero solo 1 tiene synced,
    el picker debe preferir el de synced."""
    from pipeline import _pick_best_lrclib_candidate
    candidates = [
        _candidate("Rata Blanca", "Noches Sin Sueño", has_synced=False, _id=1),
        _candidate("Rata Blanca", "Noches Sin Sueño", has_synced=True, _id=2),
        _candidate("Rata Blanca", "Noches Sin Sueño", has_synced=False, _id=3),
    ]
    best = _pick_best_lrclib_candidate(candidates, "Rata Blanca", "Noches Sin Sueño")
    assert best is not None
    assert best["id"] == 2  # synced version wins


def test_search_prefers_exact_artist_match():
    """Si hay 2 candidates con song exacto pero artist es exacto en
    uno y substring en otro, elegir el exacto."""
    from pipeline import _pick_best_lrclib_candidate
    candidates = [
        _candidate("Rata Blanca Tributo", "Noches Sin Sueño", _id=1),
        _candidate("Rata Blanca", "Noches Sin Sueño", _id=2),
    ]
    best = _pick_best_lrclib_candidate(candidates, "Rata Blanca", "Noches Sin Sueño")
    assert best is not None
    assert best["id"] == 2  # exact artist wins


# ─── Defensive: low score, no match ─────────────────────────────────

def test_search_rejects_weak_match():
    """Si los candidates no matchean artist+song razonablemente,
    no rescatar. Threshold 0.5 evita falsos positivos."""
    from pipeline import _pick_best_lrclib_candidate
    candidates = [
        _candidate("Otro Artista", "Otra Canción", _id=1),
    ]
    best = _pick_best_lrclib_candidate(candidates, "Rata Blanca", "Noches Sin Sueño")
    assert best is None  # ningún match supera 0.5


def test_search_empty_candidates_returns_none():
    """Si /search devuelve [], el picker retorna None sin crash."""
    from pipeline import _pick_best_lrclib_candidate
    assert _pick_best_lrclib_candidate([], "X", "Y") is None


def test_search_handles_missing_artist_or_song():
    """Defensive: si artist o song vienen vacíos, no rescatamos."""
    from pipeline import _pick_best_lrclib_candidate, _try_lrclib_search
    assert _pick_best_lrclib_candidate([_candidate("X", "Y")], "", "Z") is None
    assert _try_lrclib_search("", "Y") == []
    assert _try_lrclib_search("X", "") == []


# ─── Network failure handling ───────────────────────────────────────

def test_search_network_failure_returns_empty():
    """Si /api/search lanza excepción, `_try_lrclib_search` retorna
    [] sin crash. El caller cae al Gemini fallback como antes."""
    from pipeline import _try_lrclib_search
    with patch("requests.get", side_effect=Exception("network down")):
        result = _try_lrclib_search("Rata Blanca", "Noches Sin Sueño")
    assert result == []


def test_search_http_error_returns_empty():
    """Si /api/search devuelve 500, retornar [] (igual que con 404)."""
    from pipeline import _try_lrclib_search
    resp = MagicMock()
    resp.status_code = 500
    with patch("requests.get", return_value=resp):
        result = _try_lrclib_search("Rata Blanca", "Noches Sin Sueño")
    assert result == []


def test_search_non_list_response_returns_empty():
    """Defensive: lrclib API contract is list. Si por alguna razón
    devuelve dict o str, no crash."""
    from pipeline import _try_lrclib_search
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {"unexpected": "shape"}
    with patch("requests.get", return_value=resp):
        result = _try_lrclib_search("X", "Y")
    assert result == []


# ─── Regression: /get success no toca /search ───────────────────────

def test_get_success_skips_search_entirely():
    """Si /api/get devuelve 200 OK, NO debe llamar /api/search.
    El fallback es solo para casos de fallo de /get."""
    from pipeline import _fetch_lrclib

    calls = {"get": 0, "search": 0}

    def fake_get(url, **kwargs):
        resp = MagicMock()
        if "/api/get" in url:
            calls["get"] += 1
            resp.status_code = 200
            resp.json.return_value = {
                "plainLyrics": "Línea uno\nLínea dos",
                "syncedLyrics": "[00:10.00] Línea uno\n[00:14.00] Línea dos",
                "duration": 120.0,
            }
            return resp
        if "/api/search" in url:
            calls["search"] += 1
            resp.status_code = 200
            resp.json.return_value = []
            return resp
        resp.status_code = 500
        return resp

    with patch("requests.get", side_effect=fake_get):
        result = _fetch_lrclib("Test", "Song", db=None)
    assert result is not None
    assert calls["get"] == 1
    assert calls["search"] == 0  # NO debe llamar search si /get OK
