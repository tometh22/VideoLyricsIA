"""Unit tests for the Gemini-grounded reference-lyrics fetcher.

Covers:
- Cache hit avoids the Gemini call entirely.
- Validation rejects: no grounding sources, RECITATION/SAFETY block, sentinel,
  too-few lines, repetition loop.
- Successful fetch persists to LyricsCache and records provenance with URLs.
- Feature flag turns the whole path off.

The Gemini SDK is mocked end-to-end — these tests never hit the network.
"""

import hashlib
import os
import uuid

import pytest


# ---------------------------------------------------------------------------
# Stub response classes mirroring the google-genai SDK shape we read.
# ---------------------------------------------------------------------------

class _StubWeb:
    def __init__(self, uri="", title=""):
        self.uri = uri
        self.title = title


class _StubChunk:
    def __init__(self, uri="", title=""):
        self.web = _StubWeb(uri, title)


class _StubGrounding:
    def __init__(self, uris=None, titles=None):
        uris = uris or []
        titles = titles or []
        self.grounding_chunks = [
            _StubChunk(uris[i], titles[i] if i < len(titles) else "")
            for i in range(len(uris))
        ]


class _StubCandidate:
    def __init__(self, finish_reason="STOP", grounding_uris=None, grounding_titles=None):
        self.finish_reason = finish_reason
        if grounding_uris is None:
            self.grounding_metadata = None
        else:
            self.grounding_metadata = _StubGrounding(grounding_uris, grounding_titles)


class _StubResponse:
    def __init__(self, text="", finish_reason="STOP", grounding_uris=None,
                 grounding_titles=None):
        self.text = text
        self.candidates = [
            _StubCandidate(finish_reason, grounding_uris, grounding_titles),
        ]


def _stub_client(response):
    """Return a mock genai client whose generate_content returns `response`."""
    class _Models:
        def generate_content(self, **kwargs):
            return response
    class _Client:
        models = _Models()
    return _Client()


def _key(artist, song):
    return hashlib.sha1(
        f"{artist.lower().strip()}|{song.lower().strip()}".encode()
    ).hexdigest()[:16]


# Healthy lyrics body the validator should accept (>=8 distinct lines, >=80 chars,
# no line repeats >40% of total).
HEALTHY_LYRICS = (
    "Cuando la luna se duerme en el río\n"
    "y el viento susurra al pasar\n"
    "yo sigo aquí esperando\n"
    "una señal de tu lugar\n"
    "Las calles vacías me hablan de ti\n"
    "los faroles cantan tu nombre\n"
    "y el cielo se viste de gris\n"
    "cuando no estás conmigo\n"
    "esta noche es solo nuestra\n"
    "no la dejes escapar\n"
)


def _ensure_lyrics_cache_table():
    """Force-create the LyricsCache table on the test SQLite DB. The
    session-scoped autouse fixture in conftest already calls init_db() once,
    but we add the model after that fixture runs in some IDE flows; idempotent
    create_all is safe here either way."""
    from database import Base, engine
    Base.metadata.create_all(bind=engine)


@pytest.fixture(autouse=True)
def _table(setup_db):
    _ensure_lyrics_cache_table()
    yield


@pytest.fixture
def fresh_db():
    """A DB session that explicitly cleans up LyricsCache rows after the test
    so tests can't pollute each other through the cache table."""
    from database import LyricsCache, SessionLocal
    s = SessionLocal()
    yield s
    s.rollback()
    try:
        s.query(LyricsCache).delete()
        s.commit()
    except Exception:
        s.rollback()
    s.close()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_cache_hit_skips_gemini(fresh_db, monkeypatch):
    """Pre-populated LyricsCache row → Gemini client is never built."""
    from database import LyricsCache
    from pipeline import _fetch_lyrics_via_gemini_search

    artist, song = "Test Artist Cache", "Song A " + uuid.uuid4().hex[:6]
    fresh_db.add(LyricsCache(
        cache_key=_key(artist, song), artist=artist, title=song,
        lyrics=HEALTHY_LYRICS, source_urls=["https://test.com/x"],
        fetched_by_model="gemini-2.5-flash",
    ))
    fresh_db.commit()

    def _explode():
        raise AssertionError("_get_genai_client must NOT be called on cache hit")
    monkeypatch.setattr("pipeline._get_genai_client", _explode)

    result = _fetch_lyrics_via_gemini_search(artist, song, db=fresh_db)
    assert result == HEALTHY_LYRICS


def test_validation_rejects_no_grounding(fresh_db, monkeypatch):
    """Gemini answers without any grounding metadata → reject (hallucination)."""
    from pipeline import _fetch_lyrics_via_gemini_search

    artist, song = "NoGround Artist", "NoGround Song " + uuid.uuid4().hex[:6]
    monkeypatch.setattr("pipeline._get_genai_client", lambda: _stub_client(
        _StubResponse(text=HEALTHY_LYRICS, finish_reason="STOP",
                      grounding_uris=None),
    ))
    result = _fetch_lyrics_via_gemini_search(artist, song, db=fresh_db)
    assert result is None


def test_validation_rejects_empty_grounding_chunks(fresh_db, monkeypatch):
    """Grounding metadata exists but has zero chunks → also reject."""
    from pipeline import _fetch_lyrics_via_gemini_search

    artist, song = "EmptyGround", "Empty " + uuid.uuid4().hex[:6]
    monkeypatch.setattr("pipeline._get_genai_client", lambda: _stub_client(
        _StubResponse(text=HEALTHY_LYRICS, grounding_uris=[]),
    ))
    result = _fetch_lyrics_via_gemini_search(artist, song, db=fresh_db)
    assert result is None


def test_recitation_finish_reason_falls_back(fresh_db, monkeypatch):
    """Gemini blocked the answer for copyrighted recitation → return None."""
    from pipeline import _fetch_lyrics_via_gemini_search

    artist, song = "RecitArtist", "RecitSong " + uuid.uuid4().hex[:6]
    monkeypatch.setattr("pipeline._get_genai_client", lambda: _stub_client(
        _StubResponse(text="", finish_reason="RECITATION",
                      grounding_uris=["https://genius.com/song"]),
    ))
    result = _fetch_lyrics_via_gemini_search(artist, song, db=fresh_db)
    assert result is None


def test_safety_finish_reason_falls_back(fresh_db, monkeypatch):
    """Same path for SAFETY block."""
    from pipeline import _fetch_lyrics_via_gemini_search

    artist, song = "SafetyArtist", "SafetySong " + uuid.uuid4().hex[:6]
    monkeypatch.setattr("pipeline._get_genai_client", lambda: _stub_client(
        _StubResponse(text="", finish_reason="SAFETY",
                      grounding_uris=["https://lyrics.com/x"]),
    ))
    result = _fetch_lyrics_via_gemini_search(artist, song, db=fresh_db)
    assert result is None


def test_lyrics_not_found_sentinel(fresh_db, monkeypatch):
    """Gemini emitted the LYRICS_NOT_FOUND sentinel → return None."""
    from pipeline import _fetch_lyrics_via_gemini_search

    artist, song = "Sentinel", "Song " + uuid.uuid4().hex[:6]
    monkeypatch.setattr("pipeline._get_genai_client", lambda: _stub_client(
        _StubResponse(text="LYRICS_NOT_FOUND",
                      grounding_uris=["https://genius.com/x"]),
    ))
    result = _fetch_lyrics_via_gemini_search(artist, song, db=fresh_db)
    assert result is None


def test_too_few_lines_rejected(fresh_db, monkeypatch):
    """Real songs have a chorus + verses — anything under 8 lines is suspect."""
    from pipeline import _fetch_lyrics_via_gemini_search

    artist, song = "Tiny", "Tiny " + uuid.uuid4().hex[:6]
    short = "line one\nline two\nline three\n"
    monkeypatch.setattr("pipeline._get_genai_client", lambda: _stub_client(
        _StubResponse(text=short, grounding_uris=["https://genius.com/x"]),
    ))
    result = _fetch_lyrics_via_gemini_search(artist, song, db=fresh_db)
    assert result is None


def test_repetition_guard(fresh_db, monkeypatch):
    """20 identical lines → Gemini hallucination loop → reject."""
    from pipeline import _fetch_lyrics_via_gemini_search

    artist, song = "LoopArtist", "LoopSong " + uuid.uuid4().hex[:6]
    looped = "\n".join(["yo soy el riesgo"] * 20)
    monkeypatch.setattr("pipeline._get_genai_client", lambda: _stub_client(
        _StubResponse(text=looped, grounding_uris=["https://genius.com/x"]),
    ))
    result = _fetch_lyrics_via_gemini_search(artist, song, db=fresh_db)
    assert result is None


def test_successful_fetch_persists_cache(fresh_db, monkeypatch):
    """Healthy response → result returned, LyricsCache row written, second
    call hits cache without invoking Gemini again."""
    from database import LyricsCache
    from pipeline import _fetch_lyrics_via_gemini_search

    artist, song = "OK Artist", "OK Song " + uuid.uuid4().hex[:6]
    call_counter = {"n": 0}
    def _client_factory():
        call_counter["n"] += 1
        return _stub_client(_StubResponse(
            text=HEALTHY_LYRICS,
            grounding_uris=["https://genius.com/test", "https://letras.com/test"],
            grounding_titles=["Genius", "Letras"],
        ))
    monkeypatch.setattr("pipeline._get_genai_client", _client_factory)

    first = _fetch_lyrics_via_gemini_search(artist, song, db=fresh_db)
    assert first == HEALTHY_LYRICS.strip()

    row = fresh_db.query(LyricsCache).filter(
        LyricsCache.cache_key == _key(artist, song)
    ).first()
    assert row is not None
    assert row.lyrics == HEALTHY_LYRICS.strip()
    assert row.source_urls is not None
    assert any("genius.com" in u for u in row.source_urls)

    second = _fetch_lyrics_via_gemini_search(artist, song, db=fresh_db)
    assert second == HEALTHY_LYRICS.strip()
    assert call_counter["n"] == 1, "second call should be served from cache"


def test_feature_flag_off_skips_gemini(fresh_db, monkeypatch):
    """LYRICS_GEMINI_SEARCH_ENABLED=false → return None without calling Gemini."""
    from pipeline import _fetch_lyrics_via_gemini_search

    monkeypatch.setenv("LYRICS_GEMINI_SEARCH_ENABLED", "false")

    def _explode():
        raise AssertionError("Gemini must NOT be called when flag is off")
    monkeypatch.setattr("pipeline._get_genai_client", _explode)

    artist, song = "FlagOff", "Song " + uuid.uuid4().hex[:6]
    result = _fetch_lyrics_via_gemini_search(artist, song, db=fresh_db)
    assert result is None


def test_records_provenance_with_source_urls(fresh_db, monkeypatch):
    """When job_id is supplied, provenance is recorded and source URLs land
    in response_summary so UMG can audit the lyrics origin."""
    import json
    from pipeline import _fetch_lyrics_via_gemini_search

    artist, song = "ProvArtist", "ProvSong " + uuid.uuid4().hex[:6]
    monkeypatch.setattr("pipeline._get_genai_client", lambda: _stub_client(
        _StubResponse(
            text=HEALTHY_LYRICS,
            grounding_uris=["https://genius.com/foo", "https://letras.com/bar"],
            grounding_titles=["Genius", "Letras"],
        ),
    ))

    captured = {}

    class _FakeRecorder:
        def finish(self, response_summary=None, output_artifact=None):
            captured["response_summary"] = response_summary
            captured["output_artifact"] = output_artifact

    def _fake_record_ai_call(**kwargs):
        captured["kwargs"] = kwargs
        return _FakeRecorder()

    # Patch where pipeline.py imports it (lazy import inside the function).
    monkeypatch.setattr("provenance.record_ai_call", _fake_record_ai_call)

    result = _fetch_lyrics_via_gemini_search(
        artist, song, job_id="prov_test_job", db=fresh_db,
    )
    assert result == HEALTHY_LYRICS.strip()
    assert captured["kwargs"]["step"] == "lyrics_reference_fetch"
    assert captured["kwargs"]["tool_name"] == "gemini-2.5-flash"
    assert captured["kwargs"]["input_data_types"] == ["artist_name", "song_title"]
    assert captured["output_artifact"].startswith("lyrics_cache:")
    parsed = json.loads(captured["response_summary"])
    assert any("genius.com" in u for u in parsed["grounding_sources"])
    assert parsed["validation_passed"] is True
