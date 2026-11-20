"""Independent sung-language probe + line-level language discrepancy.

Incident 2026-11-18 (campaign 6637d82e93dd): WhisperX auto-LID picked English
from the intro and TRANSLATED Spanish verses ("Gran Ídolo De Asia", Gilda
"Rompo Las Cadenas", ...). The output looked bilingual, so the contract
exempted it, and without a reference nothing fired. Lyrics below are invented.
"""
import json
import types

import pytest

import language_probe
from language_review import review_payload
from transcription_language import (
    build_language_contract,
    line_language,
    probe_conflict_indices,
)
from transcription_quality import supersede_pending_analysis


SPANISH = [
    "Te encuentro en la noche cuando nadie me ve",
    "Yo tengo tu nombre guardado en el corazón",
    "Nunca supe decirte que eras mi vida",
    "Hoy vuelvo temprano y el tiempo no está",
]
# What WhisperX emits when it decodes the same verses with language=en.
TRANSLATED = [
    "I find you in the night when nobody sees me",
    "I have your name kept in my heart",
    "I never knew how to tell you that you were my life",
    "Today I come back early and the time is not here",
]
PROBE_ES = {"schema": "sung-language-probe-v1", "source": "gemini", "status": "ok",
            "main_language": "es", "languages": ["es"], "sections": []}


def _segs(lines):
    return [{"start": float(i * 4), "end": float(i * 4 + 3), "text": t}
            for i, t in enumerate(lines)]


def test_line_language_is_confident_or_abstains():
    assert line_language("I never knew how to tell you that you were my life") == "en"
    assert line_language("Yo tengo tu nombre guardado en el corazón") == "es"
    assert line_language("oh oh") is None
    assert line_language("Llamas") is None


def test_translated_half_song_is_flagged_even_though_it_looks_bilingual():
    output = _segs(TRANSLATED + SPANISH)
    contract = build_language_contract(output, "", None, language_probe=PROBE_ES,
                                       infer_uncertain_from_request=False)
    assert contract["mixed_language"] is True
    assert contract["language_probe_conflict"] is True
    assert contract["language_conflict"] is True
    assert contract["needs_language_review"] is True
    assert contract["language_probe_conflict_indices"] == [0, 1, 2, 3]
    assert {0, 1, 2, 3} <= set(contract["output_reference_unexplained_indices"])
    assert contract["language_probe"]["main_language"] == "es"


def test_without_probe_the_same_output_is_still_exempt():
    contract = build_language_contract(_segs(TRANSLATED + SPANISH), "", None,
                                       infer_uncertain_from_request=False)
    assert contract["needs_language_review"] is False
    assert contract["language_probe"] is None


def test_real_bilingual_song_is_not_flagged():
    probe = {**PROBE_ES, "main_language": "es", "languages": ["en", "es"]}
    contract = build_language_contract(_segs(TRANSLATED + SPANISH), "", None,
                                       language_probe=probe)
    assert contract["language_probe_conflict"] is False


def test_english_song_heard_as_english_is_not_flagged():
    probe = {**PROBE_ES, "main_language": "en", "languages": ["en"]}
    assert probe_conflict_indices(_segs(TRANSLATED), probe) == []


def test_single_foreign_line_is_not_enough():
    assert probe_conflict_indices(_segs(SPANISH + TRANSLATED[:1]), PROBE_ES) == []


def test_non_latin_script_is_flagged_when_probe_heard_spanish():
    output = _segs(SPANISH + ["アルーサージンガー", "おやすみなさい"])
    assert probe_conflict_indices(output, PROBE_ES) == [4, 5]


@pytest.mark.parametrize("probe", [
    None,
    {**PROBE_ES, "status": "unavailable", "languages": []},
    {**PROBE_ES, "status": "no_vocals", "languages": []},
    # Catalan cannot be classified line by line: abstain, never coerce.
    {**PROBE_ES, "languages": ["ca", "es"]},
])
def test_probe_abstains(probe):
    assert probe_conflict_indices(_segs(TRANSLATED + SPANISH), probe) == []


def test_gate_recomputes_from_persisted_probe_and_survives_an_edit():
    segments = _segs(TRANSLATED + SPANISH)
    quality = {"language_probe": PROBE_ES, "unsafe_windows": []}
    assert review_payload(segments, quality, 0)["needs_language_review"] is True
    edited = supersede_pending_analysis(quality, revision=1, segments=segments)
    assert edited["language_probe"] == PROBE_ES
    assert review_payload(segments, edited, 1)["needs_language_review"] is True
    # Once the reviewer rewrites the verses in Spanish the alert clears.
    assert review_payload(_segs(SPANISH * 2), edited, 2)["needs_language_review"] is False


def test_parse_probe_response_normalizes_and_tolerates_truncation():
    ok = language_probe.parse_probe_response(json.dumps({
        "main_language": "ES", "sections": [
            {"start_s": 0, "end_s": 20, "language": "none"},
            {"start_s": 20, "end_s": 180, "language": "es"},
        ]}))
    assert ok["status"] == "ok" and ok["languages"] == ["es"] and ok["main_language"] == "es"
    truncated = language_probe.parse_probe_response(
        '{"main_language":"en","sections":[{"start_s":0,"end_s":30,"language":"en"},'
        '{"start_s":30,"end_s":32,"language":"no')
    # Recovered sections stay visible, but the list is not exhaustive.
    assert truncated["status"] == "incomplete" and truncated["languages"] == ["en"]
    assert truncated["complete"] is False
    assert truncated["incomplete_reason"] == "truncated_response"
    assert ok["complete"] is True and ok["incomplete_reason"] is None
    silent = language_probe.parse_probe_response(
        '{"main_language":"none","sections":[{"start_s":0,"end_s":180,"language":"none"}]}')
    assert silent["status"] == "no_vocals" and silent["languages"] == []
    assert language_probe.parse_probe_response("not json")["status"] == "unavailable"


class _FakeModels:
    def __init__(self, answers):
        self.answers = list(answers)
        self.calls = 0

    def generate_content(self, **_kwargs):
        self.calls += 1
        answer = self.answers.pop(0)
        if isinstance(answer, BaseException):
            raise answer
        if isinstance(answer, tuple):
            text, finish = answer
            return types.SimpleNamespace(
                text=text,
                candidates=[types.SimpleNamespace(
                    finish_reason=types.SimpleNamespace(name=finish))],
            )
        return types.SimpleNamespace(text=answer)


def _install_fake_client(monkeypatch, answers):
    models = _FakeModels(answers)
    client = types.SimpleNamespace(models=models)
    import pipeline
    monkeypatch.setattr(pipeline, "_get_genai_client", lambda: client)
    monkeypatch.setattr(pipeline, "_call_with_timeout",
                        lambda fn, timeout_s, label="": fn())
    monkeypatch.setattr(language_probe, "_compact_audio", lambda path, tmp: path)
    return models


def test_probe_retries_quota_errors(monkeypatch, tmp_path):
    audio = tmp_path / "a.wav"
    audio.write_bytes(b"RIFF")
    models = _install_fake_client(monkeypatch, [
        RuntimeError("429 RESOURCE_EXHAUSTED"),
        '{"main_language":"es","sections":[{"start_s":0,"end_s":100,"language":"es"}]}',
    ])
    probe = language_probe.probe_sung_languages(str(audio), backoff=(0.0, 0.0, 0.0))
    assert probe["status"] == "ok" and probe["languages"] == ["es"]
    assert probe["attempts"] == 2 and models.calls == 2


def test_probe_gives_up_without_raising(monkeypatch, tmp_path):
    audio = tmp_path / "a.wav"
    audio.write_bytes(b"RIFF")
    _install_fake_client(monkeypatch, [RuntimeError("boom")])
    probe = language_probe.probe_sung_languages(str(audio), backoff=(0.0, 0.0))
    assert probe["status"] == "unavailable" and probe["reason"] == "call_failed"


def test_probe_kill_switch(monkeypatch, tmp_path):
    monkeypatch.setenv("SUNG_LANGUAGE_PROBE_ENABLED", "0")
    assert language_probe.probe_sung_languages(str(tmp_path / "x.wav"))["reason"] == "disabled"


def test_probe_module_never_requests_lyrics():
    prompt = language_probe._PROMPT.lower()
    assert "do not transcribe" in prompt and "translate" in prompt


# --- Regression: review r4618337711 (truncated Gemini answer) -------------
# An English intro section parsed completely, then the answer is cut inside the
# Spanish section. The recovered list says ["en"]; treating it as exhaustive
# flagged two correct Spanish verses and blocked approval.
TRUNCATED_EN_THEN_CUT_ES = (
    '{"main_language":"en","sections":[{"start_s":0,"end_s":30,"language":"en"},'
    '{"start_s":30,"end_s":180,"language":"e'
)
TWO_SPANISH_VERSES = _segs([
    "Yo tengo tu nombre guardado en el corazón",
    "Nunca supe decirte que eras mi vida",
])


def test_truncated_answer_never_gates_valid_spanish_verses():
    probe = language_probe.parse_probe_response(TRUNCATED_EN_THEN_CUT_ES)
    assert probe["status"] == "incomplete" and probe["complete"] is False
    assert probe["languages"] == ["en"]
    assert probe_conflict_indices(TWO_SPANISH_VERSES, probe) == []
    review = review_payload(TWO_SPANISH_VERSES, {"language_probe": probe}, 0)
    assert review["language_probe_conflict"] is False
    assert review["needs_language_review"] is False
    assert review["language_probe"]["complete"] is False
    assert review["language_probe"]["status"] == "incomplete"


@pytest.mark.parametrize("finish", ["MAX_TOKENS", "SAFETY", "RECITATION"])
def test_parseable_json_with_incomplete_finish_reason_abstains(finish):
    probe = language_probe.parse_probe_response(
        '{"main_language":"en","sections":[{"start_s":0,"end_s":30,"language":"en"}]}',
        finish_reason=finish,
    )
    assert probe["status"] == "incomplete" and probe["complete"] is False
    assert probe["incomplete_reason"] == f"finish_{finish.lower()}"
    review = review_payload(TWO_SPANISH_VERSES, {"language_probe": probe}, 0)
    assert review["needs_language_review"] is False


def test_complete_answer_still_gates_the_same_lines():
    """Control: an exhaustive English-only answer keeps flagging them."""
    probe = language_probe.parse_probe_response(
        '{"main_language":"en","sections":[{"start_s":0,"end_s":180,"language":"en"}]}',
        finish_reason="STOP",
    )
    assert probe["status"] == "ok" and probe["complete"] is True
    review = review_payload(TWO_SPANISH_VERSES, {"language_probe": probe}, 0)
    assert review["language_probe_conflict"] is True
    assert review["needs_language_review"] is True
    assert review["language_probe_conflict_indices"] == [0, 1]


def test_persisted_probe_marked_incomplete_abstains_even_if_status_ok():
    legacy = {**PROBE_ES, "main_language": "en", "languages": ["en"], "complete": False}
    assert probe_conflict_indices(TWO_SPANISH_VERSES, legacy) == []


def test_probe_records_max_tokens_from_the_provider(monkeypatch, tmp_path):
    audio = tmp_path / "a.wav"
    audio.write_bytes(b"RIFF")
    _install_fake_client(monkeypatch, [(
        '{"main_language":"en","sections":[{"start_s":0,"end_s":30,"language":"en"}]}',
        "MAX_TOKENS",
    )])
    probe = language_probe.probe_sung_languages(str(audio), backoff=(0.0,))
    assert probe["status"] == "incomplete"
    assert probe["incomplete_reason"] == "finish_max_tokens"


def test_finish_reason_name_normalizes_sdk_enums():
    enum_like = types.SimpleNamespace(candidates=[types.SimpleNamespace(
        finish_reason="FinishReason.MAX_TOKENS")])
    assert language_probe.finish_reason_name(enum_like) == "MAX_TOKENS"
    assert language_probe.finish_reason_name(types.SimpleNamespace(text="{}")) == ""
