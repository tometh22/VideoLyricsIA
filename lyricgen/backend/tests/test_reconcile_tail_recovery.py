"""Regresión: reconcile() no debe entregar una canción truncada cuando la
letra de referencia viene incompleta.

Caso testigo: "Rodando Por Ahí" (Intoxicados). lrclib devuelve 15 líneas que
terminan en ~186 s; el audio canta hasta 265,6 s. Antes de este fix el
resultado salía con 80 s de voz sin un solo cartel y `coverage` reportaba
15/15 = 100 % — el fallo se autocertificaba como éxito perfecto.
"""
import os

import pytest

import whisperx_reconcile as wr


REFERENCIA = """Pienso cuanto tiempo de vida voy llevando
Cuando me acuerdo de cosas y lugares que pase
Las estaciones que hice mientras iba jugando
La primera vez que en verdad me embrague
Mucha gente he conocido en barrios lejanos
Cuantas veces aburrido al diablo llame
De cuantas chicas buenas me habre enamorado
Cuanto vino tinto caliente tome
Todo eso me dice que estuve rodando
Rodando por ahi
No se cuando empece ni como comence
Rodando por ahi
Estuve rodando por ahi
Cuando los meses pasan y mi ropa no ha cambiado
Cuando me acuerdo de fotos que nunca saque"""

# Timings reales de esas 15 líneas (job b3a51559).
TIEMPOS = [
    (24.8, 28.2), (30.4, 34.0), (36.4, 40.2), (42.4, 45.9), (48.4, 52.6),
    (54.4, 57.8), (60.4, 64.2), (66.4, 69.8), (72.4, 76.9), (86.8, 90.3),
    (126.4, 130.9), (133.2, 136.8), (140.2, 143.7), (146.3, 152.0),
    (180.8, 186.0),
]

LINEAS = [l.strip() for l in REFERENCIA.splitlines() if l.strip()]


def _palabras_de(linea, ini, fin):
    ws = linea.split()
    paso = (fin - ini) / max(1, len(ws))
    return [
        {"word": w, "start": round(ini + k * paso, 2),
         "end": round(ini + k * paso + paso * 0.9, 2)}
        for k, w in enumerate(ws)
    ]


def _wx_cuerpo():
    """Segmentos whisperX de las 15 líneas de la referencia."""
    return [
        {"start": ini, "end": fin, "text": linea, "words": _palabras_de(linea, ini, fin)}
        for linea, (ini, fin) in zip(LINEAS, TIEMPOS)
    ]


def _wx_outro(desde=200.0, hasta=264.0):
    """Segmentos whisperX del outro cantado que la referencia no lista."""
    segs, t = [], desde
    while t < hasta:
        ws = []
        for w in "Estuve rodando por ahi".split():
            ws.append({"word": w, "start": round(t, 2), "end": round(t + 0.42, 2)})
            t += 0.5
        segs.append({"start": ws[0]["start"], "end": ws[-1]["end"],
                     "text": "Estuve rodando por ahi", "words": ws})
        t += 1.2
    return segs


def test_referencia_truncada_no_deja_el_outro_sin_letra():
    """El defecto original: 80 s de canto sin ningún cartel."""
    wx = _wx_cuerpo() + _wx_outro()
    ultima_voz = max(w["end"] for s in wx for w in s["words"])

    out = wr.reconcile(wx, REFERENCIA)

    assert out is not None, "reconcile no debería declinar en este caso"
    fin_letra = max(s["end"] for s in out)
    huecos = ultima_voz - fin_letra
    assert huecos < 15.0, (
        f"quedaron {huecos:.1f}s de voz cantada sin ninguna línea "
        f"(letra hasta {fin_letra:.1f}s, voz hasta {ultima_voz:.1f}s)"
    )


def test_la_cola_recuperada_trae_texto_real():
    wx = _wx_cuerpo() + _wx_outro()
    out = wr.reconcile(wx, REFERENCIA)
    cola = [s for s in out if s.get("tail_recovered")]
    assert cola, "no se recuperó ninguna línea de la cola"
    assert all((s.get("text") or "").strip() for s in cola), \
        "la cola recuperada no puede traer segmentos vacíos"
    assert all(s["end"] > s["start"] for s in cola)


def test_el_cuerpo_conserva_el_texto_curado_de_la_referencia():
    """La recuperación no debe ensuciar el cuerpo ya reconciliado."""
    wx = _wx_cuerpo() + _wx_outro()
    out = wr.reconcile(wx, REFERENCIA)
    cuerpo = [s for s in out if not s.get("tail_recovered")]
    assert len(cuerpo) == len(LINEAS)
    assert [s["text"] for s in cuerpo] == LINEAS


def test_segmentos_monotonos_y_sin_solape():
    wx = _wx_cuerpo() + _wx_outro()
    out = sorted(wr.reconcile(wx, REFERENCIA), key=lambda s: s["start"])
    for a, b in zip(out, out[1:]):
        assert a["end"] <= b["start"] + 1e-6, f"solape entre {a} y {b}"


# ── Casos negativos: el guardrail NO debe dispararse de más ────────────────

def test_no_dispara_cuando_la_referencia_cubre_toda_la_cancion():
    """Sin cola huérfana no debe agregarse nada (no-op verdadero)."""
    wx = _wx_cuerpo()
    out = wr.reconcile(wx, REFERENCIA)
    assert out is not None
    assert not [s for s in out if s.get("tail_recovered")]
    assert len(out) == len(LINEAS)


def test_no_dispara_por_una_cola_corta():
    """Un ad-lib suelto al final no es una referencia truncada."""
    wx = _wx_cuerpo() + _wx_outro(desde=188.0, hasta=193.0)
    out = wr.reconcile(wx, REFERENCIA)
    assert not [s for s in out if s.get("tail_recovered")], \
        "una cola de ~5s no debe gatillar la recuperación"


def test_kill_switch(monkeypatch):
    monkeypatch.setenv("RECONCILE_TAIL_RECOVERY_ENABLED", "0")
    wx = _wx_cuerpo() + _wx_outro()
    out = wr.reconcile(wx, REFERENCIA)
    assert not [s for s in out if s.get("tail_recovered")]


def test_fallback_arma_lineas_desde_las_palabras_si_no_hay_texto():
    """Si los segmentos de la cola no traen `text` (segmentación de otra
    etapa), las líneas se arman con las palabras huérfanas."""
    wx = _wx_cuerpo() + _wx_outro()
    for s in wx[len(LINEAS):]:
        s["text"] = ""
    out = wr.reconcile(wx, REFERENCIA)
    cola = [s for s in out if s.get("tail_recovered")]
    assert cola, "el fallback por palabras no recuperó nada"
    assert all((s.get("text") or "").strip() for s in cola)
    assert all(s["end"] - s["start"] <= wr._TAIL_LINE_MAX_S + 1.0 for s in cola), \
        "las líneas recuperadas no deben quedar interminables"
    ultima_voz = max(w["end"] for s in wx for w in s.get("words") or [])
    assert ultima_voz - max(s["end"] for s in out) < 15.0


def test_no_rompe_cuando_no_hay_word_stamps_en_la_cola():
    """whisperX a veces no devuelve words en algún segmento."""
    wx = _wx_cuerpo() + _wx_outro()
    for s in wx[len(LINEAS):]:
        s.pop("words", None)
    out = wr.reconcile(wx, REFERENCIA)
    assert out is not None
    assert len(out) >= len(LINEAS)
