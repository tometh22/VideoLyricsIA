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


def test_collapse_caps_long_adlib_runs():
    """Un run de 'uh' largo (fragmentado por whisper) se parte en bloques
    de <= MAX_ADLIB_LINE_S en vez de un subtítulo gigante."""
    # 12 líneas de uh de 3.2s = ~38s → debe partirse en varios bloques
    segs = [{"text": "Uh, uh, uh,", "start": round(70 + i * 3.2, 1),
             "end": round(70 + (i + 1) * 3.2, 1)} for i in range(12)]
    out = ac.filter_and_collapse(segs, lambda a, b: "")
    assert all(s["end"] - s["start"] <= ac.MAX_ADLIB_LINE_S + 3.3 for s in out)
    assert len(out) >= 4                     # no un único bloque de 38s
    assert all(ac.is_adlib_text(s["text"]) for s in out)


def test_collapse_preserves_adlib_text():
    """El colapso conserva la vocalización (no la reescribe a 'Uh…')."""
    segs = [{"text": "Na na na", "start": 0, "end": 2},
            {"text": "Na na na", "start": 2, "end": 4}]
    out = ac.filter_and_collapse(segs, lambda a, b: "")
    assert len(out) == 1 and "na na na" in out[0]["text"].lower()


# ── verificación de cola (El Riesgo, 05/07: letra de otra versión) ──────────
# fixtures_adlib_riesgo.json = datos REALES del job 828a33641b42: las últimas
# 16 líneas (7 legítimas en zona oída + 9 en la cola muda de 76s), con
# `heard` = lo que whisper transcribió sobre el stem en cada ventana de cola
# (todas: "Subtítulos realizados por…", la alucinación de silencio). lrclib
# entregó la letra de OTRA edición cuyo outro cantado no existe en el audio.

_RIESGO = json.loads(
    (Path(__file__).parent / "fixtures_adlib_riesgo.json").read_text())
_RIESGO_TAIL_AFTER = 266.3   # fin de la última región de voz (VAD del stem real)


def _riesgo_transcriber(start, end):
    for r in _RIESGO:
        if abs(r["start"] - start) < 0.05:
            return r.get("heard", "")
    return ""


def test_tail_candidates_are_lines_in_mute_tail():
    segs = [{"start": r["start"], "end": r["end"], "text": r["text"]} for r in _RIESGO]
    idx = ac.tail_candidates(segs, _RIESGO_TAIL_AFTER)
    assert idx == set(range(7, 16))          # exactamente las 9 de la cola
    assert ac.tail_candidates(segs, None) == set()


def test_riesgo_tail_ghosts_all_dropped():
    """Las 9 líneas de la cola muda (incluido el 'Oh-oh' y el canto repetido
    'De la, de la mariposa' que la protección de coro blindaba) se van; las
    7 legítimas de la zona oída quedan intactas."""
    segs = [{"start": r["start"], "end": r["end"], "text": r["text"]} for r in _RIESGO]
    out = ac.filter_and_collapse(segs, _riesgo_transcriber,
                                 tail_after=_RIESGO_TAIL_AFTER)
    texts = " | ".join(s["text"] for s in out).lower()
    assert "mariposa" not in texts
    assert "este es el plan" not in texts
    assert "púa" not in texts
    assert "oh-oh" not in texts
    assert [s["start"] for s in out] == [r["start"] for r in _RIESGO[:7]]


def test_riesgo_without_tail_keeps_old_behavior():
    """tail_after=None = comportamiento pre-cola exacto: el canto repetido
    queda protegido como coro (el bug que motivó esto)."""
    segs = [{"start": r["start"], "end": r["end"], "text": r["text"]} for r in _RIESGO]
    out = ac.filter_and_collapse(segs, _riesgo_transcriber)
    texts = " | ".join(s["text"] for s in out).lower()
    assert "mariposa" in texts               # sobrevive (protegido) — sin cola no lo vemos


def test_chorus_protection_still_alive_in_heard_zone():
    """La protección de coro sigue viva DENTRO de la zona oída: una línea
    repetida antes de tail_after no se toca aunque el audio no la confirme."""
    segs = [
        {"start": 10.0, "end": 12.0, "text": "Uh, uh"},
        {"start": 13.0, "end": 15.0, "text": "Somos el coro"},
        {"start": 20.0, "end": 22.0, "text": "Somos el coro"},
    ]
    out = ac.filter_and_collapse(segs, lambda a, b: "", tail_after=100.0)
    assert sum(1 for s in out if s["text"] == "Somos el coro") == 2


def test_short_tail_is_noop():
    """Una canción normal (última línea ≈ último canto) con tail_after
    apenas anterior: nada cae en la cola → output idéntico."""
    segs = [{"start": float(i * 10), "end": i * 10 + 3.0, "text": f"línea {i}"}
            for i in range(5)]
    out = ac.filter_and_collapse(segs, lambda a, b: "", tail_after=41.0)
    assert out == segs
