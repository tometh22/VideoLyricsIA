"""Tests for pipeline._llm_segment_words (LLM line-segmentation grounded in
whisperX timing). The Gemini call is mocked — we exercise the parse, the gates,
the timing mapping and the self-declining behaviour without hitting Vertex."""
import inspect
from unittest.mock import MagicMock

import pytest

import pipeline
import transcription_quality
from recognition_provenance import begin_collection, end_collection


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


def test_prompt_preserves_resolved_language_without_translation(
        tiny_audio, monkeypatch):
    monkeypatch.setenv("LLM_SEGMENT_ENABLED", "1")
    fake = MagicMock()
    fake.models.generate_content.return_value = _FakeResponse(
        "[0-3] Tengo una mala noticia\n[4-7] No fue de casualidad"
    )
    monkeypatch.setattr(pipeline, "_get_genai_client", lambda: fake)

    pipeline._llm_segment_words(
        SEGS, audio_path=tiny_audio, language="es",
    )

    config = fake.models.generate_content.call_args.kwargs["config"]
    assert "IDIOMA PRINCIPAL DE CONTEXTO: español (es)" in config.system_instruction
    assert "idioma ORIGINAL de CADA frase" in config.system_instruction
    assert "puede alternar idiomas" in config.system_instruction
    assert "no traduzcas" in config.system_instruction.lower()


def test_self_declines_on_unparseable_output(tiny_audio, monkeypatch):
    monkeypatch.setenv("LLM_SEGMENT_ENABLED", "1")
    _mock_gemini(monkeypatch, "Lo siento, no puedo hacer eso.")  # no [i-j] lines
    out = pipeline._llm_segment_words(SEGS, audio_path=tiny_audio)
    assert out is SEGS


def test_unparseable_output_is_frozen_before_llm_segment_filters(
        tiny_audio, monkeypatch):
    monkeypatch.setenv("LLM_SEGMENT_ENABLED", "1")
    raw = "Lo siento, no puedo hacer eso."
    _mock_gemini(monkeypatch, raw)
    collector, token = begin_collection()
    try:
        out = pipeline._llm_segment_words(SEGS, audio_path=tiny_audio)
        snapshot = collector.snapshot()
    finally:
        end_collection(token)

    assert out is SEGS
    assert snapshot["completed_attempt_count"] == 1
    assert snapshot["hypotheses"][0]["events"] == [{"text": raw}]
    source = inspect.getsource(pipeline._llm_segment_words)
    assert source.index("_record_gemini_audio_completion(") < source.index(
        "for ln in out.splitlines()"
    )


def test_self_declines_on_hallucination(tiny_audio, monkeypatch):
    monkeypatch.setenv("LLM_SEGMENT_ENABLED", "1")
    # Parseable ranges but the text shares no words with whisperX → overlap gate.
    _mock_gemini(monkeypatch,
                 "[0-3] aaaa bbbb cccc dddd\n[4-7] eeee ffff gggg hhhh")
    out = pipeline._llm_segment_words(SEGS, audio_path=tiny_audio)
    assert out is SEGS


def test_self_declines_when_one_code_switched_line_is_translated(
        tiny_audio, monkeypatch):
    monkeypatch.setenv("LLM_SEGMENT_ENABLED", "1")
    words = [
        {"word": "Todo", "start": 1.0, "end": 1.2},
        {"word": "el", "start": 1.2, "end": 1.3},
        {"word": "mundo", "start": 1.3, "end": 1.6},
        {"word": "baila", "start": 1.6, "end": 2.0},
        {"word": "Are", "start": 2.5, "end": 2.7},
        {"word": "you", "start": 2.7, "end": 2.8},
        {"word": "ready", "start": 2.8, "end": 3.1},
        {"word": "now", "start": 3.1, "end": 3.4},
    ]
    mixed = [{
        "start": 1.0,
        "end": 3.4,
        "text": "Todo el mundo baila Are you ready now",
        "words": words,
    }]
    _mock_gemini(
        monkeypatch,
        "[0-3] Todo el mundo baila\n[4-7] Están todos listos ahora",
    )

    assert pipeline._llm_segment_words(
        mixed, audio_path=tiny_audio, language="es",
    ) is mixed


@pytest.mark.parametrize("ranges", [
    "[4-7] No fue de casualidad\n[0-3] Tengo una mala noticia",
    "[0-4] Tengo una mala noticia No\n[4-7] No fue de casualidad",
    "[0-2] Tengo una mala\n[4-7] No fue de casualidad",
])
def test_self_declines_on_reordered_overlapping_or_gapped_ranges(
        tiny_audio, monkeypatch, ranges):
    monkeypatch.setenv("LLM_SEGMENT_ENABLED", "1")
    _mock_gemini(monkeypatch, ranges)
    out = pipeline._llm_segment_words(SEGS, audio_path=tiny_audio)
    assert out is SEGS


def test_self_declines_when_mapped_line_times_are_collapsed(
        tiny_audio, monkeypatch):
    monkeypatch.setenv("LLM_SEGMENT_ENABLED", "1")
    collapsed_words = [dict(w) for w in WORDS]
    # The semantic word order is intact, but a provider collapsed the second
    # phrase back onto the first phrase's timestamp window.
    for index, word in enumerate(collapsed_words[4:]):
        word["start"] = 1.0 + index * 0.1
        word["end"] = word["start"] + 0.1
    collapsed = [{
        "start": 1.0, "end": 2.0,
        "text": "Tengo una mala noticia No fue de casualidad",
        "words": collapsed_words,
    }]
    _mock_gemini(
        monkeypatch,
        "[0-3] Tengo una mala noticia\n[4-7] No fue de casualidad",
    )

    assert pipeline._llm_segment_words(
        collapsed, audio_path=tiny_audio,
    ) is collapsed


def test_collapses_compressed_repeated_singletons_but_keeps_other_lines(
        tiny_audio, monkeypatch):
    monkeypatch.setenv("LLM_SEGMENT_ENABLED", "1")
    words = [
        {"word": "Y", "start": 55.0, "end": 55.4},
        {"word": "te", "start": 55.4, "end": 55.8},
        {"word": "alejaste", "start": 55.8, "end": 56.8},
        {"word": "de-mi", "start": 56.8, "end": 58.0},
        {"word": "Real", "start": 60.0, "end": 60.8},
        {"word": "Real", "start": 61.1, "end": 61.8},
        {"word": "Real", "start": 62.2, "end": 62.9},
        {"word": "Real", "start": 63.3, "end": 64.0},
    ]
    segs = [{
        "start": 55.0, "end": 64.0,
        "text": "Y te alejaste de mí Real Real Real Real", "words": words,
    }]
    _mock_gemini(
        monkeypatch,
        "[0-3] Y te alejaste de mí\n[4-4] Real\n[5-5] Real\n"
        "[6-6] Real\n[7-7] Real",
    )

    out = pipeline._llm_segment_words(segs, audio_path=tiny_audio)

    assert [line["text"] for line in out] == ["Y te alejaste de mí", "Real"]
    assert out[1]["start"] == 60.0
    assert out[1]["end"] == 64.0
    assert len(out[1]["words"]) == 4
    assert out[1]["collapsed_repetition"] == 4
    assert out[1]["review"] is True
    windows = transcription_quality.build_unsafe_windows(out, [])
    repetition_window = next(
        window for window in windows
        if "provider_timing_collapsed" in window["reasons"]
    )
    assert repetition_window["start"] <= 60.0
    assert repetition_window["end"] >= 83.27


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
