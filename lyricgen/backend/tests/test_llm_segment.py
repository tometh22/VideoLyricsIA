"""Tests for pipeline._llm_segment_words (LLM line-segmentation grounded in
whisperX timing). The Gemini call is mocked — we exercise the parse, the gates,
the timing mapping and the self-declining behaviour without hitting Vertex."""
from unittest.mock import MagicMock

import pytest

import pipeline


WORDS = [
    {"word": "Tengo", "start": 1.0, "end": 1.2},
    {"word": "una", "start": 1.2, "end": 1.4},
    {"word": "mala", "start": 1.4, "end": 1.6},
    {"word": "noticia", "start": 1.6, "end": 2.0},
    {"word": "No", "start": 2.5, "end": 2.7},
    {"word": "fue", "start": 2.7, "end": 2.9},
    {"word": "de", "start": 2.9, "end": 3.0},
    {"word": "casualidad", "start": 3.0, "end": 3.5},
]
SEGS = [{"start": 1.0, "end": 3.5,
         "text": "Tengo una mala noticia No fue de casualidad",
         "words": WORDS}]


@pytest.fixture
def tiny_audio(tmp_path):
    p = tmp_path / "test.wav"
    p.write_bytes(b"RIFF\x00\x00\x00\x00WAVE" + b"\x00" * 256)
    return str(p)


class _FakeResponse:
    def __init__(self, text):
        self.text = text


def _mock_gemini(monkeypatch, text):
    fake = MagicMock()
    fake.models.generate_content.return_value = _FakeResponse(text)
    monkeypatch.setattr(pipeline, "_get_genai_client", lambda: fake)


def test_returns_input_unchanged_when_flag_off(tiny_audio, monkeypatch):
    monkeypatch.delenv("LLM_SEGMENT_ENABLED", raising=False)
    out = pipeline._llm_segment_words(SEGS, audio_path=tiny_audio)
    assert out is SEGS  # exact same object — no work done


def test_self_declines_when_no_word_timing(tiny_audio, monkeypatch):
    monkeypatch.setenv("LLM_SEGMENT_ENABLED", "1")
    segs = [{"start": 1.0, "end": 3.5, "text": "sin words"}]  # no `words`
    out = pipeline._llm_segment_words(segs, audio_path=tiny_audio)
    assert out is segs


def test_resegments_with_clean_text_and_whisperx_timing(tiny_audio, monkeypatch):
    monkeypatch.setenv("LLM_SEGMENT_ENABLED", "1")
    _mock_gemini(monkeypatch,
                 "[0-3] Tengo una mala noticia\n[4-7] No fue de casualidad")
    out = pipeline._llm_segment_words(SEGS, audio_path=tiny_audio)

    assert len(out) == 2
    # clean text from the LLM:
    assert out[0]["text"] == "Tengo una mala noticia"
    assert out[1]["text"] == "No fue de casualidad"
    # timing comes from the whisperX words (exact), NOT re-invented:
    assert out[0]["start"] == 1.0 and out[0]["end"] == 2.0
    assert out[1]["start"] == 2.5 and out[1]["end"] == 3.5
    # words are sliced between the lines (not duplicated):
    assert [w["word"] for w in out[0]["words"]] == ["Tengo", "una", "mala", "noticia"]
    assert [w["word"] for w in out[1]["words"]] == ["No", "fue", "de", "casualidad"]
    # every output word keeps its exact (word,start,end):
    flat_out = [(w["word"], w["start"], w["end"]) for s in out for w in s["words"]]
    flat_in = [(w["word"], w["start"], w["end"]) for w in WORDS]
    assert flat_out == flat_in


def test_self_declines_on_unparseable_output(tiny_audio, monkeypatch):
    monkeypatch.setenv("LLM_SEGMENT_ENABLED", "1")
    _mock_gemini(monkeypatch, "Lo siento, no puedo hacer eso.")  # no [i-j] lines
    out = pipeline._llm_segment_words(SEGS, audio_path=tiny_audio)
    assert out is SEGS


def test_self_declines_on_hallucination(tiny_audio, monkeypatch):
    monkeypatch.setenv("LLM_SEGMENT_ENABLED", "1")
    # Parseable ranges but the text shares no words with whisperX → overlap gate.
    _mock_gemini(monkeypatch,
                 "[0-3] aaaa bbbb cccc dddd\n[4-7] eeee ffff gggg hhhh")
    out = pipeline._llm_segment_words(SEGS, audio_path=tiny_audio)
    assert out is SEGS


# ─────────────────────────────────────────────────────────────────────────
# _recording_diverges — the gate that decides whether LLM line-segmentation
# may PREEMPT the canonical-recovery cascade (forced_align) after reconcile
# aborts. Reconcile aborts on BOTH true lives AND plain whisperX mishears;
# only the former should let LLM-segment win (the latter needs FA to recover
# the canonical text). See pipeline._recording_diverges.
# ─────────────────────────────────────────────────────────────────────────

def _seg(text):
    return {"start": 0.0, "end": 1.0, "text": text, "words": []}


def test_diverges_true_for_extended_live():
    # "Nada Fue Un Error En Vivo": the live sings the studio lyric plus repeated
    # verses, ad-libs and a long outro → far more words than the studio lyric.
    canonical = "Tengo una mala noticia\nNo fue de casualidad"
    live = [
        _seg("Tengo una mala noticia"),
        _seg("No fue de casualidad"),
        _seg("Y te digo los dolores nos dirigen"),
        _seg("Para bien o para mal no pase cuando viniste"),
        _seg("Nada fue un error nada de esto fue"),
    ]
    assert pipeline._recording_diverges(live, canonical) is True


def test_diverges_false_for_misheard_studio_song():
    # Incident "Viejas Locas — 638": reconcile aborted because whisperX MISHEARD
    # a few words ("638" → "780465"), NOT because the recording diverges. Word
    # count ~matches the canonical → must NOT preempt FA (which recovers 19/19).
    canonical = "Marca el seis tres ocho\nY empece a llamarte"
    misheard = [
        _seg("Marca el siete ocho cero"),   # same length, wrong digits
        _seg("Y empece y empece a llamarte"),  # one duplicated word
    ]
    assert pipeline._recording_diverges(misheard, canonical) is False


def test_diverges_false_when_no_canonical():
    # No studio lyric to recover → divergence is undefined; never preempt.
    assert pipeline._recording_diverges([_seg("anything at all here")], "") is False
    assert pipeline._recording_diverges([_seg("x y z")], None) is False


def test_diverges_ratio_is_tunable():
    canonical = "uno dos tres cuatro"          # 4 words
    segs = [_seg("uno dos tres cuatro cinco")]  # 5 words → 1.25×
    assert pipeline._recording_diverges(segs, canonical, ratio=1.25) is True
    assert pipeline._recording_diverges(segs, canonical, ratio=1.5) is False
