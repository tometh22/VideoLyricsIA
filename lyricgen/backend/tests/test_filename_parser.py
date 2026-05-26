"""U11 (audit Sprint 2+ 2026-05-25) — _parse_filename_artist_title fix.

Bug histórico: el parser asumía "X - Y" = "Artist - Title", pero ~50% de
los downloads del operador son "Title - Artist" (YouTube/Spotify export).
Fix: lookup contra known-artists del tenant para resolver la ambigüedad
+ strip track-number prefix.
"""

from unittest.mock import MagicMock, patch
from main import _parse_filename_artist_title, _KNOWN_ARTISTS_CACHE


def setup_function():
    """Clear cache between tests."""
    _KNOWN_ARTISTS_CACHE.clear()


def test_basic_artist_title_split():
    """Sin DB: default 'Artist - Title' (compat histórica)."""
    a, t = _parse_filename_artist_title("Artist Name - Song Title.mp3")
    assert a == "Artist Name"
    assert t == "Song Title"


def test_underscore_split_inverted():
    """Underscore convention: 'Title_Artist' (Suno/YouTube export)."""
    a, t = _parse_filename_artist_title("Mujer amante_Rata Blanca.wav")
    assert a == "Rata Blanca"
    assert t == "Mujer amante"


def test_track_number_prefix_stripped():
    """Track-number prefix '05 ', '05-', '05.' stripped."""
    a, t = _parse_filename_artist_title("05 Intuicion M.wav")
    assert a == ""
    assert t == "Intuicion M"

    a, t = _parse_filename_artist_title("01-Title.mp3")
    assert t == "Title"

    a, t = _parse_filename_artist_title("01. Title.mp3")
    assert t == "Title"


def test_copy_suffix_stripped_from_title():
    """'(1)' / '(2)' copy suffix stripped."""
    a, t = _parse_filename_artist_title("Amanda Pujó - Ser Anti (1).wav")
    assert a == "Amanda Pujó"
    assert t == "Ser Anti"


def test_official_video_suffix_stripped():
    """Stock suffix 'Official Video' / 'Audio' / 'Lyrics' stripped."""
    a, t = _parse_filename_artist_title("Artist - Song (Official Video).mp3")
    assert a == "Artist"
    assert t == "Song"


def test_no_separator_returns_basename():
    """Sin separator → ('', basename)."""
    a, t = _parse_filename_artist_title("SomeTrack.mp3")
    assert a == ""
    assert t == "SomeTrack"


def test_db_lookup_disambiguates_title_artist_pattern():
    """U11 core: si tenant tiene 'Viejas Locas' en su DB de artists previos,
    'Legalícenla - Viejas Locas' debe parsearse como artist=Viejas Locas
    (no como artist=Legalícenla)."""
    # Mock DB: tenant 't1' tiene "Viejas Locas" como artist known.
    mock_db = MagicMock()
    mock_db.query.return_value.filter.return_value.filter.return_value.filter.return_value.distinct.return_value.limit.return_value.all.return_value = [
        ("Viejas Locas",),
        ("Rata Blanca",),
        ("Los Abuelos De La Nada",),
    ]
    a, t = _parse_filename_artist_title(
        "Legalícenla - Viejas Locas.wav",
        db=mock_db,
        tenant_id="t1",
    )
    # Debería invertir el split porque "Viejas Locas" es known artist.
    assert a == "Viejas Locas"
    assert t == "Legalícenla"


def test_db_lookup_keeps_default_when_head_is_known():
    """Si el head (Artist position) MATCHEA known artist → keep default order."""
    mock_db = MagicMock()
    mock_db.query.return_value.filter.return_value.filter.return_value.filter.return_value.distinct.return_value.limit.return_value.all.return_value = [
        ("Pink Floyd",),
    ]
    a, t = _parse_filename_artist_title(
        "Pink Floyd - Wish You Were Here.mp3",
        db=mock_db,
        tenant_id="t1",
    )
    assert a == "Pink Floyd"
    assert t == "Wish You Were Here"


def test_db_lookup_no_match_falls_back_to_default():
    """Ningún lado matchea known artists → comportamiento histórico (head=artist)."""
    mock_db = MagicMock()
    mock_db.query.return_value.filter.return_value.filter.return_value.filter.return_value.distinct.return_value.limit.return_value.all.return_value = [
        ("Some Other Artist",),
    ]
    a, t = _parse_filename_artist_title(
        "Unknown1 - Unknown2.wav",
        db=mock_db,
        tenant_id="t1",
    )
    assert a == "Unknown1"
    assert t == "Unknown2"


def test_db_lookup_both_match_keeps_default():
    """Si AMBOS lados son known artists → fallback al default (head=artist).
    Caso raro pero defensivo."""
    mock_db = MagicMock()
    mock_db.query.return_value.filter.return_value.filter.return_value.filter.return_value.distinct.return_value.limit.return_value.all.return_value = [
        ("Foo",),
        ("Bar",),
    ]
    a, t = _parse_filename_artist_title("Foo - Bar.mp3", db=mock_db, tenant_id="t1")
    assert a == "Foo"
    assert t == "Bar"


def test_db_lookup_is_cached(monkeypatch):
    """Segunda llamada con mismo tenant_id no re-pega la DB (cache TTL=5min)."""
    mock_db = MagicMock()
    mock_db.query.return_value.filter.return_value.filter.return_value.filter.return_value.distinct.return_value.limit.return_value.all.return_value = [
        ("Cached Artist",),
    ]
    _parse_filename_artist_title("A - B.mp3", db=mock_db, tenant_id="cached_tenant")
    _parse_filename_artist_title("C - D.mp3", db=mock_db, tenant_id="cached_tenant")
    _parse_filename_artist_title("E - F.mp3", db=mock_db, tenant_id="cached_tenant")
    # DB consultado UNA sola vez.
    assert mock_db.query.call_count == 1


def test_no_tenant_id_skips_db_lookup():
    """Sin tenant_id → no se consulta DB (compat con llamadas legacy)."""
    mock_db = MagicMock()
    mock_db.query.return_value.filter.return_value.filter.return_value.filter.return_value.distinct.return_value.limit.return_value.all.return_value = [
        ("Some Artist",),
    ]
    a, t = _parse_filename_artist_title("X - Y.mp3", db=mock_db, tenant_id="")
    # No DB call.
    assert mock_db.query.call_count == 0
    # Default behavior.
    assert a == "X"
    assert t == "Y"


def test_legalicenla_real_world_case():
    """Caso concreto del audit 2026-05-25: el operador previamente subió
    'Una Vez Mas' que parseaba bien (underscore Rata Blanca-style) →
    'Viejas Locas' queda en DB. Después sube 'Legalícenla - Viejas Locas.wav'
    → con el fix, parser invierte y queda Artist='Viejas Locas'."""
    mock_db = MagicMock()
    mock_db.query.return_value.filter.return_value.filter.return_value.filter.return_value.distinct.return_value.limit.return_value.all.return_value = [
        ("Viejas Locas",),  # Already in DB from prior uploads.
    ]
    a, t = _parse_filename_artist_title(
        "Legalícenla - Viejas Locas.wav",
        db=mock_db,
        tenant_id="umg",
    )
    assert a == "Viejas Locas", f"expected artist='Viejas Locas', got {a!r}"
    assert t == "Legalícenla", f"expected title='Legalícenla', got {t!r}"
