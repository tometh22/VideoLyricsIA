"""span_gate — duration-sanity rejection of foreign/longer-version timelines.

The numbers below are the REAL lab measurements (2026-06-01, ~/genly_timing_lab)
on the incident wavs, so the gate is tuned against ground truth, not guesses:
  - cumbia "Luz de Día": lrclib synced last_end 248.6 s on a 169.2 s audio
    (−79.5 s overshoot) → MUST reject.
  - Rata Blanca "La Leyenda": good scaffold last_end 309.1 s on 325.2 s → keep.
  - "Noches Sin Sueño": vocals end ~266 s on a 371 s audio (a ~105 s guitar
    outro) → MUST keep (under-coverage is legitimate, not the gate's job).
"""
import pytest

from timing_confidence import span_gate, SpanVerdict


def _segs(last_end):
    """Two segments ending at `last_end` (start kept < end)."""
    return [
        {"start": 0.0, "end": 1.0, "text": "a"},
        {"start": max(0.0, last_end - 1.0), "end": last_end, "text": "b"},
    ]


# ── the core incident: foreign longer version must be rejected ──────────────

def test_cumbia_overshoot_rejected():
    """The exact lab case: synced 248.6 s on a 169.2 s cumbia → reject."""
    v = span_gate(_segs(248.6), 169.2)
    assert v.ok is False
    assert "overshoot" in v.reason
    assert v.last_end == pytest.approx(248.6)


def test_egregious_overshoot_rejected():
    v = span_gate(_segs(400.0), 200.0)
    assert v.ok is False


# ── legitimate results must pass, including long instrumental outros ────────

def test_rata_good_scaffold_kept():
    """Good Rata Blanca scaffold (309.1 s on 325.2 s audio) → keep."""
    assert span_gate(_segs(309.1), 325.2).ok is True


def test_long_instrumental_outro_kept():
    """Noches Sin Sueño: last lyric 266 s, audio 371 s (~105 s guitar outro).
    Under-coverage is NOT the span gate's job → must keep."""
    assert span_gate(_segs(266.0), 371.0).ok is True


def test_trailing_slack_tolerated():
    """A held final word / reverb a few seconds past the nominal end is normal
    and must NOT be rejected (ratio+pad gives headroom)."""
    assert span_gate(_segs(230.0), 220.0).ok is True   # 230 < 220*1.15+5=258


def test_gasolina_kept():
    assert span_gate(_segs(175.3), 192.7).ok is True


# ── conservative edges ──────────────────────────────────────────────────────

def test_empty_is_not_ok():
    v = span_gate([], 200.0)
    assert v.ok is False and v.reason == "empty"


@pytest.mark.parametrize("dur", [None, 0, 0.0, -3])
def test_missing_duration_does_not_block(dur):
    """No usable duration → we cannot judge, so we do NOT block (other guards
    still apply). reason flags it for observability."""
    v = span_gate(_segs(248.6), dur)
    assert v.ok is True and v.reason == "no_duration"


def test_none_and_malformed_segments_are_safe():
    """Must never raise on None ends / missing keys / junk."""
    segs = [{"start": 1.0}, {"text": "no times"}, {"end": None}, {"end": 150.0}]
    v = span_gate(segs, 200.0)
    assert isinstance(v, SpanVerdict) and v.ok is True
    assert v.last_end == pytest.approx(150.0)


def test_returns_frozen_verdict():
    v = span_gate(_segs(100.0), 200.0)
    with pytest.raises(Exception):
        v.ok = False  # frozen dataclass
