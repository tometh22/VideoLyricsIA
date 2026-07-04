"""Filtro de fantasmas por consenso acústico (adlib_consensus.py).

El fixture fixtures_adlib_amanda.json son los datos REALES de la canción
que destapó el bug (Amanda Pujó — No Hay Santos, 04/07): cada línea con su
texto asignado y `heard` = lo que whisper transcribió sobre el stem de VOZ
en la ventana de esa línea. Verdad del operador: fantasmas en 70.2 y 85.4
(contenido incrustado en la sección de "uh"); todo lo demás es real.
"""
import json
from pathlib import Path

import adlib_consensus as ac

_FIX = json.loads((Path(__file__).parent / "fixtures_adlib_amanda.json").read_text())
_GHOST_STARTS = {70.2, 85.4}


def _transcribe_from_fixture(start, end):
    """transcribe_window inyectada: devuelve el `heard` capturado para la
    línea cuyo start coincide."""
    for r in _FIX:
        if abs(r["start"] - start) < 0.05:
            return r.get("heard", "")
    return ""


# ── piezas puras ─────────────────────────────────────────────────────────────

def test_is_adlib_text():
    assert ac.is_adlib_text("Uh, uh, uh, uh")
    assert ac.is_adlib_text("ah ah oh")
    assert not ac.is_adlib_text("Tus santos de papel")
    assert not ac.is_adlib_text("")


def test_phonetic_tolerates_asr_slips():
    # whisper oyó "Más que el miedo, tu dolor" — casi idéntico fonéticamente
    assert ac.phonetic("Más que el miedo, tu dolor", "Tomás del miedo tu don") > 0.6
    # el fantasma: whisper oyó basura de subtítulos (valor real del fixture)
    assert ac.phonetic("Subtítulos realizados por la comunidad de Amara.org",
                       "Tus santos de papel") < 0.35


def test_is_phantom_core():
    # audio confirma (whisper resbala pero suena parecido) → real
    assert not ac.is_phantom("Tomás del miedo tu don", "Más que el miedo, tu dolor", False)
    # audio vacío → fantasma
    assert ac.is_phantom("Tus santos de papel", "", False)
    # audio basura no-fonética (valor real del fixture) → fantasma
    assert ac.is_phantom("Tus santos de papel",
                         "Subtítulos realizados por la comunidad de Amara.org", False)
    # coro protegido: aunque el audio no confirme, se conserva
    assert not ac.is_phantom("¿Para qué?", "¡Gracias!", protected=True)


def test_find_candidates_are_lines_touching_adlibs():
    segs = [
        {"text": "Verso uno", "start": 0, "end": 2},        # 0: antes del uh → candidata
        {"text": "Uh, uh", "start": 2, "end": 4},           # 1: adlib
        {"text": "Fantasma", "start": 4, "end": 6},         # 2: después del uh → candidata
        {"text": "Verso dos", "start": 20, "end": 22},      # 3: lejos → NO
    ]
    assert ac.find_candidates(segs) == [0, 2]


def test_song_without_adlibs_is_noop():
    segs = [{"text": f"línea {i}", "start": i, "end": i + 1} for i in range(6)]
    out = ac.filter_and_collapse(segs, lambda a, b: "cualquier cosa")
    assert out == segs                      # cero cambios, cero llamadas efectivas


# ── el caso real completo, end-to-end de la función (fixture) ────────────────

def test_amanda_full_100pct():
    segs = [{"start": r["start"], "end": r["end"], "text": r["text"]} for r in _FIX]
    out = ac.filter_and_collapse(segs, _transcribe_from_fixture)
    starts_out = [round(s["start"], 1) for s in out]
    # los dos fantasmas fueron descartados
    for g in _GHOST_STARTS:
        assert not any(abs(g - x) < 0.2 for x in starts_out), f"fantasma {g} sobrevivió"
    # las líneas legítimas de contenido sobreviven (muestras clave, case-insensitive)
    texts = " | ".join(s["text"] for s in out).lower()
    for keep in ("tomás del miedo tu don", "dentro de tu piel se esconden los indicios",
                 "de que nada es perfecto", "frágil espejo de vos"):
        assert keep in texts, f"se perdió línea legítima: {keep!r}"
    # los ad-libs consecutivos se colapsaron en bloques "Uh…"
    assert any(s["text"].startswith("Uh") for s in out)


def test_transcribe_error_keeps_line():
    """Si la transcripción falla, la candidata se conserva (nunca borrar por
    las dudas)."""
    segs = [
        {"text": "Uh, uh", "start": 0, "end": 2},
        {"text": "línea dudosa", "start": 2, "end": 4},
        {"text": "Uh, uh", "start": 4, "end": 6},
    ]
    def boom(a, b):
        raise RuntimeError("whisper caído")
    out = ac.filter_and_collapse(segs, boom)
    assert any(s["text"] == "línea dudosa" for s in out)
