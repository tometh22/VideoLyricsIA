"""Unit tests for operator lyrics alignment.

The reference is authoritative content: CTC is preferred, Whisper-DP is the
fallback, and a double decline is explicit so upload callers can fail closed.
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


def test_flag_off_with_submitted_reference_declines_and_never_calls_engine(monkeypatch):
    monkeypatch.delenv("ANCHOR_LYRICS_ENABLED", raising=False)

    def _boom(*a, **k):
        raise AssertionError("retime_segments must not run with the flag off")
    monkeypatch.setattr(ctc_align, "retime_segments", _boom)
    monkeypatch.setattr(vocal_sep, "separate_vocals", _boom)
    result = _result()
    out = _run(result, ANCHOR)
    assert out["segments"] == result["segments"]
    assert out["anchor_alignment"]["status"] == "declined"
    assert out["anchor_alignment"]["reason"] == "feature_disabled"


def test_empty_anchor_is_noop_but_short_anchor_uses_fallback(monkeypatch):
    monkeypatch.setenv("ANCHOR_LYRICS_ENABLED", "1")
    _no_stem(monkeypatch)
    result = _result()
    assert _run(result, "") is result
    assert _run(result, "  \n \n") is result
    monkeypatch.setattr(
        "lyrics_whisper_align.whisper_word_align",
        lambda *_a, **_kw: [
            {"start": 1.0, "end": 2.0, "text": "una linea"},
            {"start": 3.0, "end": 4.0, "text": "dos lineas"},
        ],
    )
    out = _run(result, "una linea\n\ndos lineas")
    assert [segment["text"] for segment in out["segments"]] == [
        "una linea", "dos lineas",
    ]
    assert out["anchor_alignment"]["timing_source"] == "whisper_align"
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
    # Per-line gate: median score 0.2 < default 0.25 → review; 0.85 → clean;
    # no scores → not flagged.
    assert out["segments"][0].get("review") is not True
    assert out["segments"][1]["review"] is True
    assert out["segments"][2].get("review") is not True


def _retimed_borderline():
    # Línea 2 con mediana 0.27: cae ENTRE el default nuevo (0.25, no marca)
    # y el viejo (0.30, marcaba). Las otras dos son claramente buenas.
    return [
        {"start": 0.5, "end": 2.1, "text": "primera linea oficial",
         "words": [{"word": "a", "start": 0.5, "end": 1.0, "score": 0.9}]},
        {"start": 2.4, "end": 4.0, "text": "segunda linea oficial",
         "words": [{"word": "a", "start": 2.4, "end": 3.0, "score": 0.27},
                   {"word": "b", "start": 3.1, "end": 3.5, "score": 0.27}]},
        {"start": 4.3, "end": 6.2, "text": "tercera linea oficial",
         "words": [{"word": "a", "start": 4.3, "end": 5.0, "score": 0.8}]},
    ]


def test_review_threshold_default_025_marks_fewer(monkeypatch):
    # Default (sin env): umbral 0.25 → una línea a 0.27 NO se marca. Antes
    # (0.30) sí se marcaba. Confirma que el default marca MENOS líneas
    # (feedback dueño: 11/26 marcadas cuando el sync salió excelente).
    monkeypatch.setenv("ANCHOR_LYRICS_ENABLED", "1")
    monkeypatch.delenv("ANCHOR_REVIEW_MIN_SCORE", raising=False)
    _no_stem(monkeypatch)
    monkeypatch.setattr(ctc_align, "retime_segments",
                        lambda *a, **k: _retimed_borderline())
    out = _run(_result(), ANCHOR)
    assert [s.get("review") is True for s in out["segments"]] == [False, False, False]


def test_review_threshold_is_env_tuneable(monkeypatch):
    # Subir el umbral a 0.30 vuelve a marcar la línea borderline (0.27).
    monkeypatch.setenv("ANCHOR_LYRICS_ENABLED", "1")
    monkeypatch.setenv("ANCHOR_REVIEW_MIN_SCORE", "0.30")
    _no_stem(monkeypatch)
    monkeypatch.setattr(ctc_align, "retime_segments",
                        lambda *a, **k: _retimed_borderline())
    out = _run(_result(), ANCHOR)
    assert [s.get("review") is True for s in out["segments"]] == [False, True, False]


def test_review_threshold_bad_env_falls_back_to_default(monkeypatch):
    # Un valor no numérico no debe romper el anclado — cae al default 0.25.
    monkeypatch.setenv("ANCHOR_LYRICS_ENABLED", "1")
    monkeypatch.setenv("ANCHOR_REVIEW_MIN_SCORE", "not-a-number")
    _no_stem(monkeypatch)
    monkeypatch.setattr(ctc_align, "retime_segments",
                        lambda *a, **k: _retimed_borderline())
    out = _run(_result(), ANCHOR)
    assert out["timing_source"] == "anchor_ctc"
    assert [s.get("review") is True for s in out["segments"]] == [False, False, False]


def test_double_decline_is_explicit_and_preserves_provider_evidence(monkeypatch):
    monkeypatch.setenv("ANCHOR_LYRICS_ENABLED", "1")
    _no_stem(monkeypatch)
    monkeypatch.setattr(ctc_align, "retime_segments",
                        lambda *a, **k: None)
    monkeypatch.setattr(ctc_align, "last_decline_reason", "structural",
                        raising=False)
    result = _result()
    before = [dict(s) for s in result["segments"]]
    out = _run(result, ANCHOR)
    assert out["segments"] == before
    assert "timing_source" not in out
    assert out["reference_lyrics"] == ANCHOR
    assert out["anchor_alignment"]["status"] == "declined"
    assert out["anchor_alignment"]["reason"] == "structural"


def test_ctc_exception_uses_whisper_fallback(monkeypatch):
    monkeypatch.setenv("ANCHOR_LYRICS_ENABLED", "1")
    _no_stem(monkeypatch)

    def _raise(*a, **k):
        raise RuntimeError("ctc exploded")
    monkeypatch.setattr(ctc_align, "retime_segments", _raise)
    monkeypatch.setattr(
        "lyrics_whisper_align.whisper_word_align",
        lambda *_a, **_kw: _retimed(),
    )
    result = _result()
    out = _run(result, ANCHOR)
    assert out["timing_source"] == "anchor_ctc"
    assert out["anchor_alignment"]["status"] == "applied"
    assert out["anchor_alignment"]["timing_source"] == "whisper_align"
    assert out["anchor_alignment"]["ctc_decline_reason"] == "ctc_RuntimeError"


def test_stem_lookup_failure_aligns_on_mix(monkeypatch):
    # vocal_sep exploding must still allow a safe mix-based alignment.
    monkeypatch.setenv("ANCHOR_LYRICS_ENABLED", "1")

    def _raise(*a, **k):
        raise RuntimeError("replicate down")
    monkeypatch.setattr(vocal_sep, "separate_vocals", _raise)
    monkeypatch.setattr(ctc_align, "retime_segments",
                        lambda *a, **k: _retimed())
    result = _result()
    out = _run(result, ANCHOR)
    assert out["timing_source"] == "anchor_ctc"
    assert out["anchor_alignment"]["status"] == "applied"
    assert [segment["text"] for segment in out["segments"]] == ANCHOR.splitlines()


def test_ctc_decline_uses_whisper_fallback_and_keeps_official_text(monkeypatch):
    monkeypatch.setenv("ANCHOR_LYRICS_ENABLED", "1")
    _no_stem(monkeypatch)
    monkeypatch.setattr(ctc_align, "retime_segments", lambda *_a, **_kw: None)
    monkeypatch.setattr(
        ctc_align, "last_decline_reason", "short_repeated_motif", raising=False,
    )
    seen = {}

    def _fallback(_audio, lines, *, language=None, job_id=None):
        seen["lines"] = lines
        seen["language"] = language
        seen["job_id"] = job_id
        return _retimed()

    monkeypatch.setattr("lyrics_whisper_align.whisper_word_align", _fallback)
    out = _run(_result(), ANCHOR)

    assert seen == {
        "lines": ANCHOR.splitlines(),
        "language": None,
        "job_id": "j-anchor",
    }
    assert [segment["text"] for segment in out["segments"]] == ANCHOR.splitlines()
    assert out["reference_lyrics"] == ANCHOR
    assert out["anchor_alignment"] == {
        "status": "applied",
        "content_source": "operator_reference",
        "timing_source": "whisper_align",
        "ctc_decline_reason": "short_repeated_motif",
        "original_provider_segment_count": 3,
        "review_count": 2,
    }


def test_ctc_decline_accepts_complete_monotonic_hosted_alignment(monkeypatch):
    monkeypatch.setenv("ANCHOR_LYRICS_ENABLED", "1")
    _no_stem(monkeypatch)
    monkeypatch.setattr(ctc_align, "retime_segments", lambda *_a, **_kw: None)
    monkeypatch.setattr(
        ctc_align, "last_decline_reason", "short_repeated_motif", raising=False,
    )
    monkeypatch.setattr(
        "forced_align.forced_align_lyrics", lambda *_a, **_kw: _retimed(),
    )
    monkeypatch.setattr(
        "lyrics_whisper_align.whisper_word_align",
        lambda *_a, **_kw: (_ for _ in ()).throw(
            AssertionError("Whisper fallback must not run after safe hosted FA")
        ),
    )

    out = _run(_result(), ANCHOR)

    assert out["anchor_alignment"]["status"] == "applied"
    assert out["anchor_alignment"]["timing_source"] == "forced_align"
    assert [segment["text"] for segment in out["segments"]] == ANCHOR.splitlines()


def test_collapsed_hosted_alignment_is_rejected(monkeypatch):
    monkeypatch.setenv("ANCHOR_LYRICS_ENABLED", "1")
    _no_stem(monkeypatch)
    monkeypatch.setattr(ctc_align, "retime_segments", lambda *_a, **_kw: None)
    monkeypatch.setattr(ctc_align, "last_decline_reason", "structural", raising=False)
    collapsed = _retimed()
    collapsed[1]["start"] = collapsed[0]["start"]
    monkeypatch.setattr(
        "forced_align.forced_align_lyrics", lambda *_a, **_kw: collapsed,
    )
    monkeypatch.setattr(
        "lyrics_whisper_align.whisper_word_align", lambda *_a, **_kw: None,
    )

    out = _run(_result(), ANCHOR)

    assert out["anchor_alignment"]["status"] == "declined"
    assert "timing_source" not in out
