"""Tests for lyrics_format — orthographic correction + line splitting.

All tests mock the OpenAI client (no network). Timing tests exercise
_split_by_words directly to verify timestamp assignment.
"""
import asyncio
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import lyrics_format as lf


# ── helpers ───────────────────────────────────────────────────────────────────

def _seg(text, start=0.0, end=4.0, words=None):
    s = {"text": text, "start": start, "end": end}
    if words is not None:
        s["words"] = words
    return s


def _word(w, start, end, score=0.9):
    return {"word": w, "start": start, "end": end, "score": score}


def _result(segs):
    return {"segments": segs, "job_id": "test"}


def _run(coro):
    # Python 3.11 no longer creates a replacement loop implicitly after a
    # previous test closes the process-global one. Give every sync wrapper an
    # isolated lifecycle so this file is independent of suite order.
    return asyncio.run(coro)


def _patch_openai(monkeypatch, lines_per_seg: "dict[int, list[str]]"):
    """Patch AsyncOpenAI to return a numbered response.

    lines_per_seg: {1-based index: [sub_text, ...]}
    Segments with 1 entry emit 'N. text'; with >1 emit repeated 'N. text'.
    """
    out_lines = []
    for idx in sorted(lines_per_seg):
        for text in lines_per_seg[idx]:
            out_lines.append(f"{idx}. {text}")
    response_text = "\n".join(out_lines)

    class _Choice:
        class message:
            content = response_text

    class _Resp:
        choices = [_Choice()]

    class _FakeCompletions:
        async def create(self, **_kw):
            return _Resp()

    class _FakeChat:
        completions = _FakeCompletions()

    class _FakeClient:
        chat = _FakeChat()

    import openai
    import provenance

    class _Recorder:
        def finish(self, **_kwargs):
            pass

    monkeypatch.setattr(
        provenance, "record_ai_call", lambda **_kwargs: _Recorder(),
    )
    monkeypatch.setattr(openai, "AsyncOpenAI", lambda: _FakeClient())
    monkeypatch.setattr(lf.asyncio, "wait_for", lambda coro, **_kw: coro)


# ── orthographic correction ───────────────────────────────────────────────────

def test_corrects_accents(monkeypatch):
    segs = [_seg("fragil espejo de voz"), _seg("para que tus santos")]
    _patch_openai(monkeypatch, {
        1: ["Frágil espejo de voz"],
        2: ["¿Para qué tus santos?"],
    })
    result = _run(lf.format_lyrics_pass(_result(segs), language="es"))
    assert result["segments"][0]["text"] == "Frágil espejo de voz"
    assert result["segments"][1]["text"] == "¿Para qué tus santos?"


def test_formatter_registra_el_job_y_modelo_openai(monkeypatch):
    import provenance

    _patch_openai(monkeypatch, {1: ["Frágil espejo de voz"]})
    captured = {}

    class _Recorder:
        def finish(self, **kwargs):
            captured["summary"] = kwargs["response_summary"]

    monkeypatch.setattr(
        provenance,
        "record_ai_call",
        lambda **kwargs: captured.update(kwargs) or _Recorder(),
    )

    _run(lf.format_lyrics_pass(_result([_seg("fragil espejo de voz")]), "es"))

    assert captured["job_id"] == "test"
    assert captured["tool_name"] == "gpt-4o-mini"
    assert captured["tool_provider"] == "openai"
    assert captured["summary"] == "succeeded"


def test_timestamps_unchanged_no_split(monkeypatch):
    segs = [_seg("fragil espejo", 10.0, 14.5)]
    _patch_openai(monkeypatch, {1: ["Frágil espejo"]})
    result = _run(lf.format_lyrics_pass(_result(segs), language="es"))
    seg = result["segments"][0]
    assert seg["start"] == 10.0
    assert seg["end"] == 14.5


def test_unchanged_text_keeps_same_object(monkeypatch):
    """If LLM returns same text, the original segment dict is reused."""
    segs = [_seg("Ya está bien")]
    _patch_openai(monkeypatch, {1: ["Ya está bien"]})
    result = _run(lf.format_lyrics_pass(_result(segs), language="es"))
    assert result["segments"][0] is segs[0]


# ── line splitting — timing with word timestamps ───────────────────────────────

def test_split_uses_word_timestamps(monkeypatch):
    """Long segment with word timestamps → sub-lines get timestamps from word list."""
    words = [
        _word("para",     0.0,  0.3),
        _word("que",      0.3,  0.5),
        _word("tus",      0.5,  0.7),
        _word("santos",   0.7,  1.1),
        _word("tengan",   1.1,  1.4),
        _word("fe",       1.4,  1.7),
        _word("para",     1.7,  2.0),
        _word("que",      2.0,  2.2),
        _word("el",       2.2,  2.4),
        _word("diablo",   2.4,  2.9),
        _word("pierda",   2.9,  3.5),
    ]
    seg = _seg("para que tus santos tengan fe para que el diablo pierda",
               0.0, 3.5, words=words)
    _patch_openai(monkeypatch, {
        1: ["¿Para qué tus santos tengan fe?", "¿Para qué el diablo pierda?"],
    })

    result = _run(lf.format_lyrics_pass(_result([seg]), language="es"))
    out = result["segments"]

    assert len(out) == 2
    # First sub-line covers roughly the first half of words
    assert out[0]["start"] == 0.0
    assert out[0]["end"] > 0.0
    assert out[0]["end"] <= out[1]["start"] or out[0]["end"] == out[1]["start"]
    # Second sub-line ends at original segment end
    assert out[1]["end"] == 3.5
    # Texts are correct
    assert "santos" in out[0]["text"]
    assert "diablo" in out[1]["text"]


def test_split_proportional_fallback_no_words(monkeypatch):
    """Segment without word timestamps → proportional character-count split."""
    seg = _seg("para que tus santos tengan fe para que el diablo pierda",
               10.0, 20.0)  # 10s span, no words key
    _patch_openai(monkeypatch, {
        1: ["¿Para qué tus santos tengan fe?", "¿Para qué el diablo pierda?"],
    })

    result = _run(lf.format_lyrics_pass(_result([seg]), language="es"))
    out = result["segments"]

    assert len(out) == 2
    assert out[0]["start"] == 10.0
    assert out[1]["end"] == 20.0
    # Non-overlapping
    assert out[0]["end"] <= out[1]["start"]
    # Total span preserved
    assert out[1]["end"] - out[0]["start"] == pytest.approx(10.0, abs=0.01)


def test_no_split_short_segment(monkeypatch):
    """Short segments returned as single lines even if LLM just corrects them."""
    seg = _seg("frágil espejo de voz", 5.0, 8.0)
    _patch_openai(monkeypatch, {1: ["Frágil espejo de voz"]})

    result = _run(lf.format_lyrics_pass(_result([seg]), language="es"))
    assert len(result["segments"]) == 1
    assert result["segments"][0]["start"] == 5.0
    assert result["segments"][0]["end"] == 8.0


def test_split_timing_monotonic(monkeypatch):
    """Sub-segment timestamps must be strictly non-decreasing."""
    words = [_word(f"w{i}", i * 0.5, i * 0.5 + 0.4) for i in range(12)]
    seg = _seg(" ".join(f"w{i}" for i in range(12)), 0.0, 6.0, words=words)
    _patch_openai(monkeypatch, {
        1: ["w0 w1 w2 w3", "w4 w5 w6 w7", "w8 w9 w10 w11"],
    })

    result = _run(lf.format_lyrics_pass(_result([seg]), language="es"))
    out = result["segments"]

    assert len(out) == 3
    for prev, nxt in zip(out, out[1:]):
        assert prev["end"] <= nxt["start"] + 0.01, (
            f"non-monotonic: {prev['end']} > {nxt['start']}"
        )
    assert out[0]["start"] >= 0.0
    assert out[-1]["end"] == pytest.approx(6.0, abs=0.01)


def test_multi_segment_mixed(monkeypatch):
    """Mix of split and non-split segments in the same pass."""
    segs = [
        _seg("fragil espejo de voz", 0.0, 3.0),   # short → correct only
        _seg("para que tus santos tengan fe para que el diablo pierda",
             5.0, 10.0),                           # long → split
        _seg("tanto miedo", 12.0, 14.0),           # short → correct only
    ]
    _patch_openai(monkeypatch, {
        1: ["Frágil espejo de voz"],
        2: ["¿Para qué tus santos tengan fe?", "¿Para qué el diablo pierda?"],
        3: ["Tanto miedo"],
    })

    result = _run(lf.format_lyrics_pass(_result(segs), language="es"))
    out = result["segments"]

    assert len(out) == 4
    assert out[0]["text"] == "Frágil espejo de voz"
    assert out[0]["start"] == 0.0 and out[0]["end"] == 3.0
    # Split segment
    assert "santos" in out[1]["text"]
    assert "diablo" in out[2]["text"]
    assert out[1]["start"] == 5.0
    assert out[2]["end"] == 10.0
    # Third original segment
    assert out[3]["text"] == "Tanto miedo"
    assert out[3]["start"] == 12.0 and out[3]["end"] == 14.0


def test_code_switched_line_is_never_translated(monkeypatch):
    """A Spanish primary language must not translate an English phrase."""
    segs = [
        _seg("Are you ready?", 0.0, 1.5),
        _seg("fragil corazón", 2.0, 4.0),
    ]
    _patch_openai(monkeypatch, {
        1: ["Estoy listo"],
        2: ["Frágil corazón"],
    })

    result = _run(lf.format_lyrics_pass(_result(segs), language="es"))

    assert result["segments"][0]["text"] == "Are you ready?"
    assert result["segments"][1]["text"] == "Frágil corazón"


def test_code_switch_prompt_preserves_each_lines_language():
    prompt = lf._build_prompt(["Are you ready?", "Yo estoy listo"], "Spanish")

    assert "code-switch" in prompt
    assert "ORIGINAL language of EACH line" in prompt
    assert "NEVER translate or paraphrase" in prompt
    assert "NEVER end a lyric display line with a full stop" in prompt


def test_lexical_guard_allows_only_orthographic_changes():
    assert lf.preserves_lexical_content("ARE YOU READY", "¿Are you ready?")
    assert lf.preserves_lexical_content("fragil corazon", "Frágil corazón")
    assert not lf.preserves_lexical_content("Are you ready?", "Estoy listo")
    assert not lf.preserves_lexical_content("Home sweet home", "Hogar dulce hogar")
    assert not lf.preserves_lexical_content("Marca el 638", "Marca el 780465")


# ── failure / guard cases ─────────────────────────────────────────────────────

def test_missing_index_in_response_returns_original(monkeypatch):
    """If LLM skips a line index, original result is returned unchanged."""
    segs = [_seg("linea uno"), _seg("linea dos")]

    broken_content = "1. Línea uno"  # missing "2." — parse will fail

    class _Msg:
        content = broken_content

    class _Choice:
        message = _Msg()

    class _Resp:
        choices = [_Choice()]

    class _Completions:
        async def create(self, **_kw):
            return _Resp()

    class _Chat:
        completions = _Completions()

    class _BadClient:
        chat = _Chat()

    import openai
    monkeypatch.setattr(openai, "AsyncOpenAI", lambda: _BadClient())
    monkeypatch.setattr(lf.asyncio, "wait_for", lambda coro, **_kw: coro)

    r = _result(segs)
    result = _run(lf.format_lyrics_pass(r, language="es"))
    assert result["segments"][0]["text"] == "linea uno"
    assert result["segments"][1]["text"] == "linea dos"


def test_disabled_by_env(monkeypatch):
    monkeypatch.setenv("LYRICS_FORMAT_ENABLED", "0")
    r = _result([_seg("fragil espejo")])
    result = _run(lf.format_lyrics_pass(r, language="es"))
    assert result is r


def test_empty_segments_returns_original():
    r = {"segments": [], "job_id": "x"}
    assert _run(lf.format_lyrics_pass(r)) is r


def test_non_dict_result_returns_original():
    assert _run(lf.format_lyrics_pass(None)) is None  # type: ignore


def test_openai_failure_returns_original(monkeypatch):
    import openai

    class _BrokenClient:
        class chat:
            class completions:
                @staticmethod
                async def create(**_kw):
                    raise RuntimeError("network error")

    monkeypatch.setattr(openai, "AsyncOpenAI", lambda: _BrokenClient())
    r = _result([_seg("fragil espejo")])
    result = _run(lf.format_lyrics_pass(r, language="es"))
    assert result["segments"][0]["text"] == "fragil espejo"


# ── display layout policy ─────────────────────────────────────────────────────

def test_display_layout_repairs_vestido_de_besos_without_changing_timing():
    """Regression from the 2026-09 Chile campaign screenshots."""
    segs = [
        _seg(
            "En las mejillas. En", 42.5, 45.0381,
            words=[
                _word("en", 42.58, 42.74),
                _word("las", 42.82, 43.26),
                _word("mejillas", 43.34, 44.62),
                _word("En", 45.26, 45.36),
            ],
        ),
        _seg(
            "Sus labios un rojo carmesí.", 45.4, 48.94,
            words=[
                _word("sus", 45.48, 45.84),
                _word("labios", 46.0, 46.42),
                _word("un", 46.48, 46.68),
                _word("rojo", 46.84, 47.3),
                _word("carmesí", 47.6, 48.44),
            ],
        ),
    ]
    before_timing = [(row["start"], row["end"]) for row in segs]
    before_words = [word["word"] for row in segs for word in row["words"]]

    out = lf.polish_display_layout(_result(segs))["segments"]

    assert [row["text"] for row in out] == [
        "En las mejillas",
        "En sus labios un rojo carmesí",
    ]
    assert [(row["start"], row["end"]) for row in out] == before_timing
    assert [word["word"] for row in out for word in row["words"]] == before_words


def test_display_layout_moves_preposition_pair_and_keeps_exact_card_bounds():
    segs = [
        _seg(
            "Somos parte de los", 10.125, 12.875,
            words=[
                _word("Somos", 10.2, 10.6),
                _word("parte", 10.7, 11.1),
                _word("de", 12.45, 12.58),
                _word("los", 12.58, 12.7),
            ],
        ),
        _seg(
            "Rockers de verdad.", 12.9, 15.25,
            words=[
                _word("Rockers", 12.95, 13.5),
                _word("de", 13.6, 13.75),
                _word("verdad", 13.8, 14.4),
            ],
        ),
    ]

    out = lf.polish_display_layout(_result(segs))["segments"]

    assert [row["text"] for row in out] == [
        "Somos parte", "De los Rockers de verdad",
    ]
    assert [(row["start"], row["end"]) for row in out] == [
        (10.125, 12.875), (12.9, 15.25),
    ]


def test_display_layout_does_not_guess_without_close_word_evidence():
    no_words = [
        _seg("Se abre por la", 10.0, 12.0),
        _seg("Izquierda", 12.1, 13.0),
    ]
    far_from_boundary = [
        _seg(
            "Creo en", 20.0, 25.0,
            words=[_word("Creo", 20.1, 20.5), _word("en", 21.0, 21.2)],
        ),
        _seg("Ti", 25.1, 26.0, words=[_word("Ti", 25.2, 25.5)]),
    ]

    assert lf.polish_display_layout(_result(no_words))["segments"] is no_words
    assert lf.polish_display_layout(_result(far_from_boundary))["segments"] is far_from_boundary


def test_display_layout_keeps_standalone_connector_card_for_human_review():
    segs = [
        _seg("Que", 30.0, 30.3, words=[_word("Que", 30.0, 30.2)]),
        _seg("Al igual", 30.4, 31.2, words=[
            _word("Al", 30.45, 30.6), _word("igual", 30.65, 31.0),
        ]),
    ]

    assert lf.polish_display_layout(_result(segs))["segments"] is segs


def test_display_layout_does_not_confuse_que_or_de_with_connectors():
    segs = [
        _seg(
            "¿Por qué", 40.0, 41.0,
            words=[_word("Por", 40.1, 40.4), _word("qué", 40.5, 40.8)],
        ),
        _seg("Los gringos", 41.1, 42.0, words=[
            _word("Los", 41.15, 41.4), _word("gringos", 41.5, 41.9),
        ]),
        _seg(
            "Al final me dé", 50.0, 51.0,
            words=[
                _word("Al", 50.1, 50.2), _word("final", 50.25, 50.5),
                _word("me", 50.55, 50.7), _word("dé", 50.75, 50.9),
            ],
        ),
        _seg("Igual", 51.1, 52.0, words=[_word("Igual", 51.15, 51.8)]),
    ]

    assert lf.polish_display_layout(_result(segs))["segments"] is segs


def test_display_layout_only_removes_single_terminal_full_stops():
    segs = [
        _seg("Una loca yo me enamoré."),
        _seg("¿Me escuchás?"),
        _seg("Sigue..."),
        _seg("Avíseme. Suele usar rubor"),
    ]

    out = lf.polish_display_layout(_result(segs))["segments"]

    assert [row["text"] for row in out] == [
        "Una loca yo me enamoré",
        "¿Me escuchás?",
        "Sigue...",
        "Avíseme. Suele usar rubor",
    ]


def test_display_layout_runs_when_formatter_is_disabled(monkeypatch):
    monkeypatch.setenv("LYRICS_FORMAT_ENABLED", "0")
    result = _run(lf.format_lyrics_pass(_result([_seg("No sé.")])))
    assert result["segments"][0]["text"] == "No sé"


# ── _split_by_words unit tests (timing contract) ──────────────────────────────

def test_split_by_words_exact_boundary():
    """Word timestamps: first sub-line ends where second begins."""
    words = [
        _word("a", 0.0, 1.0),
        _word("b", 1.0, 2.0),
        _word("c", 2.0, 3.0),
        _word("d", 3.0, 4.0),
    ]
    seg = _seg("a b c d", 0.0, 4.0, words=words)
    result = lf._split_by_words(seg, ["a b", "c d"])

    assert len(result) == 2
    assert result[0]["start"] == 0.0
    assert result[0]["end"] > 0.0
    assert result[1]["end"] == pytest.approx(4.0, abs=0.01)
    # No gap: second starts where first ends
    assert result[0]["end"] <= result[1]["start"] + 0.01


def test_split_by_words_single_passthrough():
    seg = _seg("only one line", 5.0, 8.0)
    result = lf._split_by_words(seg, ["Only one line"])
    assert len(result) == 1
    assert result[0]["text"] == "Only one line"
    assert result[0]["start"] == 5.0
    assert result[0]["end"] == 8.0


def test_split_by_words_no_words_proportional():
    seg = _seg("aaaa bbbb", 0.0, 10.0)  # no "words" key
    result = lf._split_by_words(seg, ["aaaa", "bbbb"])
    assert len(result) == 2
    assert result[0]["start"] == 0.0
    assert result[1]["end"] == 10.0
    assert result[0]["end"] == pytest.approx(result[1]["start"], abs=0.001)
