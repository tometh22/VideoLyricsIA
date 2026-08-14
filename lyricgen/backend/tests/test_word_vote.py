"""Voto por palabra: audio corrige a la referencia, referencia aporta
ortografía. Reglas validadas contra el job real b0c32e10 — el prototipo
corrigió exactamente las 3 palabras objetivo (canciones, embriagué, y el
"Cuando…mi" de la línea 1 que la deja idéntica a Rotor) sin tocar nada más.
"""
import pytest

import word_vote as wv


def _w(texto, ini, paso=0.5):
    out, t = [], ini
    for tok in texto.split():
        out.append({"word": tok, "start": round(t, 2), "end": round(t + 0.4, 2)})
        t += paso
    return out


def _seg(texto, ini, fin):
    return {"start": ini, "end": fin, "text": texto}


def _relleno(ini=100.0):
    """Testigo de relleno para superar el mínimo de 8 palabras."""
    return _w("relleno uno dos tres cuatro cinco seis siete", ini)


# ── sustituciones ancladas ────────────────────────────────────────────────

def test_sustitucion_anclada_gana_el_audio():
    """'las [estaciones] que' vs testigo 'las [canciones] que' → audio."""
    seg = _seg("Las estaciones que hice mientras iba jugando", 36.0, 40.0)
    testigo = _w("las canciones que hice mientras iba jugando", 36.0) + _relleno()
    out, stats = wv.vote([seg], testigo)
    assert out[0]["text"] == "Las canciones que hice mientras iba jugando"
    assert stats["substitutions"] == 1
    assert out[0].get("word_voted") is True
    assert not out[0].get("review")          # sustituir no exige revisión


def test_transferencia_de_acento():
    """ref 'embragué' (acentuada) + testigo 'embriague' (sin acento) →
    'embriagué': palabra del audio, ortografía de la referencia."""
    seg = _seg("La primera vez que en verdad me embragué", 42.0, 46.0)
    testigo = _w("la primera vez que en verdad me embriague", 42.0) + _relleno()
    out, _ = wv.vote([seg], testigo)
    assert out[0]["text"].endswith("me embriagué")


def test_sin_anclas_no_se_sustituye():
    """Si los vecinos también difieren, no hay evidencia local → no tocar."""
    seg = _seg("Las estaciones que hice", 36.0, 40.0)
    testigo = _w("unas canciones y dijo", 36.0) + _relleno()
    out, stats = wv.vote([seg], testigo)
    assert out[0]["text"] == "Las estaciones que hice"
    assert stats["substitutions"] == 0


def test_ortografia_de_la_referencia_se_preserva():
    """Testigo dice 'tome' (sin acento) pero normalizado es IGUAL a 'tomé'
    → no se toca: la referencia conserva su ortografía curada."""
    seg = _seg("¿Cuánto vino tinto caliente tomé?", 66.0, 70.0)
    testigo = _w("cuanto vino tinto caliente tome", 66.0) + _relleno()
    out, stats = wv.vote([seg], testigo)
    assert out[0]["text"] == "¿Cuánto vino tinto caliente tomé?"
    assert stats["lines_changed"] == 0


# ── inserciones ancladas ──────────────────────────────────────────────────

def test_insercion_inicial_y_media_caso_linea_1():
    """El caso real completo: '+Cuando' al inicio (con ajuste de mayúsculas)
    y '+mi' en el medio → línea idéntica a Rotor. Marca review."""
    seg = _seg("Pienso cuánto tiempo de vida voy llevando", 25.0, 28.2)
    testigo = _w("Cuando pienso cuánto tiempo de mi vida voy llevando", 24.6)
    out, stats = wv.vote([seg], testigo)
    assert out[0]["text"] == "Cuando pienso cuánto tiempo de mi vida voy llevando"
    assert stats["insertions"] == 2
    assert out[0].get("review") is True      # contenido nuevo → operador


def test_guard_antiduplicacion_de_bordes():
    """Una palabra en el borde entre dos carteles cae en la ventana de ambos:
    si pertenece al cartel vecino NO se inserta acá (se pintaría dos veces).
    Detectado en el prototipo sobre el outro real."""
    seg_a = _seg("estuve rodando por ahi", 200.0, 203.4)
    seg_b = _seg("por ahi estuve", 203.5, 206.0)
    # El testigo oye un 'rodando' final en 202.9-203.3: DENTRO del cartel A,
    # pero también dentro de la ventana con pad del cartel B (203.5-0.4).
    testigo = (_w("estuve rodando por ahi", 200.0)
               + [{"word": "rodando", "start": 202.9, "end": 203.3}]
               + _w("por ahi estuve", 203.9))
    out, stats = wv.vote([seg_a, seg_b], testigo)
    assert out[1]["text"] == "por ahi estuve", \
        "no debe insertarse la palabra que pertenece al cartel vecino"


def test_insercion_larga_no_se_hace():
    """>2 tokens = otra línea, no una palabra comida."""
    seg = _seg("por ahi estuve", 200.0, 206.0)
    testigo = _w("por una larga avenida de recuerdos rotos ahi estuve", 200.0)
    out, stats = wv.vote([seg], testigo)
    assert stats["insertions"] == 0


# ── guardas generales ─────────────────────────────────────────────────────

def test_sin_testigo_declina():
    seg = _seg("Las estaciones que hice", 36.0, 40.0)
    out, stats = wv.vote([seg], [])
    assert out == [seg]
    assert "sin_testigo" in stats["declined"]


def test_lineas_sin_cambios_pasan_identicas():
    seg = _seg("Mucha gente he conocido en barrios lejanos", 48.0, 52.6)
    testigo = _w("mucha gente he conocido en barrios lejanos", 48.0) + _relleno()
    out, stats = wv.vote([seg], testigo)
    assert out[0] is seg                     # mismo objeto: no-op verdadero
    assert stats["lines_changed"] == 0


def test_nunca_levanta_con_basura():
    out, stats = wv.vote([{"start": "x"}, None, 42],       # type: ignore
                         _w("a b c d e f g h", 0.0))
    assert isinstance(out, list) and isinstance(stats, dict)


def test_kill_switch(monkeypatch):
    monkeypatch.delenv("WORD_VOTE_ENABLED", raising=False)
    assert wv.is_enabled() is False
    monkeypatch.setenv("WORD_VOTE_ENABLED", "1")
    assert wv.is_enabled() is True
