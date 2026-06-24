"""Tests for _ctc_cascade_veto — midpoint-distance matching.

All tests are pure Python, no torch/network/DB required.

Key regression case (No Hay Santos 2:07):
  whisperX places "frágil espejo" at ~110s (cascade seg, ws=0.82)
  Gemini retimes wrong text "tanto miedo tu don" to ~127s
  Midpoint distance = |118.5 - 130.5| = 12s  → within ±20s window → veto fires
  Old overlap-based matching: 0% overlap → veto did NOT fire (regression)
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from ctc_cascade_veto import _ctc_cascade_veto


def _seg(text, start, end, words=None):
    s = {"text": text, "start": start, "end": end}
    if words is not None:
        s["words"] = words
    return s


def _word(w, score=0.85):
    return {"word": w, "score": score, "start": 0.0, "end": 0.5}


def _high_ws_words(text, score=0.82):
    return [_word(w, score) for w in text.split()]


# ── core regression: 2:07 no-hay-santos ──────────────────────────────────────

def test_veto_fires_zero_overlap_within_midpoint_window():
    """whisperX at 110-127s, CTC retimes wrong text to 127-134s.

    Old logic (overlap-based): overlap = 0 → no veto.
    New logic (midpoint): |118.5 - 130.5| = 12s ≤ 20s → veto fires.
    """
    cascade = [
        _seg("frágil espejo de voz", 110.2, 116.8,
             words=_high_ws_words("frágil espejo de voz", score=0.82)),
    ]
    retimed = [
        _seg("tanto miedo tu don", 127.8, 134.1),
    ]
    out = _ctc_cascade_veto(retimed, cascade)
    assert len(out) == 1
    assert out[0]["text"] == "frágil espejo de voz", (
        f"Expected cascade text, got: {out[0]['text']!r}"
    )


def test_veto_fires_whisperx_at_126s_variant():
    """Same song, different whisperX run — now places 'frágil espejo' at 126s.

    CTC still retimes wrong text to 127.8s (1.8s midpoint distance).
    Veto must fire in both runs.
    """
    cascade = [
        _seg("frágil espejo de voz", 126.0, 132.5,
             words=_high_ws_words("frágil espejo de voz", score=0.78)),
    ]
    retimed = [
        _seg("tanto miedo tu don", 127.8, 134.1),
    ]
    out = _ctc_cascade_veto(retimed, cascade)
    assert out[0]["text"] == "frágil espejo de voz"


def test_veto_blocked_outside_midpoint_window():
    """Cascade midpoint 40s away → outside ±20s window → veto doesn't fire."""
    cascade = [
        _seg("frágil espejo de voz", 60.0, 67.0,
             words=_high_ws_words("frágil espejo de voz", score=0.85)),
    ]
    retimed = [
        _seg("tanto miedo tu don", 127.8, 134.1),
    ]
    out = _ctc_cascade_veto(retimed, cascade)
    assert out[0]["text"] == "tanto miedo tu don"


def test_veto_blocked_by_low_ws():
    """Cascade ws < 0.70 (unreliable) → veto doesn't fire even within window."""
    cascade = [
        _seg("frágil espejo de voz", 110.0, 117.0,
             words=_high_ws_words("frágil espejo de voz", score=0.55)),
    ]
    retimed = [_seg("tanto miedo tu don", 127.8, 134.1)]
    out = _ctc_cascade_veto(retimed, cascade)
    assert out[0]["text"] == "tanto miedo tu don"


def test_veto_blocked_by_high_containment():
    """Gemini correcting/extending cascade (≥40% overlap of cascade words) → let through."""
    cascade = [
        _seg("tu piel se siente fría", 50.0, 55.0,
             words=_high_ws_words("tu piel se siente fría", score=0.80)),
    ]
    # Gemini adds context but most cascade words present → containment high
    retimed = [_seg("Dentro de tu piel se siente fría", 50.5, 56.0)]
    out = _ctc_cascade_veto(retimed, cascade)
    assert out[0]["text"] == "Dentro de tu piel se siente fría"


def test_veto_blocked_same_text():
    """Texts identical → no change needed → pass through."""
    cascade = [
        _seg("frágil espejo de voz", 110.0, 117.0,
             words=_high_ws_words("frágil espejo de voz")),
    ]
    retimed = [_seg("frágil espejo de voz", 127.8, 134.1)]
    out = _ctc_cascade_veto(retimed, cascade)
    assert out[0]["text"] == "frágil espejo de voz"
    assert out[0]["start"] == 127.8


def test_veto_blocked_cascade_too_short():
    """Cascade with < 3 unique tokens → containment guard short-circuits → pass through."""
    cascade = [
        _seg("uh uh", 110.0, 112.0,
             words=_high_ws_words("uh uh", score=0.90)),
    ]
    retimed = [_seg("tanto miedo tu don", 127.8, 134.1)]
    out = _ctc_cascade_veto(retimed, cascade)
    assert out[0]["text"] == "tanto miedo tu don"


def test_veto_picks_nearest_of_multiple_candidates():
    """Two cascade segments within window — veto should pick the nearest by midpoint."""
    cascade = [
        _seg("segmento lejano aquí", 100.0, 106.0,     # midpoint 103, dist = 27.5
             words=_high_ws_words("segmento lejano aquí", score=0.85)),
        _seg("frágil espejo de voz", 118.0, 124.0,     # midpoint 121, dist = 9.5
             words=_high_ws_words("frágil espejo de voz", score=0.82)),
    ]
    retimed = [_seg("tanto miedo tu don", 127.0, 133.0)]  # midpoint 130
    out = _ctc_cascade_veto(retimed, cascade)
    # "segmento lejano aquí": dist = |103 - 130| = 27 > 20 → outside window
    # "frágil espejo de voz": dist = |121 - 130| = 9  ≤ 20 → wins
    assert out[0]["text"] == "frágil espejo de voz"


def test_unaffected_segments_pass_through():
    """Segments without a nearby high-ws match are returned unchanged."""
    cascade = [
        _seg("frágil espejo de voz", 110.0, 117.0,
             words=_high_ws_words("frágil espejo de voz")),
    ]
    retimed = [
        _seg("primera línea correcta", 10.0, 15.0),       # dist = 151s → no match
        _seg("tanto miedo tu don", 127.8, 134.1),          # dist = 14s → veto
        _seg("última línea también bien", 190.0, 195.0),   # dist = 78s → no match
    ]
    out = _ctc_cascade_veto(retimed, cascade)
    assert len(out) == 3
    assert out[0]["text"] == "primera línea correcta"
    assert out[1]["text"] == "frágil espejo de voz"
    assert out[2]["text"] == "última línea también bien"


def test_empty_cascade_returns_retimed_unchanged():
    retimed = [_seg("alguna línea", 10.0, 15.0)]
    out = _ctc_cascade_veto(retimed, [])
    assert out == retimed


def test_empty_retimed_returns_empty():
    cascade = [_seg("algo", 10.0, 15.0, words=_high_ws_words("algo"))]
    assert _ctc_cascade_veto([], cascade) == []


def test_ws_floor_env_override(monkeypatch):
    """CTC_CASCADE_VETO_WS_FLOOR env var overrides default 0.70."""
    monkeypatch.setenv("CTC_CASCADE_VETO_WS_FLOOR", "0.95")
    # ws=0.82 is fine for default 0.70 but fails at 0.95 → no veto
    cascade = [
        _seg("frágil espejo de voz", 110.0, 117.0,
             words=_high_ws_words("frágil espejo de voz", score=0.82)),
    ]
    retimed = [_seg("tanto miedo tu don", 127.8, 134.1)]
    out = _ctc_cascade_veto(retimed, cascade)
    assert out[0]["text"] == "tanto miedo tu don"
