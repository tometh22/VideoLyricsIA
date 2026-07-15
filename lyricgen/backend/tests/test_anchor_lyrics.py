"""Unit tests for `_maybe_anchor_align` — Versión B (anchor lyrics → CTC).

Contract under test: gated by ANCHOR_LYRICS_ENABLED (default OFF), never
raises, and returns the input `result` UNCHANGED on flag-off / empty anchor /
<3 lines / decline / exception so the transcription falls back to Versión A
(the current cascade). ctc_align + vocal_sep are mocked so these run without
torch / Replicate (same pattern as test_karaoke_align.py).
"""
import asyncio

import ctc_align
import vocal_sep
from main import _maybe_anchor_align


ANCHOR = "primera linea oficial\nsegunda linea oficial\ntercera linea oficial"


def _result():
    return {
        "job_id": "j-anchor",
        "segments": [
            {"start": 0.0, "end": 2.0, "text": "primera linea transcrita"},
            {"start": 2.0, "end": 4.0, "text": "segunda linea transcrita"},
            {"start": 4.0, "end": 6.0, "text": "tercera linea transcrita"},
        ],
        "reference_lyrics": "",
    }


def _retimed():
    # Shape of ctc_align.retime_segments output: per-line segs with word
    # stamps + scores. Line 2 has LOW scores (median 0.2 < 0.35) → review.
    return [
        {"start": 0.5, "end": 2.1, "text": "primera linea oficial",
         "words": [{"word": "primera", "start": 0.5, "end": 1.0, "score": 0.9},
                   {"word": "linea", "start": 1.1, "end": 1.6, "score": 0.8},
                   {"word": "oficial", "start": 1.7, "end": 2.1, "score": 0.85}]},
        {"start": 2.4, "end": 4.0, "text": "segunda linea oficial",
         "words": [{"word": "segunda", "start": 2.4, "end": 3.0, "score": 0.2},
                   {"word": "linea", "start": 3.1, "end": 3.5, "score": 0.1},
                   {"word": "oficial", "start": 3.6, "end": 4.0, "score": 0.3}]},
        {"start": 4.3, "end": 6.2, "text": "tercera linea oficial",
         "words": []},  # sin scores → no marcar
    ]


def _run(result, anchor, **kw):
    return asyncio.run(_maybe_anchor_align(result, "/tmp/a.mp3", "j-anchor",
                                           anchor, **kw))


def _no_stem(monkeypatch):
    # No cached stem + no compute → the helper aligns on the mix path.
    monkeypatch.setattr(vocal_sep, "separate_vocals",
                        lambda path, cache_only=False: None)
    monkeypatch.setenv("CTC_ALIGN_COMPUTE_STEM", "0")


def test_flag_off_is_identity_and_never_calls_engine(monkeypatch):
    monkeypatch.delenv("ANCHOR_LYRICS_ENABLED", raising=False)

    def _boom(*a, **k):
        raise AssertionError("retime_segments must not run with the flag off")
    monkeypatch.setattr(ctc_align, "retime_segments", _boom)
    monkeypatch.setattr(vocal_sep, "separate_vocals", _boom)
    result = _result()
    out = _run(result, ANCHOR)
    assert out is result


def test_empty_anchor_and_short_anchor_are_noop(monkeypatch):
    monkeypatch.setenv("ANCHOR_LYRICS_ENABLED", "1")

    def _boom(*a, **k):
        raise AssertionError("engine must not run without a usable anchor")
    monkeypatch.setattr(ctc_align, "retime_segments", _boom)
    monkeypatch.setattr(vocal_sep, "separate_vocals", _boom)
    result = _result()
    assert _run(result, "") is result
    assert _run(result, "  \n \n") is result
    # < 3 non-empty lines → no-op
    assert _run(result, "una linea\n\ndos lineas") is result
    # result no dict → no-op
    assert _run(None, ANCHOR) is None


def test_align_ok_replaces_segments_and_flags_low_conf_lines(monkeypatch):
    monkeypatch.setenv("ANCHOR_LYRICS_ENABLED", "1")
    _no_stem(monkeypatch)
    seen = {}

    def _fake_retime(audio_path, segments, job_id="", mix_path=None, *a, **k):
        seen["psegs"] = segments
        seen["audio_path"] = audio_path
        return _retimed()
    monkeypatch.setattr(ctc_align, "retime_segments", _fake_retime)
    result = _result()
    out = _run(result, ANCHOR)
    assert out is not result                      # copy, original untouched
    assert result.get("timing_source") is None
    assert out["timing_source"] == "anchor_ctc"
    texts = [s["text"] for s in out["segments"]]
    assert texts == ["primera linea oficial", "segunda linea oficial",
                     "tercera linea oficial"]
    # Anchor lines (not the transcript) were fed to the engine, zeroed.
    assert [p["text"] for p in seen["psegs"]] == texts
    assert all(p["start"] == 0.0 and p["end"] == 0.0 for p in seen["psegs"])
    # Per-line gate: median score 0.2 < 0.35 → review; 0.85 → clean;
    # no scores → not flagged.
    assert out["segments"][0].get("review") is not True
    assert out["segments"][1]["review"] is True
    assert out["segments"][2].get("review") is not True


def test_decline_keeps_result_intact(monkeypatch):
    monkeypatch.setenv("ANCHOR_LYRICS_ENABLED", "1")
    _no_stem(monkeypatch)
    monkeypatch.setattr(ctc_align, "retime_segments",
                        lambda *a, **k: None)
    monkeypatch.setattr(ctc_align, "last_decline_reason", "structural",
                        raising=False)
    result = _result()
    before = [dict(s) for s in result["segments"]]
    out = _run(result, ANCHOR)
    assert out is result
    assert out["segments"] == before
    assert "timing_source" not in out


def test_exception_never_propagates(monkeypatch):
    monkeypatch.setenv("ANCHOR_LYRICS_ENABLED", "1")
    _no_stem(monkeypatch)

    def _raise(*a, **k):
        raise RuntimeError("ctc exploded")
    monkeypatch.setattr(ctc_align, "retime_segments", _raise)
    result = _result()
    before = [dict(s) for s in result["segments"]]
    out = _run(result, ANCHOR)
    assert out is result                          # swallowed → unchanged
    assert out["segments"] == before
    assert "timing_source" not in out


def test_stem_lookup_failure_still_never_raises(monkeypatch):
    # vocal_sep itself exploding must degrade to "no anchor", not a 500.
    monkeypatch.setenv("ANCHOR_LYRICS_ENABLED", "1")

    def _raise(*a, **k):
        raise RuntimeError("replicate down")
    monkeypatch.setattr(vocal_sep, "separate_vocals", _raise)
    monkeypatch.setattr(ctc_align, "retime_segments",
                        lambda *a, **k: _retimed())
    result = _result()
    out = _run(result, ANCHOR)
    assert out is result
