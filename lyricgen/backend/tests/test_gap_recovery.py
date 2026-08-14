"""Tests for pipeline._recover_gap_lyrics (recover lyrics whisperX dropped in
large gaps via a SHORT bounded re-transcription). librosa.load is mocked with a
synthetic vocal stem (silence + a voiced run inside the gap); the Gemini call is
mocked. We exercise gap detection, the voiced-run scan, the loop + containment
gates, timing distribution, and the self-declining behaviour — no real audio,
no Vertex."""
from unittest.mock import MagicMock

import numpy as np
import pytest

import pipeline


SR = 22050

# 8 chorus words packed into 1.0–1.9 s, then a long hole to end-of-audio.
WORDS = [
    {"word": "Nada", "start": 1.00, "end": 1.10},
    {"word": "fue", "start": 1.12, "end": 1.22},
    {"word": "un", "start": 1.24, "end": 1.34},
    {"word": "error", "start": 1.36, "end": 1.50},
    {"word": "nada", "start": 1.52, "end": 1.62},
    {"word": "de", "start": 1.64, "end": 1.72},
    {"word": "esto", "start": 1.74, "end": 1.84},
    {"word": "fue", "start": 1.86, "end": 1.90},
]
SEGS = [{"start": 1.0, "end": 1.9,
         "text": "Nada fue un error nada de esto fue", "words": list(WORDS)}]
CANON = "Nada fue un error, nada de esto fue un error"


def _synthetic_stem(duration=20.0):
    """Near-silent stem with two loud regions: 1.0–1.9 s (the existing words)
    and 5.0–8.0 s (a voiced run inside the trailing gap to be recovered)."""
    y = np.full(int(duration * SR), 1e-5, dtype=np.float32)
    for t0, t1 in ((1.0, 1.9), (5.0, 8.0)):
        y[int(t0 * SR):int(t1 * SR)] = 0.3
    return y


@pytest.fixture
def stem(tmp_path):
    p = tmp_path / "vocals.wav"
    p.write_bytes(b"RIFF\x00\x00\x00\x00WAVE" + b"\x00" * 256)  # path must exist
    return str(p)


def _mock(monkeypatch, gemini_text):
    fake = MagicMock()
    fake.models.generate_content.return_value = MagicMock(text=gemini_text)
    monkeypatch.setattr(pipeline, "_get_genai_client", lambda: fake)
    monkeypatch.setattr(pipeline.librosa, "load",
                        lambda *a, **k: (_synthetic_stem(), SR))


def test_flag_off_is_noop(stem, monkeypatch):
    monkeypatch.delenv("GAP_RECOVERY_ENABLED", raising=False)
    out = pipeline._recover_gap_lyrics(SEGS, audio_path=stem, canonical=CANON)
    assert out is SEGS  # exact same object, no audio loaded


def test_recovers_line_in_gap(stem, monkeypatch):
    monkeypatch.setenv("GAP_RECOVERY_ENABLED", "1")
    _mock(monkeypatch, "Nada de esto fue")
    out = pipeline._recover_gap_lyrics(SEGS, audio_path=stem, canonical=CANON)

    rec = [s for s in out if s.get("provenance") == "gap-recovery"]
    assert len(rec) == 1
    assert rec[0]["text"] == "Nada de esto fue"
    # timing lands inside the detected voiced run (~5–8 s), never re-times words
    assert 4.9 <= rec[0]["start"] <= 8.5 and 4.9 <= rec[0]["end"] <= 8.5
    assert rec[0]["end"] > rec[0]["start"]
    # the original whisperX words are byte-identical and still present
    kept = [w for s in out for w in s.get("words", [])
            if w.get("provenance") != "gap-recovery"]
    assert [(w["word"], w["start"], w["end"]) for w in kept] == \
           [(w["word"], w["start"], w["end"]) for w in WORDS]
    # recovered words carry provenance + low score (approx timing marker)
    assert all(w["provenance"] == "gap-recovery" and w["score"] == 0.3
               for w in rec[0]["words"])


def test_rejects_loop_hallucination(stem, monkeypatch):
    monkeypatch.setenv("GAP_RECOVERY_ENABLED", "1")
    # the long-clip failure mode: the same phrase repeated many times
    _mock(monkeypatch, "\n".join(["Nada fue un error"] * 5))
    out = pipeline._recover_gap_lyrics(SEGS, audio_path=stem, canonical=CANON)
    assert all(s.get("provenance") != "gap-recovery" for s in out)


def test_rejects_low_containment(stem, monkeypatch):
    monkeypatch.setenv("GAP_RECOVERY_ENABLED", "1")
    # words that do not appear in the song → hallucination guard drops them
    _mock(monkeypatch, "Wikipedia teléfono automóvil")
    out = pipeline._recover_gap_lyrics(SEGS, audio_path=stem, canonical=CANON)
    assert all(s.get("provenance") != "gap-recovery" for s in out)


def test_drops_pure_label_lines(stem, monkeypatch):
    monkeypatch.setenv("GAP_RECOVERY_ENABLED", "1")
    # v1 keeps only lyric lines; pure (label)/¡shout! lines are dropped
    _mock(monkeypatch, "(grito)")
    out = pipeline._recover_gap_lyrics(SEGS, audio_path=stem, canonical=CANON)
    assert all(s.get("provenance") != "gap-recovery" for s in out)


def test_no_gap_is_noop(stem, monkeypatch):
    monkeypatch.setenv("GAP_RECOVERY_ENABLED", "1")
    _mock(monkeypatch, "Nada de esto fue")
    # audio only ~2 s long → no trailing gap >= 8 s, no between-word gap
    monkeypatch.setattr(pipeline.librosa, "load",
                        lambda *a, **k: (_synthetic_stem(2.0), SR))
    out = pipeline._recover_gap_lyrics(SEGS, audio_path=stem, canonical=CANON)
    assert out is SEGS


# ───────────────── Path 1: forced-align timing for recovered text ─────────

def test_gap_words_to_lines_maps_real_timing():
    # one stamp per token, in order; line start/end come from the stamps
    stamps = [{"word": "Nada", "start": 5.20, "end": 5.40, "score": 0.9},
              {"word": "de", "start": 5.45, "end": 5.60, "score": 0.8},
              {"word": "esto", "start": 5.65, "end": 5.90, "score": 0.9},
              {"word": "fue", "start": 5.95, "end": 6.30, "score": 0.7}]
    out = pipeline._gap_words_to_lines(stamps, ["Nada de esto fue"])
    assert len(out) == 1
    assert out[0]["start"] == 5.20 and out[0]["end"] == 6.30
    assert [w["start"] for w in out[0]["words"]] == [5.20, 5.45, 5.65, 5.95]
    # real score (not the 0.3 uniform marker) → beat_snap leaves it alone
    assert all(w["score"] >= 0.7 for w in out[0]["words"])
    assert all(w["provenance"] == "gap-recovery" for w in out[0]["words"])


def test_gap_words_to_lines_returns_none_on_mismatch():
    # far fewer stamps than tokens → can't trust the mapping → None (→ uniform)
    stamps = [{"word": "Nada", "start": 5.2, "end": 5.4}]
    assert pipeline._gap_words_to_lines(stamps, ["Nada de esto fue un error"]) is None
    assert pipeline._gap_words_to_lines([], ["x y"]) is None


def test_recovers_with_forced_align_timing(stem, monkeypatch):
    monkeypatch.setenv("GAP_RECOVERY_ENABLED", "1")
    monkeypatch.setenv("GAP_RECOVERY_ALIGN_ENABLED", "1")
    _mock(monkeypatch, "Nada de esto fue")
    # inject a forced-aligner that returns REAL absolute stamps for the clip
    stamps = [{"word": "Nada", "start": 5.30, "end": 5.50, "score": 0.95},
              {"word": "de", "start": 5.55, "end": 5.70, "score": 0.90},
              {"word": "esto", "start": 5.75, "end": 6.00, "score": 0.92},
              {"word": "fue", "start": 6.05, "end": 6.40, "score": 0.88}]
    monkeypatch.setattr(pipeline, "_GAP_CLIP_ALIGNER",
                        lambda clip, sr, c0, lines: stamps)
    out = pipeline._recover_gap_lyrics(SEGS, audio_path=stem, canonical=CANON)

    rec = [s for s in out if s.get("provenance") == "gap-recovery"]
    assert len(rec) == 1
    # timing is the REAL aligned timing, NOT the uniform voiced-run split
    assert rec[0]["start"] == 5.30 and rec[0]["end"] == 6.40
    assert [w["start"] for w in rec[0]["words"]] == [5.30, 5.55, 5.75, 6.05]
    assert all(w["score"] >= 0.85 for w in rec[0]["words"])  # not 0.3


def test_align_disabled_keeps_uniform_timing(stem, monkeypatch):
    monkeypatch.setenv("GAP_RECOVERY_ENABLED", "1")
    monkeypatch.delenv("GAP_RECOVERY_ALIGN_ENABLED", raising=False)
    _mock(monkeypatch, "Nada de esto fue")
    # even if a hook is present, the flag gates it off → uniform (score 0.3)
    monkeypatch.setattr(pipeline, "_GAP_CLIP_ALIGNER",
                        lambda *a, **k: [{"word": "x", "start": 5.0, "end": 5.1}])
    out = pipeline._recover_gap_lyrics(SEGS, audio_path=stem, canonical=CANON)
    rec = [s for s in out if s.get("provenance") == "gap-recovery"]
    assert len(rec) == 1 and all(w["score"] == 0.3 for w in rec[0]["words"])


# ───────── Combined pipeline / Stage 3: acoustic-twin transplant ─────────

SRC_CHORUS = [{"start": 10.0, "end": 13.0, "text": "corazon noche amor",
               "words": [{"word": "corazon", "start": 10.0, "end": 11.0},
                         {"word": "noche", "start": 11.0, "end": 12.0},
                         {"word": "amor", "start": 12.0, "end": 13.0}]}]


def test_gap_warp_segments_linear_retime():
    # 3s source warped into a 6s gap → everything scaled ×2, text preserved
    out = pipeline._gap_warp_segments(SRC_CHORUS, 50.0, 56.0)
    assert len(out) == 1 and out[0]["text"] == "corazon noche amor"
    assert out[0]["start"] == 50.0 and out[0]["end"] == 56.0
    assert [w["start"] for w in out[0]["words"]] == [50.0, 52.0, 54.0]
    assert [w["end"] for w in out[0]["words"]] == [52.0, 54.0, 56.0]
    assert all(w["provenance"] == "gap-transplant" for w in out[0]["words"])


def test_transplant_gap_uses_twin(monkeypatch):
    monkeypatch.setattr(pipeline, "_GAP_TWIN_FINDER",
                        lambda *a: (SRC_CHORUS, 0.05))
    out = pipeline._transplant_gap([], 50.0, 53.0, None, None,
                                   canonical="corazon noche amor")
    assert out and out[0]["provenance"] == "gap-transplant"
    assert out[0]["start"] == 50.0 and out[0]["text"] == "corazon noche amor"


def test_transplant_gap_rejects_foreign_vocab(monkeypatch):
    foreign = [{"start": 10.0, "end": 13.0, "text": "wikipedia telefono automovil",
                "words": [{"word": "wikipedia", "start": 10.0, "end": 11.0},
                          {"word": "telefono", "start": 11.0, "end": 12.0},
                          {"word": "automovil", "start": 12.0, "end": 13.0}]}]
    monkeypatch.setattr(pipeline, "_GAP_TWIN_FINDER", lambda *a: (foreign, 0.05))
    out = pipeline._transplant_gap([], 50.0, 54.0, None, None,
                                   canonical="corazon noche amor")
    assert out is None   # vocabulary not in the song → rejected (anti-garbage)


def test_recover_uses_transplant_when_enabled(stem, monkeypatch):
    monkeypatch.setenv("GAP_RECOVERY_ENABLED", "1")
    monkeypatch.setenv("GAP_RECOVERY_TRANSPLANT_ENABLED", "1")
    # voiced across the whole gap so the vocal-presence gate passes
    voiced = np.full(int(20 * SR), 0.3, dtype=np.float32)
    monkeypatch.setattr(pipeline.librosa, "load", lambda *a, **k: (voiced, SR))
    # stub the (heavy) chroma/beat precompute deterministically
    monkeypatch.setattr(pipeline.librosa.feature, "chroma_cqt",
                        lambda **k: np.zeros((12, 40)))
    monkeypatch.setattr(pipeline.librosa.beat, "beat_track",
                        lambda **k: (120.0, np.arange(40)))
    monkeypatch.setattr(pipeline.librosa, "frames_to_time",
                        lambda f, **k: np.asarray(f, dtype=float) * 0.5)
    monkeypatch.setattr(pipeline.librosa.util, "sync",
                        lambda c, b, **k: np.zeros((12, len(b))))
    # enough words for the big trailing gap (~18s) to clear the smear guard
    w16 = ["Nada", "fue", "un", "error"] * 4
    src = [{"start": 1.0, "end": 1.9, "text": " ".join(w16),
            "words": [{"word": w, "start": 1.0 + i * 0.05, "end": 1.0 + i * 0.05 + 0.04}
                      for i, w in enumerate(w16)]}]
    monkeypatch.setattr(pipeline, "_GAP_TWIN_FINDER", lambda *a: (src, 0.04))
    out = pipeline._recover_gap_lyrics(SEGS, audio_path=stem, canonical=CANON)
    tx = [s for s in out if s.get("provenance") == "gap-transplant"]
    assert len(tx) >= 1 and "Nada fue un error" in tx[0]["text"]


def test_find_gap_twin_real_dtw():
    # PROD path (no hook): a synthetic song where beats 5-9 repeat at 25-29.
    C = np.zeros((12, 40), dtype=float)
    for j in range(40):
        C[j % 12, j] = 1.0
    C[:, 25:30] = C[:, 5:10]            # make the gap region a repeat of 5-9
    beat_t = np.arange(40) * 0.5         # 0.5s per beat
    src_seg = {"start": 2.5, "end": 4.5, "text": "corazon noche amor",
               "words": [{"word": "corazon", "start": 2.5, "end": 3.0},
                         {"word": "noche", "start": 3.0, "end": 4.0},
                         {"word": "amor", "start": 4.0, "end": 4.5}]}
    # gap at beats 25-29 → times 12.5-14.5
    out = pipeline._find_gap_twin([src_seg], 12.5, 14.5, C, beat_t, 0.5)
    assert out is not None
    src, cost = out
    assert src and src[0]["text"] == "corazon noche amor" and cost < 0.2


def test_find_gap_twin_declines_when_no_match():
    # two-hot columns with a per-12-block second pitch → no global repeat of the
    # gap's pattern (chroma is inherently period-12, so vary by block)
    C = np.zeros((12, 40), dtype=float)
    for j in range(40):
        C[j % 12, j] = 1.0
        C[(j // 12 + 6) % 12, j] = 1.0
    beat_t = np.arange(40) * 0.5
    seg = {"start": 2.5, "end": 4.5, "text": "x",
           "words": [{"word": "x", "start": 2.5, "end": 4.5}]}
    out = pipeline._find_gap_twin([seg], 12.5, 14.5, C, beat_t, 0.01)
    assert out is None                   # nothing below max_cost → decline


def test_warp_segments_declines_on_degenerate_input():
    s = [{"start": 10.0, "end": 13.0, "text": "a",
          "words": [{"word": "a", "start": 10.0, "end": 13.0}]}]
    assert pipeline._gap_warp_segments([], 50.0, 56.0) is None
    assert pipeline._gap_warp_segments(s, 56.0, 50.0) is None    # inverted gap
    zero = [{"start": 10.0, "end": 10.0, "text": "a",
             "words": [{"word": "a", "start": 10.0, "end": 10.0}]}]
    assert pipeline._gap_warp_segments(zero, 50.0, 56.0) is None  # zero span


def test_transplant_smear_guard_rejects_sparse_gap(monkeypatch):
    # 3 words warped over an 84s gap → ~28s/word → smear → decline
    monkeypatch.setattr(pipeline, "_GAP_TWIN_FINDER", lambda *a: (SRC_CHORUS, 0.02))
    out = pipeline._transplant_gap([], 200.0, 284.0, None, None,
                                   canonical="corazon noche amor")
    assert out is None


def test_transplant_vocal_presence_gate_rejects_instrumental(monkeypatch):
    # even with a perfect twin, a silent/instrumental gap (no voice) is declined
    monkeypatch.setattr(pipeline, "_GAP_TWIN_FINDER", lambda *a: (SRC_CHORUS, 0.02))
    silent = np.full(int(60 * SR), 1e-5, dtype=np.float32)
    out = pipeline._transplant_gap([], 50.0, 53.0, None, None, silent, SR, 0.01,
                                   canonical="corazon noche amor")
    assert out is None


def test_recording_diverges_counts_words_without_text():
    # segs carry word stream but empty text → must still count (real whisperX case)
    segs = [{"text": "", "words": [{"word": "w"}] * 400}]
    assert pipeline._recording_diverges(segs, " ".join(["x"] * 250)) is True


def test_transplant_flag_off_keeps_gemini_path(stem, monkeypatch):
    monkeypatch.setenv("GAP_RECOVERY_ENABLED", "1")
    monkeypatch.delenv("GAP_RECOVERY_TRANSPLANT_ENABLED", raising=False)
    _mock(monkeypatch, "Nada de esto fue")
    # hook present but flag off → transplant never runs, Gemini path used
    monkeypatch.setattr(pipeline, "_GAP_TWIN_FINDER",
                        lambda *a: (SRC_CHORUS, 0.01))
    out = pipeline._recover_gap_lyrics(SEGS, audio_path=stem, canonical=CANON)
    assert all(s.get("provenance") != "gap-transplant" for s in out)
    assert any(s.get("provenance") == "gap-recovery" for s in out)
