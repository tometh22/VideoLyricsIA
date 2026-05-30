"""Unit tests for karaoke_align — the on-demand word-timing enrichment.

Contract under test (see plan): never raises, gated/cached at the caller, and
returns the input segments UNCHANGED on disable / failure / no-match so the
render falls back to today's synthesis (no regression). forced_align is mocked
so these run without Replicate.
"""
import karaoke_align


def _segs():
    return [
        {"start": 0.0, "end": 2.0, "text": "hola mundo"},
        {"start": 2.0, "end": 4.0, "text": "segunda linea"},
    ]


def _fa_output():
    # Shape of forced_align.forced_align_lyrics: per-line segs with words.
    return [
        {"start": 0.1, "end": 1.9, "text": "hola mundo",
         "words": [{"word": "hola", "start": 0.1, "end": 0.6, "score": 0.9},
                   {"word": "mundo", "start": 0.7, "end": 1.9, "score": 0.8}]},
        {"start": 2.1, "end": 3.8, "text": "segunda linea",
         "words": [{"word": "segunda", "start": 2.1, "end": 3.0, "score": 0.9},
                   {"word": "linea", "start": 3.1, "end": 3.8, "score": 0.7}]},
    ]


def test_attaches_words_and_preserves_operator_timing(monkeypatch):
    monkeypatch.setattr("forced_align.is_enabled", lambda: True)
    monkeypatch.setattr("forced_align.forced_align_lyrics", lambda a, t: _fa_output())
    out = karaoke_align.enrich_segments_with_word_timings(_segs(), "/tmp/a.mp3")
    assert out[0]["words"][0]["word"] == "hola"
    assert out[1]["words"][1]["word"] == "linea"
    # Operator's per-line start/end is preserved (the WHEN); FA only adds words.
    assert out[0]["start"] == 0.0 and out[0]["end"] == 2.0
    assert out[1]["start"] == 2.0 and out[1]["end"] == 4.0


def test_line_text_mismatch_leaves_that_line_wordless(monkeypatch):
    # Operator edited the 2nd line after alignment → FA text won't match → that
    # line keeps no words (falls back to synthesis), the 1st still gets words.
    edited = _segs()
    edited[1]["text"] = "una linea totalmente distinta"
    monkeypatch.setattr("forced_align.is_enabled", lambda: True)
    monkeypatch.setattr("forced_align.forced_align_lyrics", lambda a, t: _fa_output())
    out = karaoke_align.enrich_segments_with_word_timings(edited, "/tmp/a.mp3")
    assert out[0].get("words")          # matched
    assert "words" not in out[1]        # mismatch → no words


def test_disabled_is_noop_and_does_not_call_fa(monkeypatch):
    called = {"n": 0}
    def _boom(*a, **k):
        called["n"] += 1
        raise AssertionError("forced_align_lyrics must not be called when disabled")
    monkeypatch.setattr("forced_align.is_enabled", lambda: False)
    monkeypatch.setattr("forced_align.forced_align_lyrics", _boom)
    segs = _segs()
    out = karaoke_align.enrich_segments_with_word_timings(segs, "/tmp/a.mp3")
    assert out is segs and called["n"] == 0


def test_fa_returns_none_is_noop(monkeypatch):
    monkeypatch.setattr("forced_align.is_enabled", lambda: True)
    monkeypatch.setattr("forced_align.forced_align_lyrics", lambda a, t: None)
    segs = _segs()
    out = karaoke_align.enrich_segments_with_word_timings(segs, "/tmp/a.mp3")
    assert out is segs


def test_fa_raises_never_propagates(monkeypatch):
    monkeypatch.setattr("forced_align.is_enabled", lambda: True)
    def _raise(a, t):
        raise RuntimeError("replicate exploded")
    monkeypatch.setattr("forced_align.forced_align_lyrics", _raise)
    segs = _segs()
    out = karaoke_align.enrich_segments_with_word_timings(segs, "/tmp/a.mp3")
    assert out is segs   # swallowed → unchanged


def test_already_has_words_is_noop_without_fa(monkeypatch):
    def _boom(*a, **k):
        raise AssertionError("must not align when words already present")
    monkeypatch.setattr("forced_align.is_enabled", lambda: (_ for _ in ()).throw(AssertionError("no")))
    monkeypatch.setattr("forced_align.forced_align_lyrics", _boom)
    segs = [{"start": 0.0, "end": 2.0, "text": "hola",
             "words": [{"word": "hola", "start": 0.0, "end": 1.0}]}]
    out = karaoke_align.enrich_segments_with_word_timings(segs, "/tmp/a.mp3")
    assert out is segs


def test_empty_or_no_audio_is_noop():
    assert karaoke_align.enrich_segments_with_word_timings([], "/tmp/a.mp3") == []
    segs = _segs()
    assert karaoke_align.enrich_segments_with_word_timings(segs, "") is segs


def test_sanity_guard_rejects_grossly_misaligned_words(monkeypatch):
    # FA returns words for the right line text, but their timestamps land far
    # outside the operator's line window (e.g. FA aligned to a different part).
    # The guard should DROP them → that line stays on synthesis (no words).
    segs = [{"start": 0.0, "end": 2.0, "text": "hola mundo"}]
    fa = [{"start": 50.0, "end": 52.0, "text": "hola mundo",
           "words": [{"word": "hola", "start": 50.0, "end": 51.0},
                     {"word": "mundo", "start": 51.0, "end": 52.0}]}]
    monkeypatch.setattr("forced_align.is_enabled", lambda: True)
    monkeypatch.setattr("forced_align.forced_align_lyrics", lambda a, t: fa)
    out = karaoke_align.enrich_segments_with_word_timings(segs, "/tmp/a.mp3")
    assert "words" not in out[0]        # grossly off → rejected


def test_sanity_guard_tolerates_small_operator_shift(monkeypatch):
    # Operator nudged the line ~0.3s vs the FA span; they still overlap heavily
    # → words ARE attached (we only reject GROSS misalignment).
    segs = [{"start": 0.3, "end": 2.3, "text": "hola mundo"}]
    fa = [{"start": 0.0, "end": 2.0, "text": "hola mundo",
           "words": [{"word": "hola", "start": 0.0, "end": 1.0},
                     {"word": "mundo", "start": 1.0, "end": 2.0}]}]
    monkeypatch.setattr("forced_align.is_enabled", lambda: True)
    monkeypatch.setattr("forced_align.forced_align_lyrics", lambda a, t: fa)
    out = karaoke_align.enrich_segments_with_word_timings(segs, "/tmp/a.mp3")
    assert out[0].get("words")          # heavy overlap → attached


def test_repeated_lines_map_in_order(monkeypatch):
    segs = [
        {"start": 0.0, "end": 1.0, "text": "ay"},
        {"start": 1.0, "end": 2.0, "text": "ay"},
    ]
    fa = [
        {"start": 0.0, "end": 1.0, "text": "ay",
         "words": [{"word": "ay", "start": 0.0, "end": 0.9}]},
        {"start": 1.0, "end": 2.0, "text": "ay",
         "words": [{"word": "ay", "start": 1.0, "end": 1.9}]},
    ]
    monkeypatch.setattr("forced_align.is_enabled", lambda: True)
    monkeypatch.setattr("forced_align.forced_align_lyrics", lambda a, t: fa)
    out = karaoke_align.enrich_segments_with_word_timings(segs, "/tmp/a.mp3")
    assert out[0]["words"][0]["end"] == 0.9
    assert out[1]["words"][0]["end"] == 1.9   # second "ay" got the SECOND match
