"""Palabras que se escuchan y no están en la letra (reclamos UMG 29-09-2026)."""
from heard_words import (
    alert_key,
    find_missing_heard_words,
    machine_words,
    witness_words,
)


def _words(text, start, step=0.3, score=0.9):
    out = []
    t = start
    for token in text.split():
        out.append({"word": token, "start": round(t, 2), "end": round(t + step - 0.05, 2), "score": score})
        t += step
    return out


def _line(text, start, end, words=None, **extra):
    row = {"text": text, "start": start, "end": end, **extra}
    if words is not None:
        row["words"] = words
    return row


def _witness_evidence(words):
    return {"hypotheses_by_family": [{
        "role": "candidate", "family": "openai/whisper-1", "kind": "word_stream",
        "view": "bounded_audio_window", "transformation": "live_independent_verify_raw",
        "events": [{k: w[k] for k in ("word", "start", "end")} for w in words],
    }]}


# Al Pájaro (b56b9f36afb8): al resolver un pedido se pegó la cita del cliente
# y se perdió "dormite ya" al final de la línea.
PAJARO_MACHINE = [
    _line("Tu garantía de reloj se fundió, dormite ya", 43.5, 47.4,
          _words("Tu garantía de reloj se fundió, dormite ya", 43.6)),
    _line("Te lo pido, por favor", 48.7, 51.1, _words("Te lo pido, por favor", 48.7)),
]
PAJARO_WITNESS = _words("Tu garantía de reloj se fundió dormíte ya", 43.6) + \
    _words("Te lo pido por favor", 48.7)


def test_phrase_deleted_while_pasting_the_client_quote_is_flagged():
    final = [
        _line("Tu garantía de reloco se fundió", 43.5, 47.8),
        _line("Te lo pido, por favor", 48.7, 51.1),
    ]
    alerts = find_missing_heard_words(
        final, witness=PAJARO_WITNESS, machine=machine_words(PAJARO_MACHINE))
    assert [a["text"] for a in alerts] == ["dormite ya"]
    alert = alerts[0]
    assert alert["sources"] == "both"
    assert alert["action"] == "insert"
    assert alert["line_index"] == 0
    assert alert["insert_at_word"] == 6  # después de "fundió"


def test_substitution_is_not_a_missing_word():
    # reloj -> reloco es una corrección pedida por el cliente, no un faltante.
    final = [
        _line("Tu garantía de reloco se fundió, dormite ya", 43.5, 47.8),
        _line("Te lo pido, por favor", 48.7, 51.1),
    ]
    assert find_missing_heard_words(
        final, witness=PAJARO_WITNESS, machine=machine_words(PAJARO_MACHINE)) == []


def test_leading_word_goes_to_the_start_of_its_own_line():
    # ¿Por qué te tengo que olvidar? (bdb298bf2444): se borró el "Que" que
    # abría el verso; el testigo lo ubica unas décimas antes, dentro del
    # verso anterior, pero la máquina sabe a qué línea pertenecía.
    machine = [
        _line("Te recuerdo más", 54.5, 56.5, _words("Te recuerdo más", 54.6, step=0.5)),
        _line("Que hace un año te eras y siempre tú", 56.5, 61.4,
              _words("Que hace un año te eras y siempre tú", 56.6)),
    ]
    witness = _words("Te recuerdo más", 54.6, step=0.5) + [
        {"word": "Que", "start": 56.16, "end": 56.4},
    ] + _words("hace un año atrás y siempre tú", 56.84)
    final = [
        _line("Te recuerdo más", 54.5, 56.3),
        _line("Hace un año atrás y siempre tú", 56.5, 62.3),
    ]
    alerts = find_missing_heard_words(final, witness=witness, machine=machine_words(machine))
    assert [(a["text"], a["line_index"], a["insert_at_word"]) for a in alerts] == [("Que", 1, 0)]


def test_spelling_variants_and_joined_words_are_not_flagged():
    machine = [_line("¿Por qué te tengo que olvidar?", 10, 13, _words("¿Por qué te tengo que olvidar?", 10))]
    witness = _words("porque te tengo que olvidar", 10)
    final = [_line("¿POR QUÉ TE TENGO QUE OLVIDAR?", 10, 13)]
    assert find_missing_heard_words(final, witness=witness, machine=machine_words(machine)) == []


def test_dismissed_alert_stays_dismissed():
    final = [
        _line("Tu garantía de reloco se fundió", 43.5, 47.8),
        _line("Te lo pido, por favor", 48.7, 51.1),
    ]
    machine = machine_words(PAJARO_MACHINE)
    [alert] = find_missing_heard_words(final, witness=PAJARO_WITNESS, machine=machine)
    assert alert["key"] == alert_key("dormíte ya", 45.4)
    # El editor guarda la decisión "No se canta" en la línea.
    final[1]["qa_dismissed"] = [alert["key"]]
    assert find_missing_heard_words(final, witness=PAJARO_WITNESS, machine=machine) == []


CHORUS = "Borracho y agresivo nada que beber"


def test_witness_only_repeated_chorus_in_a_gap_becomes_a_new_line():
    # Borracho y Agresivo (396c71f0b66b): la máquina nunca transcribió la
    # última pasada del estribillo; el testigo sí, y es letra de la canción.
    final = [
        _line(CHORUS, 10.0, 12.0),
        _line("Te está poniendo nervioso otra vez", 75.9, 78.7),
    ]
    witness = _words(CHORUS, 10.0) + _words("Te está poniendo nervioso otra vez", 75.9, step=0.4) \
        + _words(CHORUS, 81.3)
    alerts = find_missing_heard_words(final, witness=witness, machine=[])
    assert len(alerts) == 1
    assert alerts[0]["sources"] == "witness"
    assert alerts[0]["action"] == "new_line"
    assert alerts[0]["new_line"]["start"] >= 78.7


def test_witness_only_text_that_is_not_lyric_is_ignored():
    final = [_line(CHORUS, 10.0, 12.0)]
    witness = _words(CHORUS, 10.0) + _words("Gracias a todos los que nos han apoyado", 40.0)
    assert find_missing_heard_words(final, witness=witness, machine=[]) == []


def test_whisper_loop_is_ignored():
    final = [_line("nadie me apura, sigo caminando", 10.0, 12.0)]
    witness = _words("nadie me apura sigo caminando", 10.0) + \
        _words("nadie me apura " * 6, 60.0)
    assert find_missing_heard_words(final, witness=witness, machine=[]) == []


def test_only_whole_song_witness_streams_are_used():
    evidence = _witness_evidence(_words("hola mundo", 5.0))
    evidence["hypotheses_by_family"].append({
        "role": "candidate", "family": "openai/whisper-1", "kind": "word_stream",
        "view": "bounded_audio_window", "transformation": "gap_rescue_raw",
        "events": [{"word": "relativo", "start": 0.1, "end": 0.4}],
    })
    assert [w["word"] for w in witness_words(evidence)] == ["hola", "mundo"]
