"""Unit tests for whisperx_transcribe — pure mapping + gating (no network)."""

import sys
import types
from unittest.mock import MagicMock

import whisperx_transcribe as wx


def _fake_replicate(monkeypatch, *, run):
    """Inject a stand-in `replicate` module (SDK not installed in test venv)."""
    fake = types.ModuleType("replicate")
    fake.run = run
    monkeypatch.setitem(sys.modules, "replicate", fake)


def test_is_enabled_requires_flag_and_token(monkeypatch):
    monkeypatch.delenv("WHISPERX_ENABLED", raising=False)
    monkeypatch.delenv("REPLICATE_API_TOKEN", raising=False)
    assert wx.is_enabled() is False
    monkeypatch.setenv("WHISPERX_ENABLED", "1")
    assert wx.is_enabled() is False
    monkeypatch.setenv("REPLICATE_API_TOKEN", "r8_x")
    assert wx.is_enabled() is True


def test_map_segments_basic_with_words():
    out = {
        "segments": [
            {"start": 0.0, "end": 2.0, "text": " Hola mundo ",
             "words": [
                 {"word": "Hola", "start": 0.0, "end": 1.0, "score": 0.9},
                 {"word": "mundo", "start": 1.0, "end": 2.0, "score": 0.8},
             ]},
            {"start": 2.0, "end": 4.0, "text": "Chau", "words": []},
        ],
        "detected_language": "es",
    }
    segs = wx._map_segments(out)
    assert len(segs) == 2
    assert segs[0]["text"] == "Hola mundo"
    # Words preserve `score` (whisperX confidence per word) when present —
    # enables confidence-highlighting in the editor.
    assert segs[0]["words"] == [
        {"word": "Hola", "start": 0.0, "end": 1.0, "score": 0.9},
        {"word": "mundo", "start": 1.0, "end": 2.0, "score": 0.8},
    ]
    # Empty words list → no "words" key.
    assert "words" not in segs[1]


def test_map_segments_skips_blank_and_bad_bounds():
    out = {"segments": [
        {"start": 0.0, "end": 1.0, "text": "   "},          # blank → skip
        {"start": "x", "end": 1.0, "text": "bad start"},     # non-numeric → skip
        {"start": 1.0, "end": 2.0, "text": "ok"},
    ]}
    segs = wx._map_segments(out)
    assert [s["text"] for s in segs] == ["ok"]


def test_map_segments_clamps_inverted_bounds():
    out = {"segments": [{"start": 5.0, "end": 3.0, "text": "raro"}]}
    segs = wx._map_segments(out)
    assert segs[0]["start"] == 5.0 and segs[0]["end"] == 5.0


def test_map_segments_drops_words_missing_stamps():
    out = {"segments": [{"start": 0.0, "end": 2.0, "text": "a b",
                         "words": [
                             {"word": "a", "start": 0.0, "end": 1.0},
                             {"word": "b"},  # no stamps (non-alignable) → drop
                         ]}]}
    segs = wx._map_segments(out)
    assert segs[0]["words"] == [{"word": "a", "start": 0.0, "end": 1.0}]


def test_map_segments_handles_non_dict_output():
    assert wx._map_segments(None) == []
    assert wx._map_segments("nope") == []
    # bare list of segments also accepted
    assert wx._map_segments([{"start": 0, "end": 1, "text": "hi"}])[0]["text"] == "hi"


def test_filter_ghosts_drops_short_oneword_segments():
    # The El Arbol smoke produced an 'Amén' at 5.15s for 0.18s (1 word).
    # That's whisperX false-flagging an instrumental sound as speech.
    segs = [
        {"start": 5.15, "end": 5.33, "text": "Amén"},                   # ghost
        {"start": 53.27, "end": 68.80, "text": "Después de tanto vagar"},  # real
        {"start": 70.0, "end": 70.1, "text": "x"},                       # ghost
    ]
    kept = wx._filter_ghosts(segs)
    assert [s["text"] for s in kept] == ["Después de tanto vagar"]


def test_filter_ghosts_keeps_sustained_chant():
    # A real chanted vocalisation can be one word held longer than 0.5s
    # (e.g., '¡Karol!' in a chorus) — must NOT be filtered.
    segs = [{"start": 10.0, "end": 10.7, "text": "Karol"}]
    assert wx._filter_ghosts(segs) == segs


def _w(word, start, end):
    return {"word": word, "start": start, "end": end}


def test_split_long_segments_at_biggest_gap():
    # Real El Arbol shape: a 25-word segment held for 16s. WhisperX's native
    # segmentation lumps 'Después de tanto vagar por las calles' together
    # with 'La ciudad te parece tan gris'. The split should land at the
    # natural pause between them.
    words = [
        _w("Después", 53.27, 53.6), _w("de", 53.6, 53.7),
        _w("tanto", 53.7, 54.0), _w("vagar", 54.0, 54.5),
        _w("por", 54.5, 54.7), _w("las", 54.7, 54.85),
        _w("calles", 54.85, 56.77),
        # 1.5s instrumental gap here (sung phrase boundary)
        _w("La", 58.30, 58.5), _w("ciudad", 58.5, 58.9),
        _w("te", 58.9, 59.0), _w("parece", 59.0, 59.5),
        _w("tan", 59.5, 59.8), _w("gris", 59.8, 60.5),
    ]
    seg = {"start": 53.27, "end": 60.5,
           "text": "Después de tanto vagar por las calles La ciudad te parece tan gris",
           "words": words}
    # Use a low max_dur to force the split for the test.
    out = wx._split_long_segments([seg], max_dur=5.0, min_split_gap=0.3)
    assert len(out) == 2
    assert out[0]["text"].startswith("Después") and out[0]["text"].endswith("calles")
    assert out[1]["text"].startswith("La") and out[1]["text"].endswith("gris")
    assert out[0]["end"] == 56.77
    assert out[1]["start"] == 58.30


def test_split_long_segments_does_not_split_continuous_speech():
    # No internal pause > min_split_gap: leave the segment alone even if long.
    words = [_w(f"w{i}", i * 0.3, (i + 1) * 0.3) for i in range(40)]  # ~12s, gaps = 0
    seg = {"start": 0.0, "end": 12.0, "text": "x" * 40, "words": words}
    out = wx._split_long_segments([seg], max_dur=5.0, min_split_gap=0.3)
    assert out == [seg]


def test_split_long_segments_splits_at_small_gap_when_forced():
    # Real El Arbol case: a 8.7s line with a ~0.15s pause at the phrase
    # boundary "calles | La". Old default (min_split_gap=0.3) wouldn't fire;
    # new behaviour (min_split_gap=0, max_dur=6) MUST split here so the
    # line doesn't show as one 8.7s wall.
    words = [
        _w("Después", 53.0, 53.3), _w("de", 53.3, 53.4),
        _w("tanto", 53.4, 53.7), _w("vagar", 53.7, 54.1),
        _w("por", 54.1, 54.3), _w("las", 54.3, 54.45),
        _w("calles", 54.45, 56.4),
        # Only ~0.15s pause (below the old 0.3 threshold)
        _w("La", 56.55, 56.7), _w("ciudad", 56.7, 57.1),
        _w("te", 57.1, 57.2), _w("parece", 57.2, 57.7),
        _w("tan", 57.7, 58.0), _w("gris", 58.0, 58.7),
    ]
    seg = {"start": 53.0, "end": 58.7,
           "text": "Después de tanto vagar por las calles La ciudad te parece tan gris",
           "words": words}
    out = wx._split_long_segments([seg], max_dur=5.0, min_split_gap=0.0)
    assert len(out) == 2
    assert out[0]["text"].endswith("calles")
    assert out[1]["text"].startswith("La")


def test_apply_lead_in_pulls_starts_earlier():
    # User-visible ms delay: timestamps are ±100ms behind the audible vocal.
    # Lead-in shifts each start ~120ms earlier (anticipation), capped by the
    # previous segment's end and by 0.
    segs = [
        {"start": 0.50, "end": 2.00, "text": "uno"},
        {"start": 3.00, "end": 4.00, "text": "dos"},
    ]
    out = wx._apply_lead_in(segs, lead_ms=120)
    assert abs(out[0]["start"] - 0.38) < 1e-6     # 0.50 - 0.120
    assert abs(out[1]["start"] - 2.88) < 1e-6     # 3.00 - 0.120
    # Ends untouched.
    assert out[0]["end"] == 2.00 and out[1]["end"] == 4.00


def test_apply_lead_in_does_not_overlap_previous():
    segs = [
        {"start": 0.50, "end": 2.50, "text": "uno"},
        {"start": 2.55, "end": 3.50, "text": "dos"},   # only 50ms gap
    ]
    out = wx._apply_lead_in(segs, lead_ms=120)
    # Second start would be 2.43 but prev_end=2.50 → clamp to 2.505
    assert out[1]["start"] >= 2.50
    assert out[1]["start"] <= 2.55


def test_apply_lead_in_clamps_at_zero():
    segs = [{"start": 0.05, "end": 1.00, "text": "x"}]
    out = wx._apply_lead_in(segs, lead_ms=120)
    assert out[0]["start"] == 0.0


def test_apply_lead_in_noop_when_disabled():
    segs = [{"start": 0.5, "end": 2.0, "text": "x"}]
    assert wx._apply_lead_in(segs, lead_ms=0) == segs


# ───── 2026-05-31 regression tests (Agus production report) ───────────


def test_apply_lead_in_default_is_80ms_not_120(monkeypatch):
    """Pin the new default. 120ms was at the karaoke 'feel' threshold
    (~100ms) and trained ears (UMG ops reviewers) read it as the line
    landing AHEAD of the vocal. 80ms keeps the anticipation while
    sitting safely below perception. Any future bump above 100ms must
    update this test."""
    monkeypatch.delenv("LYRIC_LEAD_IN_MS", raising=False)
    segs = [{"start": 0.50, "end": 2.00, "text": "uno"}]
    out = wx._apply_lead_in(segs)   # NO lead_ms kwarg → uses default
    # 0.50 - 0.080 = 0.42
    assert abs(out[0]["start"] - 0.42) < 1e-6


def test_apply_lead_in_env_override_still_works(monkeypatch):
    """The env var is the prod escape hatch. If we ever want the old
    120ms back without redeploying, LYRIC_LEAD_IN_MS=120 must do it."""
    monkeypatch.setenv("LYRIC_LEAD_IN_MS", "120")
    segs = [{"start": 0.50, "end": 2.00, "text": "uno"}]
    out = wx._apply_lead_in(segs)
    assert abs(out[0]["start"] - 0.38) < 1e-6   # 0.50 - 0.120


def test_apply_lead_in_env_garbage_falls_back_to_default(monkeypatch):
    """A bad env value must not crash the pipeline — it falls back to
    the hardcoded default (currently 80ms)."""
    monkeypatch.setenv("LYRIC_LEAD_IN_MS", "not_a_number")
    segs = [{"start": 0.50, "end": 2.00, "text": "uno"}]
    out = wx._apply_lead_in(segs)
    # Falls back to _DEFAULT_LEAD_MS (80ms) → 0.42
    assert abs(out[0]["start"] - 0.42) < 1e-6


def test_split_long_segments_passes_through_short_or_wordless():
    short = {"start": 0.0, "end": 4.0, "text": "ok", "words": [_w("ok", 0.0, 4.0)]}
    no_words = {"start": 0.0, "end": 20.0, "text": "no word stamps"}
    out = wx._split_long_segments([short, no_words], max_dur=5.0)
    assert out == [short, no_words]


def test_transcribe_none_when_disabled(monkeypatch, tmp_path):
    monkeypatch.delenv("WHISPERX_ENABLED", raising=False)
    f = tmp_path / "a.mp3"
    f.write_bytes(b"x")
    assert wx.transcribe_whisperx(str(f)) is None


def test_transcribe_none_on_thin_result(monkeypatch, tmp_path):
    monkeypatch.setenv("WHISPERX_ENABLED", "1")
    monkeypatch.setenv("REPLICATE_API_TOKEN", "r8_x")
    f = tmp_path / "a.mp3"
    f.write_bytes(b"x")
    _fake_replicate(monkeypatch, run=MagicMock(return_value={"segments": [
        {"start": 0, "end": 1, "text": "only one"}]}))
    assert wx.transcribe_whisperx(str(f)) is None  # < 2 segs → fall back


def test_transcribe_maps_on_success(monkeypatch, tmp_path):
    monkeypatch.setenv("WHISPERX_ENABLED", "1")
    monkeypatch.setenv("REPLICATE_API_TOKEN", "r8_x")
    f = tmp_path / "a.mp3"
    f.write_bytes(b"x")
    out = {"segments": [
        {"start": 0, "end": 1, "text": "uno"},
        {"start": 1, "end": 2, "text": "dos"},
    ]}
    _fake_replicate(monkeypatch, run=MagicMock(return_value=out))
    segs = wx.transcribe_whisperx(str(f), language="es")
    assert [s["text"] for s in segs] == ["uno", "dos"]


# ──────────────────────────────────────────────────────────────────────
# VAD-first ad-lib splitting (PR #708)
# ──────────────────────────────────────────────────────────────────────

def _make_seg(text, start, end, words=None):
    s = {"start": start, "end": end, "text": text}
    if words is not None:
        s["words"] = words
    return s


def test_vad_split_adlib_splits_on_silence(monkeypatch, tmp_path):
    """Happy path: VAD finds 3 regions → all re-transcriptions succeed → 3 sub-segments.

    The following normal segment starts 3s after the adlib end (gap > _ADLIB_NEXT_MERGE_GAP_S),
    so lookforward does NOT apply — the normal segment passes through unchanged.
    """
    f = tmp_path / "vocals.wav"
    f.write_bytes(b"RIFF\x00\x00\x00\x00WAVE" + b"\x00" * 256)
    audio_path = str(f)

    monkeypatch.setattr(wx, "_detect_adlib_voice_regions",
                        lambda *a, **k: [(73.0, 84.0), (84.5, 92.5), (93.0, 101.0)])

    def _fake_retranscribe(audio_path, start_s, end_s, language=None):
        return [{"start": start_s + 0.1, "end": end_s - 0.1, "text": "uh uh uh"}]

    monkeypatch.setattr(wx, "_retranscribe_slice", _fake_retranscribe)
    monkeypatch.setenv("ADLIB_VAD_RETRANSCRIBE_ENABLED", "1")

    mega = _make_seg("uh uh uh uh uh uh uh uh uh uh", 69.0, 101.0)
    # 3s gap → outside _ADLIB_NEXT_MERGE_GAP_S (2s) → no lookforward
    normal = _make_seg("Tomas del miedo tu don", 104.0, 109.0)
    out = wx._vad_split_adlib_segments([mega, normal], audio_path, language="es")

    assert len(out) == 4, f"expected 3 sub-segs + 1 normal = 4, got {len(out)}: {out}"
    assert out[0]["start"] >= 73.0
    assert out[-1]["text"] == "Tomas del miedo tu don"


def test_vad_split_extends_window_for_mislabeled_tail(monkeypatch, tmp_path):
    """Lookforward fixes the No Hay Santos 1:35 bug.

    whisperX places the adlib at 69.4-95.2s and misidentifies the adlib tail
    as "Tomas del miedo tu don" starting at 95.2s (gap=0 ≤ 2s). The new code
    extends the VAD window to 110.2s. VAD finds the real singing at 102s and
    re-transcribes it with correct timing. Both original segments are consumed.
    """
    import pytest
    f = tmp_path / "vocals.wav"
    f.write_bytes(b"RIFF\x00\x00\x00\x00WAVE" + b"\x00" * 256)
    audio_path = str(f)

    # Extended window: 69.4-110.2s → VAD finds 3 uh clusters + 1 real lyric region
    monkeypatch.setattr(
        wx, "_detect_adlib_voice_regions",
        lambda *a, **k: [(73.0, 84.0), (84.5, 92.5), (93.0, 101.0), (102.0, 107.0)],
    )

    def _fake_retranscribe(audio_path, start_s, end_s, language=None):
        if start_s >= 102.0:
            return [{"start": 102.3, "end": 105.1, "text": "Tomas del miedo tu don"}]
        return [{"start": start_s + 0.1, "end": end_s - 0.1, "text": "uh uh uh"}]

    monkeypatch.setattr(wx, "_retranscribe_slice", _fake_retranscribe)
    monkeypatch.setenv("ADLIB_VAD_RETRANSCRIBE_ENABLED", "1")

    mega = _make_seg("uh uh uh uh uh uh uh uh uh uh", 69.4, 95.2)
    mislabeled = _make_seg("Tomas del miedo tu don", 95.2, 110.2)  # gap=0 → lookforward
    out = wx._vad_split_adlib_segments([mega, mislabeled], audio_path, language="es")

    # Both original segs consumed; replaced by 3 uh + 1 lyric
    assert len(out) == 4, f"expected 4 sub-segs, got {len(out)}: {out}"
    lyric_seg = next((s for s in out if "Tomas" in s.get("text", "")), None)
    assert lyric_seg is not None, "Lyric segment must appear in output"
    assert lyric_seg["start"] == pytest.approx(102.3, abs=0.01), (
        f"Expected ~102.3s, got {lyric_seg['start']:.1f}s — lookforward fix failed"
    )
    assert lyric_seg["start"] > 100.0, "Timing must be past the adlib, not at 95s"


def test_vad_split_falls_back_when_single_region(monkeypatch, tmp_path):
    """VAD finds only 1 region (no clear silences) → original segment unchanged."""
    f = tmp_path / "vocals.wav"
    f.write_bytes(b"RIFF\x00\x00\x00\x00WAVE" + b"\x00" * 256)
    audio_path = str(f)

    monkeypatch.setattr(wx, "_detect_adlib_voice_regions",
                        lambda *a, **k: [(69.0, 101.0)])
    monkeypatch.setenv("ADLIB_VAD_RETRANSCRIBE_ENABLED", "1")

    mega = _make_seg("uh uh uh uh uh uh uh uh uh uh", 69.0, 101.0)
    out = wx._vad_split_adlib_segments([mega], audio_path)
    assert len(out) == 1
    assert out[0] is mega


def test_vad_split_falls_back_when_retranscribe_fails(monkeypatch, tmp_path):
    """If ANY region retranscription returns None, entire original segment is kept."""
    f = tmp_path / "vocals.wav"
    f.write_bytes(b"RIFF\x00\x00\x00\x00WAVE" + b"\x00" * 256)
    audio_path = str(f)

    monkeypatch.setattr(wx, "_detect_adlib_voice_regions",
                        lambda *a, **k: [(73.0, 84.0), (84.5, 92.5)])

    call_count = {"n": 0}

    def _partial_fail(audio_path, start_s, end_s, language=None):
        call_count["n"] += 1
        if call_count["n"] > 1:
            return None
        return [{"start": start_s + 0.1, "end": end_s - 0.1, "text": "uh uh uh"}]

    monkeypatch.setattr(wx, "_retranscribe_slice", _partial_fail)
    monkeypatch.setenv("ADLIB_VAD_RETRANSCRIBE_ENABLED", "1")

    mega = _make_seg("uh uh uh uh uh uh uh uh uh uh", 69.0, 101.0)
    out = wx._vad_split_adlib_segments([mega], audio_path)
    assert len(out) == 1
    assert out[0] is mega


def test_vad_split_noop_when_disabled(monkeypatch, tmp_path):
    """ADLIB_VAD_RETRANSCRIBE_ENABLED=0 must be a complete no-op."""
    f = tmp_path / "vocals.wav"
    f.write_bytes(b"RIFF\x00\x00\x00\x00WAVE" + b"\x00" * 256)
    audio_path = str(f)

    monkeypatch.setenv("ADLIB_VAD_RETRANSCRIBE_ENABLED", "0")
    mega = _make_seg("uh uh uh uh uh uh uh uh uh uh", 69.0, 101.0)
    out = wx._vad_split_adlib_segments([mega], audio_path)
    assert out == [mega]


def test_vad_split_skips_segment_with_words(monkeypatch, tmp_path):
    """VAD runs on adlib segments even when word stamps are present.

    whisperX force-alignment always populates word stamps — including on
    hallucinated adlib text — so `not words` was a broken guard (PR #710
    removed it). VAD still runs; when retranscription fails the original
    segment is kept as fallback.
    """
    f = tmp_path / "vocals.wav"
    f.write_bytes(b"RIFF\x00\x00\x00\x00WAVE" + b"\x00" * 256)
    audio_path = str(f)

    vad_called = {"n": 0}

    def _counting_vad(*a, **k):
        vad_called["n"] += 1
        return [(73.0, 84.0), (84.5, 92.5)]

    monkeypatch.setattr(wx, "_detect_adlib_voice_regions", _counting_vad)
    monkeypatch.setattr(wx, "_retranscribe_slice", lambda *a, **k: None)  # simulate failure
    monkeypatch.setenv("ADLIB_VAD_RETRANSCRIBE_ENABLED", "1")

    words = [{"word": "uh", "start": 69.0 + i * 2, "end": 70.0 + i * 2} for i in range(8)]
    seg_with_words = _make_seg("uh uh uh uh uh uh uh uh", 69.0, 101.0, words)
    out = wx._vad_split_adlib_segments([seg_with_words], audio_path)
    assert out == [seg_with_words]   # fallback: original kept when retranscription fails
    assert vad_called["n"] == 1, "VAD must run on adlib segments even with word stamps"


# ── _correct_post_adlib_timing ───────────────────────────────────────────────

def test_correct_post_adlib_timing_shifts_tail(monkeypatch, tmp_path):
    """No Hay Santos regression: 'Tomas del miedo' 7s too early → shifted to true onset.

    The adlib block ends at 95.2s. whisperX places 'Tomas del miedo' at 95.2s
    (0s gap), but uh sounds continue until ~102s. VAD finds two intervals:
    [95.2-101.0] (uh sounds) and [102.0-106.0] (actual lyric). Largest gap
    (1s) precedes the 102s interval → 'Tomas del miedo' shifts to 102.0s.
    """
    import numpy as np

    f = tmp_path / "vocals.wav"
    f.write_bytes(b"RIFF\x00\x00\x00\x00WAVE" + b"\x00" * 256)
    audio_path = str(f)

    # VAD returns two intervals (in sample space for a 16000 sr signal):
    # [95.2+0, 95.2+5.8s] = uh sounds; [95.2+6.8s, 95.2+10.8s] = lyric
    def _fake_vad(path, offset, duration):
        sr = 16000
        return np.array([
            [0,              int(5.8 * sr)],   # uh cluster
            [int(6.8 * sr),  int(10.8 * sr)],  # lyric onset
        ])

    import librosa as _lib_real
    monkeypatch.setattr(_lib_real.effects, "split", lambda y, top_db: _fake_vad(None, None, None))
    monkeypatch.setattr(_lib_real, "load", lambda *a, **k: (None, 16000))

    adlib = _make_seg("uh uh uh uh uh uh", 69.4, 95.2)
    lyric = _make_seg("Tomas del miedo tu don", 95.2, 100.0)
    out = wx._correct_post_adlib_timing([adlib, lyric], audio_path)

    assert out[0] == adlib
    corrected = out[1]
    assert abs(corrected["start"] - (95.2 + 6.8)) < 0.1, (
        f"Expected start ~102s, got {corrected['start']}"
    )
    assert corrected["text"] == "Tomas del miedo tu don"


def test_correct_post_adlib_timing_noop_when_no_clear_gap(monkeypatch, tmp_path):
    """No shift when all gaps are < 0.4s (lyric really does start right after adlib)."""
    import numpy as np

    f = tmp_path / "vocals.wav"
    f.write_bytes(b"RIFF\x00\x00\x00\x00WAVE" + b"\x00" * 256)
    audio_path = str(f)

    # Single interval: no gap found → returns None → no shift
    def _fake_vad(y, top_db):
        sr = 16000
        return np.array([[0, int(4.0 * sr)]])

    import librosa as _lib_real
    monkeypatch.setattr(_lib_real.effects, "split", lambda y, top_db: _fake_vad(y, top_db))
    monkeypatch.setattr(_lib_real, "load", lambda *a, **k: (None, 16000))

    adlib = _make_seg("uh uh uh uh", 69.4, 95.2)
    lyric = _make_seg("Tomas del miedo tu don", 95.2, 99.0)
    out = wx._correct_post_adlib_timing([adlib, lyric], audio_path)

    assert out[1]["start"] == 95.2  # unchanged


def test_correct_post_adlib_timing_skips_large_gap_between_segs(monkeypatch, tmp_path):
    """No shift when next segment starts >2s after adlib block (already correct gap)."""
    f = tmp_path / "vocals.wav"
    f.write_bytes(b"RIFF\x00\x00\x00\x00WAVE" + b"\x00" * 256)
    audio_path = str(f)

    called = {"n": 0}

    def _counting_load(*a, **k):
        called["n"] += 1
        return (None, 16000)

    import librosa as _lib_real
    monkeypatch.setattr(_lib_real, "load", _counting_load)

    adlib = _make_seg("uh uh uh uh", 69.4, 95.2)
    lyric = _make_seg("Tomas del miedo tu don", 98.0, 103.0)  # 2.8s gap → skip
    out = wx._correct_post_adlib_timing([adlib, lyric], audio_path)

    assert called["n"] == 0, "Should not call librosa when gap > 2s"
    assert out[1]["start"] == 98.0


def test_correct_post_adlib_timing_never_changes_text(monkeypatch, tmp_path):
    """Text is never modified by timing correction."""
    import numpy as np

    f = tmp_path / "vocals.wav"
    f.write_bytes(b"RIFF\x00\x00\x00\x00WAVE" + b"\x00" * 256)
    audio_path = str(f)

    def _fake_vad(y, top_db):
        sr = 16000
        return np.array([[0, int(5.0 * sr)], [int(7.0 * sr), int(11.0 * sr)]])

    import librosa as _lib_real
    monkeypatch.setattr(_lib_real.effects, "split", lambda y, top_db: _fake_vad(y, top_db))
    monkeypatch.setattr(_lib_real, "load", lambda *a, **k: (None, 16000))

    adlib = _make_seg("uh uh uh uh", 69.4, 95.2)
    lyric = _make_seg("Frágil espejo de voz", 95.2, 99.0)
    out = wx._correct_post_adlib_timing([adlib, lyric], audio_path)

    assert out[1]["text"] == "Frágil espejo de voz"
