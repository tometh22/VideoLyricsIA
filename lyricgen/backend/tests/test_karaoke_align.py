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


# --- KARAOKE_FA_RETIME_ENABLED: trust confident forced-align over a drifted
#     whisperX window (fix for Mercedes Sosa "Hablando A Tu Corazón") ---

def _fa_drifted():
    # FA places the lines at their REAL onset, far from the (wrong) window.
    return [
        {"start": 5.0, "end": 7.0, "text": "hola mundo",
         "words": [{"word": "hola", "start": 5.0, "end": 5.6, "score": 0.9},
                   {"word": "mundo", "start": 5.7, "end": 7.0, "score": 0.85}]},
        {"start": 8.0, "end": 9.5, "text": "segunda linea",
         "words": [{"word": "segunda", "start": 8.0, "end": 8.8, "score": 0.88},
                   {"word": "linea", "start": 8.9, "end": 9.5, "score": 0.8}]},
    ]


def test_retime_off_by_default_drops_drifted_line(monkeypatch):
    # Flag OFF (default): correct FA disagrees with the wrong window → the guard
    # rejects it → line stays wordless (today's behaviour, documented).
    monkeypatch.delenv("KARAOKE_FA_RETIME_ENABLED", raising=False)
    monkeypatch.setattr("forced_align.is_enabled", lambda: True)
    monkeypatch.setattr("forced_align.forced_align_lyrics", lambda a, t: _fa_drifted())
    out = karaoke_align.enrich_segments_with_word_timings(_segs(), "/tmp/a.mp3")
    assert "words" not in out[0] and out[0]["start"] == 0.0


def test_retime_on_overrides_drifted_window(monkeypatch):
    # Flag ON + confident FA span → trust FA: re-time the line + attach words.
    monkeypatch.setenv("KARAOKE_FA_RETIME_ENABLED", "1")
    monkeypatch.setattr("forced_align.is_enabled", lambda: True)
    monkeypatch.setattr("forced_align.forced_align_lyrics", lambda a, t: _fa_drifted())
    out = karaoke_align.enrich_segments_with_word_timings(_segs(), "/tmp/a.mp3")
    assert out[0].get("retimed_by_forced_align") is True
    assert out[0]["start"] == 5.0 and out[0]["end"] == 7.0   # re-timed from FA
    assert out[0]["words"][0]["word"] == "hola"
    assert out[1]["start"] == 8.0 and out[1]["end"] == 9.5


def test_retime_on_low_confidence_does_not_retime(monkeypatch):
    # Flag ON but low FA confidence → never re-time onto shaky timing; fall
    # through to the window guard (window matches here → words attach, start/end
    # preserved).
    monkeypatch.setenv("KARAOKE_FA_RETIME_ENABLED", "1")
    low = _fa_output()
    for ln in low:
        for w in ln["words"]:
            w["score"] = 0.2
    monkeypatch.setattr("forced_align.is_enabled", lambda: True)
    monkeypatch.setattr("forced_align.forced_align_lyrics", lambda a, t: low)
    out = karaoke_align.enrich_segments_with_word_timings(_segs(), "/tmp/a.mp3")
    assert "retimed_by_forced_align" not in out[0]
    assert out[0]["start"] == 0.0 and out[0]["end"] == 2.0


def test_retime_never_overrides_a_locked_line(monkeypatch):
    # A line the operator manually synced (locked) must keep its timing even
    # with a confident FA span and the flag on.
    monkeypatch.setenv("KARAOKE_FA_RETIME_ENABLED", "1")
    segs = [{"start": 0.0, "end": 2.0, "text": "hola mundo", "locked": True}]
    monkeypatch.setattr("forced_align.is_enabled", lambda: True)
    monkeypatch.setattr("forced_align.forced_align_lyrics", lambda a, t: _fa_drifted())
    out = karaoke_align.enrich_segments_with_word_timings(segs, "/tmp/a.mp3")
    assert "retimed_by_forced_align" not in out[0]
    assert out[0]["start"] == 0.0 and out[0]["end"] == 2.0   # manual sync preserved


def test_fa_span_trustworthy_guards():
    assert karaoke_align._fa_span_trustworthy([]) is False
    assert karaoke_align._fa_span_trustworthy(
        [{"word": "x", "start": 1.0, "end": 1.0, "score": 0.9}]) is False   # zero span
    assert karaoke_align._fa_span_trustworthy(
        [{"word": "x", "start": 1.0, "end": 2.0, "score": 0.2}]) is False    # low conf
    assert karaoke_align._fa_span_trustworthy(
        [{"word": "x", "start": 1.0, "end": 2.0, "score": 0.9}]) is True
    # no scores present → coherence alone gates (trusted)
    assert karaoke_align._fa_span_trustworthy(
        [{"word": "x", "start": 1.0, "end": 2.0}]) is True


def test_wordless_lines_after_many_timed_lines_still_get_words(monkeypatch):
    # "Acaba De Nacer" (GIT, 25-nov): the first 23 lines kept their words and
    # every edited line after 2:22 had none. Lines that already carry words
    # were skipped WITHOUT advancing the cursor, so the 6-line search window
    # never reached the later lines and the look spread their words over the
    # whole window (up to 1.3 s late on held notes).
    timed = [{"start": float(i * 2), "end": float(i * 2 + 1.5), "text": f"linea {i}",
              "words": [{"word": "linea", "start": i * 2.0, "end": i * 2.0 + 0.5},
                        {"word": str(i), "start": i * 2.0 + 0.6, "end": i * 2.0 + 1.0}]}
             for i in range(8)]
    wordless = [{"start": 16.0, "end": 20.0, "text": "hoy quiero ver la luz"},
                {"start": 20.5, "end": 24.0, "text": "una nueva vida"}]
    fa = [{"start": s["start"], "end": s["end"], "text": s["text"], "words": s["words"]} for s in timed]
    fa += [
        {"start": 16.1, "end": 18.4, "text": "hoy quiero ver la luz",
         "words": [{"word": w, "start": 16.1 + k * 0.4, "end": 16.4 + k * 0.4, "score": 0.9}
                   for k, w in enumerate("hoy quiero ver la luz".split())]},
        {"start": 20.6, "end": 22.0, "text": "una nueva vida",
         "words": [{"word": w, "start": 20.6 + k * 0.4, "end": 20.9 + k * 0.4, "score": 0.9}
                   for k, w in enumerate("una nueva vida".split())]},
    ]
    monkeypatch.setattr("forced_align.is_enabled", lambda: True)
    monkeypatch.setattr("forced_align.forced_align_lyrics", lambda a, t: fa)
    out = karaoke_align.enrich_segments_with_word_timings(timed + wordless, "/tmp/a.mp3")
    assert [w["word"] for w in out[8]["words"]] == "hoy quiero ver la luz".split()
    assert [w["word"] for w in out[9]["words"]] == "una nueva vida".split()
    # Lines that already had words keep them untouched, operator timing kept.
    assert out[3]["words"] == timed[3]["words"]
    assert out[8]["start"] == 16.0 and out[8]["end"] == 20.0


def _orig():
    # Original transcription ("Acaba De Nacer", GIT): aligned words.
    return [
        {"start": 158.26, "end": 159.06, "text": "No quiero ver",
         "words": [{"word": "No", "start": 158.34, "end": 158.4},
                   {"word": "quiero", "start": 158.42, "end": 158.72},
                   {"word": "ver", "start": 158.76, "end": 159.0}]},
        {"start": 159.07, "end": 162.45, "text": "En tus ojos reflejar la luz",
         "words": [{"word": "en", "start": 159.02, "end": 159.06},
                   {"word": "tus", "start": 159.1, "end": 159.34},
                   {"word": "ojos", "start": 159.4, "end": 159.84},
                   {"word": "reflejar", "start": 159.88, "end": 160.46},
                   {"word": "la", "start": 160.48, "end": 160.66},
                   {"word": "luz", "start": 160.68, "end": 161.95}]},
    ]


def test_recovers_edited_line_words_from_the_original_transcription():
    # The operator merged two lines and changed "No" -> "hoy": the line lost
    # its words. Matched words keep their real time; "hoy" is interpolated.
    segs = [{"start": 158.25, "end": 162.28, "text": "hoy quiero ver en tus ojos reflejar la luz"}]
    out = karaoke_align.recover_words_from_reference(segs, _orig())
    words = out[0]["words"]
    assert [w["word"] for w in words] == segs[0]["text"].split()
    assert words[-1]["start"] == 160.68            # "luz" when it is sung
    assert words[0]["start"] == 158.25             # "hoy" fills the gap before "quiero"
    assert all(158.25 <= w["start"] < w["end"] <= 162.28 for w in words)
    assert all(a["end"] <= b["start"] for a, b in zip(words, words[1:]))
    assert out[0]["start"] == 158.25 and out[0]["end"] == 162.28


def test_recovery_leaves_timed_and_unmatched_lines_alone():
    timed = {"start": 1.0, "end": 2.0, "text": "hola", "words": [{"word": "hola", "start": 1.0, "end": 1.5}]}
    other = {"start": 158.25, "end": 162.28, "text": "algo completamente distinto acá"}
    segs = [timed, other]
    assert karaoke_align.recover_words_from_reference(segs, _orig()) is segs
    assert karaoke_align.recover_words_from_reference(segs, []) is segs
    assert karaoke_align.recover_words_from_reference(segs, None) is segs


def test_recovered_words_drive_the_word_timed_look():
    import lyric_looks as L

    segs = [{"start": 158.25, "end": 162.28, "text": "hoy quiero ver en tus ojos reflejar la luz"}]
    doc_synth = L.build_look_ass(segs, L.LOOKS["pincel"], width=1920, height=1080, duration=170)
    doc_real = L.build_look_ass(karaoke_align.recover_words_from_reference(segs, _orig()),
                                L.LOOKS["pincel"], width=1920, height=1080, duration=170)

    def luz_start(doc):
        line = [d for d in doc.splitlines() if d.startswith("Dialogue:") and d.endswith("}LUZ")][0]
        h, m, s = line.split(",")[1].split(":")
        return int(h) * 3600 + int(m) * 60 + float(s)

    assert luz_start(doc_real) < 160.8            # sung at 160.68
    assert luz_start(doc_synth) > 161.5           # the even split showed it >1 s late


def test_pipeline_takes_words_from_the_editor_original(monkeypatch):
    import database
    import pipeline

    class _Doc:
        original_segments = _orig()

    class _Query:
        def filter(self, *_a):
            return self

        def first(self):
            return _Doc()

    class _Session:
        def query(self, *_a):
            return _Query()

        def close(self):
            pass

    monkeypatch.setattr(database, "SessionLocal", lambda: _Session())
    segs = [{"start": 158.25, "end": 162.28, "text": "hoy quiero ver en tus ojos reflejar la luz"}]
    out = pipeline._recover_words_from_editor_original("3087ebfab4b0", segs)
    assert out[0]["words"][-1]["start"] == 160.68

    def _boom():
        raise RuntimeError("db down")

    monkeypatch.setattr(database, "SessionLocal", _boom)
    assert pipeline._recover_words_from_editor_original("3087ebfab4b0", segs) is segs
    assert pipeline._recover_words_from_editor_original("", segs) is segs


def test_stale_words_that_no_longer_spell_the_line_are_recovered():
    # "El Anda Diciendo": "amor" -> "vos" kept the old words array; the render
    # rejected it and spread the line evenly. It now counts as missing.
    seg = {"start": 166.82, "end": 170.66, "text": "Él anda diciendo que vos",
           "words": [{"word": w, "start": s, "end": s + 0.2} for w, s in
                     (("él", 166.9), ("anda", 167.1), ("diciendo", 167.7), ("que", 169.28), ("amor", 169.46))]}
    ref = [{"start": 166.82, "end": 170.66, "text": "Él anda diciendo que amor",
            "words": seg["words"]}]
    out = karaoke_align.recover_words_from_reference([seg], ref)
    words = out[0]["words"]
    assert [w["word"] for w in words] == ["Él", "anda", "diciendo", "que", "vos"]
    assert words[3]["start"] == 169.28 and words[4]["start"] >= words[3]["end"]
    # Words that still match their text are left exactly as they were.
    ok = dict(seg, text="Él anda diciendo que amor")
    assert karaoke_align.recover_words_from_reference([ok], ref)[0] is ok
