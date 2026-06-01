"""build_synced_scaffold — VAD-validated lrclib-synced scaffold (Stage 3).

Scenarios mirror the real lab measurements (~/genly_timing_lab, 2026-06-01):
  - Rata Blanca: synced lines through ~304s, whisperX voice 8-318s on a 325s
    audio → ACCEPT (clean, offset-anchored, no pile-up).
  - cumbia "Luz de Día": synced runs to 248s on a 169s audio → REJECT (span).
  - Soda live: synced (studio) compressed vs the live arrangement, lines land
    off the actual voice → REJECT (low voice coverage / under-coverage).
"""
import pytest

from anchor_align import build_synced_scaffold, vocal_regions


def _pairs(start, step, n, text="línea"):
    return [(round(start + i * step, 2), f"{text} {i}") for i in range(n)]


def _wx(first_word, last_word):
    """Minimal whisperX-shaped segments carrying a first + last word stamp."""
    return [
        {"start": first_word, "end": first_word + 0.5,
         "words": [{"word": "a", "start": first_word, "end": first_word + 0.4}]},
        {"start": last_word - 0.5, "end": last_word,
         "words": [{"word": "z", "start": last_word - 0.4, "end": last_word}]},
    ]


# ── ACCEPT: good synced that matches this recording ─────────────────────────

def test_rata_blanca_accepts_and_anchors_offset():
    pairs = _pairs(10.0, 6.5, 45)            # 45 lines, last ~296s
    wx = _wx(first_word=11.5, last_word=318.0)
    regions = [(8.0, 318.0)]                  # voice across the song
    segs, meta = build_synced_scaffold(pairs, wx, 325.2, vocal_regions=regions)
    assert segs is not None
    assert len(segs) == 45
    assert meta["reason"] == "ok"
    # offset = first whisperX word (11.5) − first synced line (10.0) = +1.5
    assert meta["offset"] == pytest.approx(1.5, abs=0.01)
    assert segs[0]["start"] == pytest.approx(11.5, abs=0.05)
    assert meta["frac_in_voice"] >= 0.70
    # repetition-safe: every line keeps its own slot (no pile-up)
    starts = [s["start"] for s in segs]
    assert len(set(starts)) == len(starts)
    assert all(s["start"] <= s["end"] for s in segs)


def test_reggaeton_repetition_each_line_own_slot():
    """Heavy repeated chorus (Gasolina/Tití): the scaffold must keep each
    repeat in its own timed slot — the thing forced_align piles up."""
    pairs = [(100.0, "Dame más gasolina"), (102.0, "Dame más gasolina"),
             (104.0, "Dame más gasolina"), (106.0, "Dame más gasolina"),
             (108.0, "Dame más gasolina")]
    wx = _wx(first_word=100.0, last_word=112.0)   # last word near the last line
    segs, meta = build_synced_scaffold(pairs, wx, 193.0, vocal_regions=[(95.0, 115.0)])
    assert segs is not None and len(segs) == 5, meta
    assert len({s["start"] for s in segs}) == 5   # NOT piled at one timestamp


# ── REJECT: foreign / mismatched version ────────────────────────────────────

def test_cumbia_overshoot_rejected():
    """synced 248s on a 169s audio → span gate rejects → fall through."""
    pairs = _pairs(5.0, 6.0, 41)              # last ~245s
    wx = _wx(first_word=5.0, last_word=155.0)
    segs, meta = build_synced_scaffold(pairs, wx, 169.2, vocal_regions=[(5.0, 158.0)])
    assert segs is None
    assert meta["reason"].startswith("span_gate")


def test_live_version_low_voice_coverage_rejected():
    """Soda live: studio synced lines fall where the live audio has no voice
    (compressed/rearranged) → low coverage → reject."""
    pairs = _pairs(10.0, 8.0, 30)             # lines spread 10..242
    wx = _wx(first_word=12.0, last_word=245.0)
    regions = [(12.0, 90.0)]                   # voice only in the first third
    segs, meta = build_synced_scaffold(pairs, wx, 289.0, vocal_regions=regions)
    assert segs is None
    assert meta["reason"].startswith("low_voice_coverage")


def test_under_coverage_rejected():
    """synced ends long before the last sung word (too-short/foreign) → reject."""
    pairs = _pairs(10.0, 4.0, 30)             # last ~126s
    wx = _wx(first_word=10.0, last_word=245.0)  # voice continues to 245s
    regions = [(8.0, 250.0)]
    segs, meta = build_synced_scaffold(pairs, wx, 289.0, vocal_regions=regions)
    assert segs is None
    assert meta["reason"].startswith("under_coverage")


# ── edges ────────────────────────────────────────────────────────────────────

def test_too_few_lines():
    segs, meta = build_synced_scaffold(_pairs(0, 5, 3), _wx(1, 50), 100.0,
                                       vocal_regions=[(0, 60)])
    assert segs is None and meta["reason"] == "too_few_synced_lines"


def test_offset_out_of_range_uses_zero():
    """A wild first-word anchor (>60s off) must not shift everything by garbage."""
    pairs = _pairs(10.0, 5.0, 20)
    wx = _wx(first_word=200.0, last_word=260.0)   # 190s off → ignore
    segs, meta = build_synced_scaffold(pairs, wx, 300.0, vocal_regions=[(0, 280)])
    assert meta["offset"] == 0.0


def test_no_vad_falls_back_to_span_only():
    """No VAD signal (librosa missing / silent stem) → span gate only."""
    pairs = _pairs(10.0, 6.0, 20)             # last ~124s
    segs, meta = build_synced_scaffold(pairs, _wx(10, 130), 200.0, vocal_regions=[])
    assert segs is not None
    assert meta["frac_in_voice"] == -1.0


def test_vocal_regions_missing_file_is_safe():
    """vocal_regions never raises on a bad path."""
    assert vocal_regions("/nonexistent/stem.wav") == []


def test_emitted_source_is_valid():
    from timing_sources import VALID_TIMING_SOURCES, SYNCED_SCAFFOLD
    assert SYNCED_SCAFFOLD in VALID_TIMING_SOURCES
