"""Avisos de sincronización de la Revisión rápida (lyric_timing_review).

Fixtures inventados con la forma de los casos reales de los pedidos de UMG
de sep-nov 2026: la primera palabra alineada estirada sobre el silencio
("aparece antes de escuchar la voz"), la línea que se corta antes de la
última palabra ("alargar hasta 1:13") y la que entra tarde.
"""
import pytest

import lyric_timing_review as ltr
from lyric_review import build_review


def _w(word, start, end, score=0.9):
    return {"word": word, "start": start, "end": end, "score": score}


def _spread(text, start, step=0.4):
    out, t = [], start
    for token in text.split():
        out.append(_w(token, round(t, 2), round(t + step - 0.05, 2)))
        t += step
    return out


def _line(text, start, end, segment_id, words=None):
    row = {"text": text, "start": start, "end": end, "segment_id": segment_id}
    if words is not None:
        row["words"] = words
    return row


def _evidence(whisperx=(), witness=(), transformation="replicate_raw"):
    hyps = []
    if whisperx:
        hyps.append({"family": "replicate/victor-upmeet/whisperx:abc", "kind": "segments",
                     "transformation": transformation, "role": "candidate",
                     "events": [{"start": whisperx[0]["start"], "end": whisperx[-1]["end"],
                                 "text": " ".join(w["word"] for w in whisperx), "words": list(whisperx)}]})
    if witness:
        hyps.append({"family": "openai/whisper-1", "kind": "word_stream",
                     "transformation": "live_independent_verify_raw", "role": "candidate",
                     "events": [{k: w[k] for k in ("word", "start", "end")} for w in witness]})
    return {"hypotheses_by_family": hyps}


TEXT = "El tiempo corría y la verdad cabalgaba"


def _stretched_line():
    # CTC: "El" absorbe 1,2 s de instrumental; el resto coincide con WhisperX.
    ctc = [_w("El", 82.30, 83.50, 0.21)] + _spread("tiempo corría y la verdad cabalgaba", 83.56)
    return _line(TEXT, 82.30, 86.20, "b", ctc)


def _whisperx_for_line(onset=83.30):
    return [_w("El", onset, onset + 0.24)] + _spread("tiempo corría y la verdad cabalgaba", 83.56)


def test_stretched_first_word_with_witness_is_required():
    screen = [_line("Antes de todo", 78.0, 80.0, "a"), _stretched_line(),
              _line("Ella me dio un beso", 88.0, 90.0, "c")]
    witness = _spread(TEXT, 83.20)
    [finding] = ltr.find_timing_issues(screen, _evidence(_whisperx_for_line(), witness))
    assert finding["kind"] == "starts_before_voice"
    assert finding["line"] == 1 and finding["field"] == "start"
    assert finding["required"] is True
    assert finding["voice"] == pytest.approx(83.30)
    assert finding["proposed"] == pytest.approx(83.20)
    assert set(finding["sources"]) == {"ctc", "whisperx", "witness"}


def test_stretched_word_without_witness_is_only_a_suggestion_when_long():
    screen = [_stretched_line()]
    # 1,0 s sin voz con la palabra estirada: sugerencia.
    [finding] = ltr.find_timing_issues(screen, _evidence(_whisperx_for_line(83.30)))
    assert finding["required"] is False
    # 0,7 s: sin el testigo no alcanza (en las aceptadas había 3 veces más
    # avisos así que correcciones).
    assert ltr.find_timing_issues(screen, _evidence(_whisperx_for_line(83.00))) == []


def test_long_silence_on_screen_is_reported_as_no_voice():
    ctc = [_w("Mucho", 125.07, 127.92, 0.31)] + _spread("para ti soy un tonto", 128.0)
    screen = [_line("Mucho para ti, soy un tonto", 124.99, 130.0, "a", ctc)]
    whisperx = [_w("Mucho", 127.66, 127.90)] + _spread("para ti soy un tonto", 128.0)
    witness = _spread("Mucho para ti soy un tonto", 127.0)
    [finding] = ltr.find_timing_issues(screen, _evidence(whisperx, witness))
    assert finding["kind"] == "no_voice"
    assert finding["proposed"] == pytest.approx(127.56)
    assert ltr.describe(finding)["title"] == "Línea en pantalla sin voz"


def test_aligner_that_agrees_with_the_line_contradicts_whisperx():
    ctc = _spread(TEXT, 82.30)  # el alineador dice que la voz empieza con la línea
    screen = [_line(TEXT, 82.30, 86.20, "b", ctc)]
    assert ltr.find_timing_issues(screen, _evidence(_whisperx_for_line(), _spread(TEXT, 83.2))) == []


def test_never_moves_the_start_over_the_previous_line():
    screen = [_line("Antes de todo", 78.0, 83.0, "a"), _stretched_line()]
    [finding] = ltr.find_timing_issues(screen, _evidence(_whisperx_for_line(), _spread(TEXT, 83.2)))
    assert finding["proposed"] == pytest.approx(83.20)
    screen[0]["end"] = 83.40  # la anterior termina después del comienzo medido
    [finding] = ltr.find_timing_issues(screen, _evidence(_whisperx_for_line(), _spread(TEXT, 83.2)))
    assert finding["proposed"] == pytest.approx(83.45)


def test_line_that_leaves_before_the_last_word_ends():
    text = "Ven a morder conmigo en doce gritos"
    ctc = _spread("Ven a morder conmigo en doce", 67.16) + [_w("gritos", 71.00, 72.84, 0.2)]
    screen = [_line(text, 67.08, 70.92, "a", ctc), _line("Los labios de un dolor", 73.42, 77.98, "b")]
    whisperx = _spread("Ven a morder conmigo en doce", 67.20) + [_w("gritos", 71.04, 72.86)]
    witness = _spread("Ven a morder conmigo en doce", 67.5) + [_w("gritos", 71.06, 72.90)]
    [finding] = ltr.find_timing_issues(screen, _evidence(whisperx, witness))
    assert finding["kind"] == "ends_before_voice" and finding["field"] == "end"
    assert finding["required"] is True
    assert finding["proposed"] == pytest.approx(73.01)
    # Con la siguiente pegada no invade: se acota a 0,05 s antes.
    screen[1]["start"] = 72.20
    [finding] = ltr.find_timing_issues(screen, _evidence(whisperx, witness))
    assert finding["proposed"] == pytest.approx(72.15)
    # Sin lugar para alargar, no hay aviso.
    screen[1]["start"] = 71.10
    assert ltr.find_timing_issues(screen, _evidence(whisperx, witness)) == []


def test_line_end_needs_the_witness():
    text = "Ven a morder conmigo en doce gritos"
    screen = [_line(text, 67.08, 70.92, "a"), _line("Otra línea más", 76.0, 78.0, "b")]
    whisperx = _spread("Ven a morder conmigo en doce", 67.20) + [_w("gritos", 71.04, 72.86)]
    assert ltr.find_timing_issues(screen, _evidence(whisperx)) == []


def test_line_that_enters_late():
    text = "Que se abre cualquier día y"
    ctc = _spread(text, 83.38)
    screen = [_line("Es la flor de mis heridas", 79.0, 83.0, "a"),
              _line(text, 84.27, 88.16, "b", ctc)]
    whisperx = _spread(text, 83.40)
    witness = _spread(text, 83.30)
    [finding] = ltr.find_timing_issues(screen, _evidence(whisperx, witness))
    assert finding["kind"] == "starts_after_voice"
    assert finding["required"] is True
    assert finding["proposed"] == pytest.approx(83.28)


def test_single_word_lines_are_not_located_by_text():
    ctc = [_w("Abrázame", 50.50, 53.26)]
    screen = [_line("Abrázame", 50.50, 54.07, "a", ctc)]
    whisperx = [_w("Abrázame", 51.85, 53.20)]
    assert ltr.find_timing_issues(screen, _evidence(whisperx, [_w("Abrázame", 52.0, 53.0)])) == []


def test_a_repeated_chorus_matches_the_nearest_repetition():
    # El mismo verso 3 s antes: emparejar por texto solo tomaba esa
    # repetición y la línea parecía entrar tarde.
    text = "No es tu sombra"
    screen = [_line(text, 10.0, 11.8, "a", _spread(text, 10.05)),
              _line(text, 13.0, 14.8, "b", _spread(text, 13.05))]
    stream = _spread(text, 10.05) + _spread(text, 13.05)
    assert ltr.find_timing_issues(screen, _evidence(stream, stream)) == []


def test_edited_text_drops_the_stale_aligner_words():
    ctc = [_w("Hace", 57.0, 58.9, 0.2)] + _spread("un año te eras", 59.0)
    screen = [_line("Que hace un año atrás", 57.0, 61.0, "a", ctc)]
    assert ltr._ctc_words(screen[0]) == []


def test_needs_whole_song_whisperx():
    screen = [_stretched_line()]
    windowed = _evidence(_whisperx_for_line(), _spread(TEXT, 83.2),
                         transformation="replicate_raw:offset=80.000;end=90.000;v1")
    assert ltr.find_timing_issues(screen, windowed) == []
    assert ltr.find_timing_issues(screen, None) == []


def test_mode_defaults_to_observe(monkeypatch):
    monkeypatch.delenv("LYRIC_REVIEW_TIMING_MODE", raising=False)
    assert ltr.mode() == "observe"
    monkeypatch.setenv("LYRIC_REVIEW_TIMING_MODE", "ENFORCE")
    assert ltr.mode() == "enforce"
    monkeypatch.setenv("LYRIC_REVIEW_TIMING_MODE", "off")
    assert ltr.mode() == "off"
    monkeypatch.setenv("LYRIC_REVIEW_TIMING_MODE", "bogus")
    assert ltr.mode() == "observe"


def _review_inputs():
    screen = [_line("Antes de todo", 78.0, 80.0, "a"), _stretched_line(),
              _line("Ella me dio un beso", 88.0, 90.0, "c")]
    return screen, _evidence(_whisperx_for_line(), _spread(TEXT, 83.20))


def _timing_items(review):
    return [i for i in review["items"] if i["group"] == "timing"]


def test_quick_review_shows_a_suggestion_in_observe(monkeypatch):
    monkeypatch.delenv("LYRIC_REVIEW_TIMING_MODE", raising=False)
    screen, evidence = _review_inputs()
    [item] = _timing_items(build_review(screen, original_segments=screen, machine_evidence=evidence))
    assert item["kind"] == "timing_early_start"
    assert item["title"] == "Entra antes de la voz"
    assert item["required"] is False
    [occ] = item["occurrences"]
    assert occ["fix"] == {"type": "timing", "start": 83.2, "expect_start": 82.3}
    assert occ["line_segment_id"] == "b"
    # El texto no cambia: el arreglo sólo mueve el inicio.
    assert occ["before"] == occ["after"] == TEXT
    assert "1:23,3" in item["why"] and item["action"] == "Mover el inicio a 1:23,2"


def test_quick_review_blocks_only_in_enforce(monkeypatch):
    screen, evidence = _review_inputs()
    monkeypatch.setenv("LYRIC_REVIEW_TIMING_MODE", "enforce")
    [item] = _timing_items(build_review(screen, original_segments=screen, machine_evidence=evidence))
    assert item["required"] is True
    monkeypatch.setenv("LYRIC_REVIEW_TIMING_MODE", "off")
    assert _timing_items(build_review(screen, original_segments=screen, machine_evidence=evidence)) == []


def test_dismissed_timing_point_stays_dismissed(monkeypatch):
    monkeypatch.setenv("LYRIC_REVIEW_TIMING_MODE", "enforce")
    screen, evidence = _review_inputs()
    [item] = _timing_items(build_review(screen, original_segments=screen, machine_evidence=evidence))
    screen[1]["qa_dismissed"] = item["keys"]
    review = build_review(screen, original_segments=screen, machine_evidence=evidence)
    assert _timing_items(review) == []
    assert review["required_count"] == 0
