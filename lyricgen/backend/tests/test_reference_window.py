"""Letra oficial contra un audio FRAGMENTO (demos de 30 s, Shorts, Canvas).

Evaluación de 15 demos de producción (16-11-2026): lrclib tenía 13 de las ~22
correcciones de texto, pero la Revisión rápida (similitud global < 0,45) y la
atestación (cobertura 8–24 % sobre la canción entera) la descartaban siempre.
Recortando la letra al tramo que suena, el recall de texto sube de 58 % a 71 %
y la letra de otra canción (ventana 0,39) se sigue rechazando.

Letras sintéticas inventadas para el test: nada con derechos de autor.
"""
from types import SimpleNamespace

import pytest

import lyric_review
import lyric_review_sources
from lyric_review import build_review
from reference_attestation import assess_reference_attestation, reference_gate_action
from reference_window import WINDOW_MIN_SCORE, crop_reference, find_reference_window

VERSE_1 = [
    "Camino por la orilla del río plateado",
    "Cuento las piedras que dejó la tormenta",
    "El farol de la esquina se quedó dormido",
    "Y un perro sin nombre me sigue los pasos",
    "Guardo en el bolsillo una llave oxidada",
    "Que abre una puerta que ya no existe",
]
CHORUS = [
    "Ay lunita de papel no te vayas",
    "Quedate un rato más sobre el tejado",
    "Que la noche es larga y el café se enfría",
    "Ay lunita de papel no te vayas",
]
VERSE_2 = [
    "Mi abuela tejía bufandas de colores",
    "Para los gatos que dormían en el patio",
    "Cantaba tangos mientras hervía la sopa",
    "Y el reloj de pared marcaba otro siglo",
    "Hoy la casa tiene ventanas cerradas",
    "Pero el jazmín sigue trepando la reja",
]
BRIDGE = [
    "Si me preguntan dónde quedó mi infancia",
    "Digo que la perdí en un tren de madrugada",
    "Entre maletas viejas y boletos rotos",
    "Con un caramelo de menta en la mano",
]
SONG = VERSE_1 + CHORUS + VERSE_2 + CHORUS + BRIDGE + CHORUS + CHORUS
V2_START = len(VERSE_1) + len(CHORUS)

OTHER_SONG = [
    "Los trenes del norte llegan siempre tarde",
    "Con vagones llenos de harina y de sal",
    "Mi hermano maneja un camión colorado",
    "Que tose en las subidas de la cordillera",
    "Le pusimos de nombre Tormenta de Agosto",
    "Porque nunca frena cuando ve un zorro",
]

# Lo que la máquina oyó de la estrofa 2 en una demo de 30 s: tres palabras mal
# oídas ("tejía", "tangos", "jazmín").
HEARD_V2 = [
    "Mi abuela tenía bufandas de colores",
    "Para los gatos que dormían en el patio",
    "Cantaba tango mientras hervía la sopa",
    "Y el reloj de pared marcaba otro siglo",
    "Hoy la casa tiene ventanas cerradas",
    "Pero el jardín sigue trepando la reja",
]


def _segments(lines, start=0.0, step=5.0):
    return [
        {"text": text, "start": round(start + k * step, 2),
         "end": round(start + k * step + step - 0.4, 2), "segment_id": f"s{k}"}
        for k, text in enumerate(lines)
    ]


def _witness(lines, start=0.0, step=5.0):
    events = []
    for k, line in enumerate(lines):
        t = start + k * step
        for word in line.split():
            events.append({"word": word, "start": round(t, 2), "end": round(t + 0.3, 2)})
            t += 0.35
    return {"hypotheses_by_family": [{
        "role": "candidate", "family": "openai/whisper-1", "kind": "word_stream",
        "transformation": "live_independent_verify_raw", "events": events,
    }]}


# ---------------------------------------------------------------------------
# Ventana
# ---------------------------------------------------------------------------

def test_window_finds_a_fragment_in_the_middle_of_the_song():
    window = find_reference_window(SONG, " ".join(HEARD_V2))
    assert window is not None
    assert (window["start_line"], window["end_line"]) == (V2_START, V2_START + len(VERSE_2))
    assert window["score"] >= 0.9
    assert window["reference_lines"] == len(SONG)


def test_window_with_a_repeated_chorus_picks_the_matching_repetition():
    # Sólo el estribillo: cualquiera de sus 4 repeticiones es la misma letra;
    # gana la primera.
    chorus = find_reference_window(SONG, " ".join(CHORUS))
    assert chorus is not None
    assert (chorus["start_line"], chorus["end_line"]) == (len(VERSE_1), len(VERSE_1) + len(CHORUS))
    assert chorus["score"] == pytest.approx(1.0)
    # Puente + estribillo: sólo hay UNA repetición del estribillo después del
    # puente, y es la que se elige aunque el estribillo aparezca antes.
    bridge_start = SONG.index(BRIDGE[0])
    tail = find_reference_window(SONG, " ".join(BRIDGE[2:] + CHORUS[:2]))
    assert (tail["start_line"], tail["end_line"]) == (bridge_start + 2, bridge_start + 6)


def test_window_rejects_another_song():
    assert find_reference_window(SONG, " ".join(OTHER_SONG)) is None
    weak = find_reference_window(SONG, " ".join(OTHER_SONG), min_score=0.0)
    assert weak is not None and weak["score"] < WINDOW_MIN_SCORE


def test_window_needs_enough_machine_words_and_skips_blank_lines():
    assert find_reference_window(SONG, "Ay lunita de papel") is None
    with_blanks = VERSE_1 + ["", "   "] + VERSE_2
    window = find_reference_window(with_blanks, " ".join(HEARD_V2))
    assert (window["start_line"], window["end_line"]) == (len(VERSE_1) + 2, len(with_blanks))


def test_crop_reference_pads_one_line_each_side():
    text, window = crop_reference("\n".join(SONG), " ".join(HEARD_V2), pad_lines=1)
    assert text.splitlines() == SONG[V2_START - 1:V2_START + len(VERSE_2) + 1]
    assert (window["crop_start_line"], window["crop_end_line"]) == (V2_START - 1, V2_START + 7)
    assert crop_reference("\n".join(SONG), " ".join(OTHER_SONG)) is None


# ---------------------------------------------------------------------------
# Revisión rápida
# ---------------------------------------------------------------------------

def _official_fixes(review):
    return {
        (i["occurrences"][0]["fix"].get("find"), i["occurrences"][0]["fix"].get("replace")): i["required"]
        for i in review["items"] if "official" in i["sources"]
    }


def test_fragment_review_uses_the_official_lyrics_window(monkeypatch):
    segs = _segments(HEARD_V2)
    # El testigo oyó bien "tejía"; "tango" y "jardín" sólo los corrige la letra.
    evidence = _witness([VERSE_2[0]] + HEARD_V2[1:])
    official = "\n".join(SONG)

    # Antes: comparada contra la canción entera, la letra no pasaba el filtro.
    monkeypatch.setattr(lyric_review, "crop_reference", lambda *a, **k: None)
    before = build_review(segs, original_segments=segs, machine_evidence=evidence,
                          official_text=official)
    assert before["sources"]["similarity"]["official"] < lyric_review.EAR_MIN_SIMILARITY
    assert _official_fixes(before) == {}
    monkeypatch.undo()

    after = build_review(segs, original_segments=segs, machine_evidence=evidence,
                         official_text=official)
    window = after["sources"]["official_window"]
    assert (window["start_line"], window["end_line"]) == (V2_START, V2_START + len(VERSE_2))
    assert after["sources"]["similarity"]["official"] >= lyric_review.EAR_MIN_SIMILARITY
    assert _official_fixes(after) == {
        ("tenía", "tejía"): True,     # letra oficial + testigo: obligatorio
        ("tango", "tangos"): False,   # sólo la letra oficial: sugerencia
        ("jardín", "jazmín"): False,
    }


def test_fragment_review_rejects_the_lyrics_of_another_song():
    segs = _segments(OTHER_SONG)
    review = build_review(segs, original_segments=segs, official_text="\n".join(SONG))
    assert "official_window" not in review["sources"]
    assert _official_fixes(review) == {}


def test_fragment_edges_do_not_borrow_the_cut_line():
    # El audio empieza a mitad de la línea 2 ("Cuento" quedó afuera y la
    # máquina oyó "Lento"): la línea anterior de relleno no se propone.
    heard = ["Lento las piedras que dejó la tormenta"] + VERSE_1[2:] + CHORUS[:1]
    segs = _segments(heard)
    review = build_review(segs, original_segments=segs, official_text="\n".join(SONG))
    assert review["sources"]["official_window"]["crop_start_line"] == 0
    for item in review["items"]:
        fix = item["occurrences"][0]["fix"]
        assert "Camino" not in str(fix.get("replace") or fix.get("text") or "")


def test_full_song_review_is_unchanged(monkeypatch):
    heard = list(SONG)
    heard[V2_START] = HEARD_V2[0]
    segs = _segments(heard)

    def never(*_a, **_k):
        raise AssertionError("una canción entera no se recorta")

    monkeypatch.setattr(lyric_review, "crop_reference", never)
    review = build_review(segs, original_segments=segs, official_text="\n".join(SONG))
    assert "official_window" not in review["sources"]
    assert review["sources"]["similarity"]["official"] >= 0.85
    assert ("tenía", "tejía") in _official_fixes(review)


# ---------------------------------------------------------------------------
# Atestación del pipeline
# ---------------------------------------------------------------------------

def test_attestation_measures_a_short_fragment_on_its_window():
    asr = _segments(HEARD_V2)
    song_scope = assess_reference_attestation("\n".join(SONG), asr)  # sin duración
    assert song_scope["metrics"]["coverage_scope"] == "song"
    assert song_scope["text_status"] == "unsafe_without_witness"

    report = assess_reference_attestation("\n".join(SONG), asr, audio_duration_s=30.0)
    metrics = report["metrics"]
    assert metrics["coverage_scope"] == "window"
    assert metrics["reference_window"]["start_line"] == V2_START
    assert metrics["reference_token_coverage"] >= 0.8
    assert report["allow_vocabulary_reconciliation"] is True
    # La ventana nunca autoriza alinear la canción entera sobre el fragmento.
    assert report["allow_global_forced_alignment"] is False
    assert "reference_window_fragment" in report["reasons"]
    assert reference_gate_action(report, mode="enforce", is_live=False) == "audio_first"


def test_attestation_fragment_of_another_song_stays_unsafe():
    report = assess_reference_attestation(
        "\n".join(SONG), _segments(OTHER_SONG), audio_duration_s=30.0)
    assert report["metrics"]["coverage_scope"] == "song"
    assert report["text_status"] == "unsafe_without_witness"
    assert report["allow_vocabulary_reconciliation"] is False


def test_attestation_full_length_audio_is_not_windowed():
    # Mismo ASR parcial, pero el audio dura lo que la canción: es omisión, no
    # fragmento, y se mide contra la canción entera como siempre.
    asr = _segments(HEARD_V2, start=150.0)
    report = assess_reference_attestation("\n".join(SONG), asr, audio_duration_s=200.0)
    assert report["metrics"]["coverage_scope"] == "song"
    assert report["metrics"]["reference_window"] is None
    assert report["allow_vocabulary_reconciliation"] is False

    whole = assess_reference_attestation(
        "\n".join(SONG), _segments(SONG, step=6.0), audio_duration_s=200.0)
    assert whole["metrics"]["coverage_scope"] == "song"
    assert whole["allow_global_forced_alignment"] is True


# ---------------------------------------------------------------------------
# Campañas batch: letra oficial en la Revisión rápida detrás de una variable
# ---------------------------------------------------------------------------

class _Rows:
    """db falso: devuelve las filas en el orden en que se consultan."""

    def __init__(self, rows):
        self.rows = list(rows)

    def query(self, *_a, **_k):
        return self

    def filter(self, *_a, **_k):
        return self

    def first(self):
        return self.rows.pop(0) if self.rows else None


def _job(workload_class):
    return SimpleNamespace(job_id="j1", artist="Artista Inventado", song_title="Lunita de papel",
                           campaign_item_id=None, workload_class=workload_class, tenant_id="t")


def test_batch_official_lyrics_flag(monkeypatch):
    lrclib_row = SimpleNamespace(lyrics="\n".join(SONG))
    monkeypatch.delenv("LYRIC_REVIEW_OFFICIAL_IN_BATCH", raising=False)
    assert lyric_review_sources._external_lyrics_allowed(_job("single")) is True
    assert lyric_review_sources._external_lyrics_allowed(_job("batch")) is False
    # operador (nada) → letra de lrclib del editor
    assert lyric_review_sources.official_text(_Rows([None, lrclib_row]), _job("batch")) == ("", "")
    assert lyric_review_sources.official_text(_Rows([None, lrclib_row]), _job("single")) == (
        lrclib_row.lyrics, "lrclib")

    monkeypatch.setenv("LYRIC_REVIEW_OFFICIAL_IN_BATCH", "1")
    assert lyric_review_sources._external_lyrics_allowed(_job("batch")) is True
    assert lyric_review_sources.official_text(_Rows([None, lrclib_row]), _job("batch")) == (
        lrclib_row.lyrics, "lrclib")

    monkeypatch.setenv("LYRIC_REVIEW_OFFICIAL_IN_BATCH", "0")
    assert lyric_review_sources._external_lyrics_allowed(_job("batch")) is False


def test_batch_flag_keeps_the_background_fetch_gated(monkeypatch):
    started = []

    def fake_thread(*_a, **k):
        def start():
            started.append(k.get("name"))
            lyric_review_sources._fetch_slots.release()  # el hilo real lo libera al terminar
        return SimpleNamespace(start=start)

    monkeypatch.setattr(lyric_review_sources.threading, "Thread", fake_thread)
    monkeypatch.setenv("LYRIC_REVIEW_FETCH_OFFICIAL", "1")
    monkeypatch.delenv("LYRIC_REVIEW_OFFICIAL_IN_BATCH", raising=False)
    lyric_review_sources._fetch_attempted.clear()
    lyric_review_sources.schedule_official_fetch(_job("batch"))
    assert started == []
    monkeypatch.setenv("LYRIC_REVIEW_OFFICIAL_IN_BATCH", "1")
    lyric_review_sources.schedule_official_fetch(_job("batch"))
    assert started == ["lyric-review-official"]
    lyric_review_sources._fetch_attempted.clear()
    # El interruptor general de la búsqueda sigue mandando.
    monkeypatch.setenv("LYRIC_REVIEW_FETCH_OFFICIAL", "0")
    lyric_review_sources.schedule_official_fetch(_job("batch"))
    assert started == ["lyric-review-official"]
