"""Letra oficial pegada en la subida: una fila por línea, en orden, ninguna
perdida (incidente prod 11-nov-2026: líneas fundidas y las 2 últimas
borradas en un fragmento de 30 s).

Letras SINTÉTICAS a propósito: nunca pegar letras con copyright en el repo.
"""
import asyncio

import pytest

import ctc_align
import reference_lines as RL
import vocal_sep
from main import _maybe_anchor_align, _maybe_timing_consistency


# 8 líneas: estribillo repetido (1 y 5), una interjección corta (3) y las dos
# últimas cerca del final de un audio de 30 s.
LINES = [
    "linha sintetica do refrao inicial",
    "segunda frase inventada para o teste",
    "Uh oh uh oh",
    "quarta frase que ninguem canta de verdade",
    "linha sintetica do refrao inicial",
    "sexta frase do fragmento curto",
    "setima frase quase no final",
    "oitava frase colada ao fim do audio",
]
ANCHOR = "\n".join(LINES)
AUDIO_S = 30.0


def _row(text, start, end, score=0.9):
    tokens = text.split()
    step = (end - start) / len(tokens)
    return {
        "start": start, "end": end, "text": text,
        "words": [
            {"word": w, "start": round(start + i * step, 3),
             "end": round(start + (i + 1) * step - 0.02, 3), "score": score}
            for i, w in enumerate(tokens)
        ],
    }


def _hosted_tail_cut():
    """Forma real de forced_align.wordstamps_to_segments cuando el guard de
    pile-up corta la cola: 6 de 8 líneas, las dos últimas no vuelven."""
    starts = [1.0, 4.5, 8.0, 10.0, 14.0, 18.0]
    return [_row(LINES[i], s, s + 3.0) for i, s in enumerate(starts)]


def _assert_contract(segments, *, audio_s=AUDIO_S):
    assert [s["text"] for s in segments] == LINES
    starts = [float(s["start"]) for s in segments]
    assert all(b > a for a, b in zip(starts, starts[1:])), starts
    for s in segments:
        assert 0.0 <= float(s["start"]) < float(s["end"]) <= audio_s + 1e-6, s


# ---------------------------------------------------------------------------
# complete_alignment (puro)
# ---------------------------------------------------------------------------


def test_split_reference_lines_strips_and_drops_blank_lines():
    assert RL.split_reference_lines("  uno  \n\n dos\t\r\n   \ntres ") == [
        "uno", "dos", "tres",
    ]


def test_missing_tail_lines_are_interpolated_inside_the_audio_and_flagged():
    out, stats = RL.complete_alignment(
        LINES, _hosted_tail_cut(), audio_duration=AUDIO_S,
    )
    _assert_contract(out)
    assert stats["missing"] == 2 and stats["interpolated"] == 2
    for tail in out[-2:]:
        assert tail["interpolated"] is True
        assert tail["review"] is True
        assert RL.REVIEW_REASON in tail["review_reasons"]
        assert "words" not in tail
    # Lo que el alineador sí ancló no se toca.
    assert out[:6] == [dict(r) for r in _hosted_tail_cut()]


def test_tail_lines_pushed_past_the_end_of_the_audio_are_put_back_inside():
    """Whisper-DP: la monotonía empuja la cola más allá del fin y el clamp
    la deja con end <= start; antes eso hacía declinar todo el fallback."""
    rows = _hosted_tail_cut() + [
        {"start": 30.2, "end": 30.0, "text": LINES[6]},
        {"start": 30.4, "end": 30.0, "text": LINES[7]},
    ]
    out, stats = RL.complete_alignment(LINES, rows, audio_duration=AUDIO_S)
    _assert_contract(out)
    assert stats["invalid_timing"] == 2


def test_ctc_zero_length_tail_gets_room_by_trimming_the_previous_line():
    """CTC deja las no ancladas de la cola en (dur, dur): sin lugar entre la
    anterior y el fin del audio, se acorta la COLA de la anterior."""
    rows = [_row(LINES[i], 1.0 + i * 4.0, 4.5 + i * 4.0) for i in range(5)]
    rows.append(_row(LINES[5], 21.0, AUDIO_S))
    rows += [{"start": AUDIO_S, "end": AUDIO_S, "text": LINES[6]},
             {"start": AUDIO_S, "end": AUDIO_S, "text": LINES[7]}]
    out, _stats = RL.complete_alignment(LINES, rows, audio_duration=AUDIO_S)
    _assert_contract(out)
    assert out[5]["start"] == 21.0          # el inicio de la anterior no se mueve
    assert out[5]["end"] < AUDIO_S
    assert out[7]["end"] <= AUDIO_S


def test_lost_middle_interjection_is_interpolated_between_its_neighbours():
    rows = [r for i, r in enumerate(_hosted_tail_cut()) if i != 2]
    rows += [_row(LINES[6], 22.0, 25.0), _row(LINES[7], 25.5, 29.0)]
    out, stats = RL.complete_alignment(LINES, rows, audio_duration=AUDIO_S)
    _assert_contract(out)
    assert stats["missing"] == 1
    assert out[1]["end"] <= out[2]["start"] < out[2]["end"] <= out[3]["start"]


def test_complete_alignment_is_identity_when_nothing_is_missing():
    rows = _hosted_tail_cut() + [_row(LINES[6], 22.0, 25.0),
                                 _row(LINES[7], 25.5, 29.0)]
    out, stats = RL.complete_alignment(LINES, rows, audio_duration=AUDIO_S)
    assert out is rows
    assert stats["interpolated"] == 0


def test_complete_alignment_declines_when_there_is_no_room_left():
    rows = [_row(LINES[i], i * 0.1, i * 0.1 + 0.05) for i in range(6)]
    out, _stats = RL.complete_alignment(LINES, rows, audio_duration=0.6)
    assert out is rows


# ---------------------------------------------------------------------------
# _maybe_anchor_align (subida con letra oficial)
# ---------------------------------------------------------------------------


def _no_stem(monkeypatch):
    monkeypatch.setattr(vocal_sep, "separate_vocals",
                        lambda path, cache_only=False: None)
    monkeypatch.setenv("CTC_ALIGN_COMPUTE_STEM", "0")


def _provider_result():
    return {
        "job_id": "j-ref-lines",
        "segments": [{"start": 0.0, "end": 30.0, "text": "asr libre fundido"}],
        "reference_lyrics": "",
    }


def _run(anchor, *, audio_path, **kw):
    return asyncio.run(_maybe_anchor_align(
        _provider_result(), audio_path, "j-ref-lines", anchor, **kw,
    ))


@pytest.fixture
def audio_30s(tmp_path, monkeypatch):
    path = tmp_path / "fragmento.mp3"
    path.write_bytes(b"\x00")
    monkeypatch.setattr("pipeline._audio_duration", lambda _p: AUDIO_S)
    return str(path)


def test_upload_keeps_all_8_pasted_lines_when_hosted_aligner_cuts_the_tail(
    monkeypatch, audio_30s,
):
    monkeypatch.setenv("ANCHOR_LYRICS_ENABLED", "1")
    _no_stem(monkeypatch)
    # Portugués: el CTC (sólo español) declina.
    monkeypatch.setattr(ctc_align, "retime_segments", lambda *_a, **_kw: None)
    monkeypatch.setattr("forced_align.forced_align_lyrics",
                        lambda *_a, **_kw: _hosted_tail_cut())
    monkeypatch.setattr(
        "lyrics_whisper_align.whisper_word_align",
        lambda *_a, **_kw: (_ for _ in ()).throw(
            AssertionError("the completed hosted alignment must be accepted")),
    )
    # Trailing spaces from a web paste must not break the exact comparison.
    out = _run("\n".join(f"{line}  " for line in LINES) + "\n\n", audio_path=audio_30s)

    assert out["anchor_alignment"]["status"] == "applied"
    assert out["anchor_alignment"]["timing_source"] == "forced_align"
    _assert_contract(out["segments"])
    assert out["anchor_alignment"]["review_count"] >= 2
    assert all(s["review"] for s in out["segments"][-2:])
    contract = out[RL.CONTRACT_KEY]
    assert [s["text"] for s in contract["segments"]] == LINES


def test_upload_ctc_zero_length_tail_is_repaired_not_left_outside_the_audio(
    monkeypatch, audio_30s,
):
    monkeypatch.setenv("ANCHOR_LYRICS_ENABLED", "1")
    _no_stem(monkeypatch)
    rows = [_row(LINES[i], 1.0 + i * 4.0, 4.5 + i * 4.0) for i in range(6)]
    rows[5]["end"] = AUDIO_S
    rows += [{"start": AUDIO_S, "end": AUDIO_S, "text": LINES[6]},
             {"start": AUDIO_S, "end": AUDIO_S, "text": LINES[7]}]
    monkeypatch.setattr(ctc_align, "retime_segments", lambda *_a, **_kw: rows)
    out = _run(ANCHOR, audio_path=audio_30s)
    assert out["anchor_alignment"]["timing_source"] == "ctc_timing_only"
    _assert_contract(out["segments"])


def test_catalog_reference_keeps_the_strict_contract(monkeypatch, audio_30s):
    """La completitud es sólo para lo que pegó el operador: una letra de
    catálogo con cola sobrante sigue siendo otra versión."""
    _no_stem(monkeypatch)
    monkeypatch.setattr(ctc_align, "retime_segments", lambda *_a, **_kw: None)
    monkeypatch.setattr("forced_align.forced_align_lyrics",
                        lambda *_a, **_kw: _hosted_tail_cut())
    monkeypatch.setattr("lyrics_whisper_align.whisper_word_align",
                        lambda *_a, **_kw: None)
    monkeypatch.setattr("lyrics_local_forced_align.local_forced_align",
                        lambda *_a, **_kw: None)
    monkeypatch.setattr("lyrics_local_forced_align.detect_audio_language",
                        lambda *_a, **_kw: "pt")
    out = _run(ANCHOR, audio_path=audio_30s,
               content_source="catalog_reference", enabled=True)
    assert out["anchor_alignment"]["status"] == "declined"
    assert RL.CONTRACT_KEY not in out


# ---------------------------------------------------------------------------
# Contrato en el último borde (_maybe_timing_consistency)
# ---------------------------------------------------------------------------


def _anchored():
    rows = _hosted_tail_cut() + [_row(LINES[6], 22.0, 25.0),
                                 _row(LINES[7], 25.5, 29.0)]
    return [dict(r, content_source="operator_reference") for r in rows]


def test_postpasses_that_merge_rewrite_and_drop_rows_are_undone():
    anchored = _anchored()
    mutated = [dict(r) for r in anchored[:6]]
    # chorus_snap / adlib collapse style merge of rows 2 and 3 …
    mutated[2] = dict(mutated[2], text=f"{LINES[2]} {LINES[3]}",
                      end=mutated[3]["end"])
    del mutated[3]
    # … an orthographic rewrite with only punctuation/case changes …
    mutated[0] = dict(mutated[0], text=LINES[0].capitalize() + ".")
    # … a refined timing on a row that survived intact …
    mutated[3] = dict(mutated[3], start=14.2)
    # … and the last two rows dropped by a tail filter.
    result = {"segments": mutated,
              RL.CONTRACT_KEY: RL.build_contract(anchored)}

    out = _maybe_timing_consistency(result, "j-ref-lines")

    _assert_contract(out["segments"])
    stats = out["postpass_stats"]["reference_line_contract"]
    assert stats["final_rows_before"] == 5 and stats["reference_rows"] == 8
    assert stats["restored"] == 4          # the 2 merged + the 2 dropped
    assert stats["text_restored"] == 1     # verbatim official text again
    assert out["segments"][4]["start"] == pytest.approx(14.2)  # kept refinement


def test_contract_is_a_noop_when_rows_already_match():
    anchored = _anchored()
    result = {"segments": anchored, RL.CONTRACT_KEY: RL.build_contract(anchored)}
    out = _maybe_timing_consistency(result, "j-ref-lines")
    assert [s["text"] for s in out["segments"]] == LINES
    assert "reference_line_contract" not in (out.get("postpass_stats") or {})


def test_results_without_official_lyrics_are_untouched_by_the_contract():
    segments = [{"start": 0.0, "end": 2.0, "text": "asr uno"},
                {"start": 2.5, "end": 4.0, "text": "asr dos"}]
    result = {"segments": segments}
    out = _maybe_timing_consistency(result, "j-no-ref")
    assert [s["text"] for s in out["segments"]] == ["asr uno", "asr dos"]
    assert "reference_line_contract" not in (out.get("postpass_stats") or {})


def test_worker_strips_the_contract_before_persisting():
    import pathlib
    src = (pathlib.Path(__file__).resolve().parent.parent
           / "transcription_worker.py").read_text()
    assert src.count('r.pop("_reference_line_contract", None)') >= 2


# ---------------------------------------------------------------------------
# Review fixes (PR #1582)
# ---------------------------------------------------------------------------


def test_reserved_tail_survives_the_final_timing_consistency_pass():
    """Two lost lines after a word-aligned line that ends at 30 s reserve
    29–30 s. The neighbour's word stamps must be clipped too, or the final
    timing pass re-extends it to its last word (30 s) over the restored tail."""
    lines = LINES[:4]
    rows = [_row(lines[0], 1.0, 9.0), _row(lines[1], 10.0, AUDIO_S)]
    rows[1]["words"][-1]["end"] = AUDIO_S
    out, _stats = RL.complete_alignment(lines, rows, audio_duration=AUDIO_S)
    assert out[1]["end"] == pytest.approx(29.0)
    assert all(w["end"] <= 29.0 + 1e-6 for w in out[1]["words"])

    result = {"segments": out, RL.CONTRACT_KEY: RL.build_contract(out)}
    final = _maybe_timing_consistency(result, "j-ref-lines")["segments"]

    assert [s["text"] for s in final] == lines
    assert final[1]["end"] <= final[2]["start"] + 1e-6, final[1]
    assert final[2]["end"] <= final[3]["start"] + 1e-6
    assert final[3]["end"] <= AUDIO_S + 1e-6


def test_next_neighbour_words_are_clipped_out_of_a_restored_middle_line():
    rows = [r for i, r in enumerate(_hosted_tail_cut()) if i != 2]
    # The line after the hole carries a word stamped before its start.
    first_word = {"word": LINES[3].split()[0], "start": 7.0, "end": 10.4, "score": 0.9}
    rows[2] = dict(rows[2], words=[first_word] + rows[2]["words"][1:])
    rows += [_row(LINES[6], 22.0, 25.0), _row(LINES[7], 25.5, 29.0)]
    out, _stats = RL.complete_alignment(LINES, rows, audio_duration=AUDIO_S)
    final = _maybe_timing_consistency(
        {"segments": out, RL.CONTRACT_KEY: RL.build_contract(out)}, "j",
    )["segments"]
    assert [s["text"] for s in final] == LINES
    assert final[2]["end"] <= final[3]["start"] + 1e-6
    assert final[3]["start"] == pytest.approx(10.0)


_CHORUS = ["refrao sintetico um", "refrao sintetico dois",
           "refrao sintetico um", "refrao sintetico dois", "ponte final"]


def _chorus_anchor():
    return [_row(t, s, s + 5.0)
            for t, s in zip(_CHORUS, [0.0, 10.0, 20.0, 30.0, 40.0])]


def test_deleted_repeated_chorus_rows_are_restored_at_their_own_occurrence():
    """Anchor A B A B C at 0/10/20/30/40; post-passes keep the first A B and C.
    Text-only block matching paired the survivors with the SECOND A B and
    restored 0, 10, 10.05, 10.1, 40."""
    anchored = _chorus_anchor()
    survivors = [dict(anchored[0]), dict(anchored[1]), dict(anchored[4])]
    out, stats = RL.enforce_contract(survivors, RL.build_contract(anchored))
    assert [s["text"] for s in out] == _CHORUS
    assert [s["start"] for s in out] == [0.0, 10.0, 20.0, 30.0, 40.0]
    assert [bool(s.get("reference_line_restored")) for s in out] == [
        False, False, True, True, False,
    ]
    assert stats["restored"] == 2


def test_survivor_of_the_second_chorus_keeps_its_own_occurrence():
    anchored = _chorus_anchor()
    refined = dict(anchored[2], start=20.3)
    out, _stats = RL.enforce_contract([refined, dict(anchored[4])],
                                      RL.build_contract(anchored))
    assert [s["start"] for s in out] == [0.0, 10.0, 20.3, 30.0, 40.0]
