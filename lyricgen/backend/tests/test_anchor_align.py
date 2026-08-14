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


@pytest.fixture(autouse=True)
def _enable_onset(monkeypatch):
    """Stage 3.1 is gated OFF by default; enable it for the behavior tests."""
    monkeypatch.setenv("ANCHOR_ONSET_ENABLED", "1")


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


# ── anchor-based alignment (drift correction) ───────────────────────────────

from anchor_align import _norm_word, _build_anchors, _fit_alignment, _wx_word_stream


def test_norm_word_strips_accents_punct():
    assert _norm_word("Amándose,") == "amandose"
    assert _norm_word("¡Yeah!") == "yeah"
    assert _norm_word("  ") == ""


def _wxw(words):
    """words = [(word, start)] → whisperX-shaped segs."""
    return [{"start": 0.0, "end": 400.0,
             "words": [{"word": w, "start": t} for w, t in words]}]


def test_anchor_fit_corrects_local_drift():
    """Rata Blanca scenario: synced lines are ~1.4s LATE vs the real vocal
    (the synced version was chosen by duration, not timing). whisperX heard the
    distinctive words at the true times → the fit pulls every line back, so
    'Cuenta la historia' lands ~14.2s, NOT 15.6s. The faint intro 'Uh, no, no'
    (no distinctive word) is NOT used as an anchor."""
    pairs = [
        (7.6, "Uh, no, no"),                                  # ad-lib: no anchor
        (15.6, "Cuenta la historia de un mago"),              # cuenta
        (50.0, "Amándose siempre y en todo lugar"),           # amandose
        (120.0, "Buscando la forma de recuperar"),            # buscando
        (260.0, "Para siempre con él se quedará"),            # quedara
    ]
    wx = _wxw([("cuenta", 14.2), ("historia", 14.8),
               ("amandose", 48.6), ("buscando", 118.6), ("quedara", 258.6)])
    segs, meta = build_synced_scaffold(pairs, wx, 325.0, vocal_regions=[(5.0, 300.0)])
    assert segs is not None, meta
    assert meta["align"]["anchors"] >= 3
    cuenta = next(s for s in segs if "Cuenta la historia" in s["text"])
    assert 13.6 <= cuenta["start"] <= 14.8, f"Cuenta at {cuenta['start']} (want ~14.2)"


def test_build_anchors_monotonic_and_distinctive():
    pairs = [(10.0, "Cuenta la historia"), (20.0, "que los unos con las")]
    wx = _wxw([("cuenta", 9.0), ("historia", 9.6)])
    anchors = _build_anchors(pairs, _wx_word_stream(wx))
    # line 1 anchors on 'cuenta'; line 2 is all stopwords/short → no anchor
    assert anchors == [(10.0, 9.0)]


def test_fit_few_anchors_falls_back_to_offset():
    """<3 anchors → median offset (a=1), no risky stretch."""
    pairs = [(10.0, "Cuenta algo"), (20.0, "linea corta"), (30.0, "otra mas")]
    wx = _wxw([("cuenta", 8.0)])
    a, b, n = _fit_alignment(pairs, wx)
    assert a == 1.0 and n == 1 and b == pytest.approx(-2.0)


def test_fit_no_anchors_uses_first_word_offset():
    """No distinctive matches AND no intro gap → plain first-word offset."""
    pairs = [(10.0, "la la la"), (20.0, "na na na")]
    wx = _wxw([("xyz", 7.0)])   # nothing matches the canonical words
    a, b, n = _fit_alignment(pairs, wx)
    assert a == 1.0 and n == 0 and b == pytest.approx(-3.0)  # 7.0 - 10.0


def test_flag_off_uses_legacy_first_word_offset(monkeypatch):
    """With ANCHOR_ONSET_ENABLED off, _fit_alignment must be the #513 behavior:
    a single global offset = first whisperX word − first synced line (no
    vocal-onset / multi-anchor logic), so the rollout is inert until flipped."""
    monkeypatch.setenv("ANCHOR_ONSET_ENABLED", "0")
    pairs = [(6.4, "Uh, no, no"), (14.34, "Cuenta la historia de un mago"),
             (50.0, "Amándose siempre"), (80.0, "Buscando la forma")]
    wx = _wxw([("no", 7.5), ("nohabia", 14.71)])
    a, b, n = _fit_alignment(pairs, wx)
    assert a == 1.0 and n == 0
    assert b == pytest.approx(7.5 - 6.4)  # first word − first line (legacy)


def test_vocal_onset_anchor_when_whisperx_mishears():
    """Rata Blanca's real failure mode: whisperX mis-hears the first verse so NO
    distinctive word matches — but there is a clear vocal onset after the intro
    gap. The first verse must anchor to that onset (timing, not text), so 'Cuenta'
    lands ~14.7s (the real onset) instead of its raw synced 14.34+drift."""
    pairs = [
        (6.4, "Uh, no, no"),                        # intro ad-lib: no anchor word
        (14.34, "Cuenta la historia de un mago"),   # first verse (raw synced)
        (50.0, "Amándose siempre y en todo lugar"),
        (80.0, "Buscando la forma de recuperar"),
    ]
    # whisperX: intro blip "no no no" @7.5-7.86, a >3s gap, then a MIS-HEARD verse
    # whose words match nothing in the canonical:
    wx = _wxw([("no", 7.5), ("no", 7.7), ("no", 7.86),
               ("nohabia", 14.71), ("podidoo", 14.9), ("amorr", 15.5)])
    segs, meta = build_synced_scaffold(pairs, wx, 325.0, vocal_regions=[(5.0, 300.0)])
    assert segs is not None, meta
    assert meta["align"]["anchors"] == 0          # no word anchors → vocal-onset path
    cuenta = next(s for s in segs if "Cuenta" in s["text"])
    assert 14.3 <= cuenta["start"] <= 15.1, f"Cuenta at {cuenta['start']} (want ~14.7)"


# ── REJECT: modest overshoot the shared 15% default used to let through ─────

def test_modest_overshoot_rejected_scaffold_gate_is_strict():
    """Luciano Pereyra "Una Mujer Como Tu" (ft. Los Ángeles Azules, live):
    224 s audio, scaffold ran to ~234 s. Under the shared span_gate default
    (ratio 1.15 → a 262 s limit) that sailed through and shipped a whole song
    of shifted lines. A scaffold maps lrclib's timeline onto THIS recording, so
    landing past the audio end means the mapping is wrong — not a held note."""
    pairs = _pairs(12.0, 5.5, 41)             # last line ~232s
    wx = _wx(first_word=12.0, last_word=213.0)
    segs, meta = build_synced_scaffold(pairs, wx, 224.0,
                                       vocal_regions=[(10.0, 215.0)])
    assert segs is None, f"expected rejection, got {len(segs or [])} lines"
    assert meta["reason"].startswith("span_gate")


def test_scaffold_ending_within_the_audio_still_accepted():
    """Non-regression for the tightened ratio: a scaffold that fits must pass,
    including the synthetic +3 s tail this builder gives its last line."""
    pairs = _pairs(10.0, 6.5, 30)             # last line ~198.5s (+3s tail)
    wx = _wx(first_word=10.0, last_word=205.0)
    segs, meta = build_synced_scaffold(pairs, wx, 210.0,
                                       vocal_regions=[(8.0, 206.0)])
    assert segs is not None, meta
    assert meta["reason"] == "ok"
