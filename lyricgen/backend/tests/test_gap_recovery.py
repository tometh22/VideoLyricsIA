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
