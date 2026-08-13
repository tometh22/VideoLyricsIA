"""Regresión: reconcile() no debe dejar zonas con voz cantada sin letra
cuando la letra de referencia viene incompleta.

`wordstamps_to_segments` itera sobre las LÍNEAS DE LA REFERENCIA, así que
emite como mucho una línea por línea de referencia. Si la letra viene
incompleta (lrclib devuelve seguido registros con la duración correcta pero
sólo las estrofas, sin las repeticiones ni el outro), las palabras que
whisperX oyó y que ninguna línea reclamó quedan huérfanas y nunca se emiten.
Y `coverage` se mide contra la referencia, así que el fallo se autocertifica
como 100 %.

Caso testigo (job b3a51559, tema de rock argentino de 278,5 s): la referencia
traía 15 líneas y la canción tiene ~36. Contra la transcripción de Rotor
faltaban 13 líneas — 7 en la cola y 6 en huecos INTERNOS. Los timings de acá
son los reales de ese job; el TEXTO es un placeholder neutro a propósito: lo
que se ejercita es la estructura (qué zonas quedan sin línea), no las
palabras, así que no hace falta meter letra con copyright en el repo.
"""
import pytest

import whisperx_reconcile as wr


# Timings reales de las 15 líneas que trajo la referencia (job b3a51559).
TIEMPOS = [
    (24.8, 28.2), (30.4, 34.0), (36.4, 40.2), (42.4, 45.9), (48.4, 52.6),
    (54.4, 57.8), (60.4, 64.2), (66.4, 69.8), (72.4, 76.9), (86.8, 90.3),
    (126.4, 130.9), (133.2, 136.8), (140.2, 143.7), (146.3, 152.0),
    (180.8, 186.0),
]

# Texto neutro, una "línea" por entrada: palabras distintas por línea para que
# el matcher las ancle sin ambigüedad.
LINEAS = [f"linea{i} alfa{i} beta{i} gamma{i}" for i in range(len(TIEMPOS))]
REFERENCIA = "\n".join(LINEAS)


def _seg(texto, ini, fin):
    ws = texto.split()
    paso = (fin - ini) / max(1, len(ws))
    words = [
        {"word": w, "start": round(ini + k * paso, 2),
         "end": round(ini + k * paso + paso * 0.9, 2)}
        for k, w in enumerate(ws)
    ]
    return {"start": ini, "end": fin, "text": texto, "words": words}


def _wx_cuerpo():
    """Segmentos whisperX de las 15 líneas que la referencia sí lista."""
    return [_seg(l, ini, fin) for l, (ini, fin) in zip(LINEAS, TIEMPOS)]


def _wx_bloque(desde, hasta, etiqueta, largo=4.5, paso=6.0):
    """Voz cantada que la referencia NO lista (repeticiones / outro).

    Cadencia tomada del caso real: líneas de ~4,5 s separadas por ~1,5 s,
    igual que las del outro en la transcripción de Rotor."""
    segs, t = [], desde
    i = 0
    while t + largo <= hasta:
        segs.append(_seg(f"{etiqueta}{i} alfa{i} beta{i} gamma{i}",
                         round(t, 2), round(t + largo, 2)))
        t += paso
        i += 1
    return segs


def _voz_de(segs):
    return max(w["end"] for s in segs for w in s.get("words") or [])


# ── El defecto ────────────────────────────────────────────────────────────

def test_referencia_incompleta_no_deja_el_outro_sin_letra():
    wx = _wx_cuerpo() + _wx_bloque(200.0, 230.0, "outro")
    out = wr.reconcile(wx, REFERENCIA)
    assert out is not None
    hueco = _voz_de(wx) - max(s["end"] for s in out)
    assert hueco < 10.0, f"quedaron {hueco:.1f}s de voz cantada sin ninguna línea"


def test_recupera_huecos_INTERNOS_no_solo_la_cola():
    """Un fix que sólo mire la cola deja la mitad del problema sin resolver:
    en el caso testigo, 6 de las 13 líneas faltantes eran internas."""
    cuerpo = _wx_cuerpo()
    interno = _wx_bloque(155.0, 179.0, "medio")
    wx = cuerpo[:14] + interno + cuerpo[14:] + _wx_bloque(200.0, 230.0, "outro")
    out = wr.reconcile(wx, REFERENCIA)

    rec = [s for s in out if s.get("gap_recovered")]
    assert [s for s in rec if s["end"] <= 180.0], "no se recuperó el hueco INTERNO"
    assert [s for s in rec if s["start"] >= 190.0], "no se recuperó la cola"

    todas = sorted(out, key=lambda s: s["start"])
    for s in interno + _wx_bloque(200.0, 230.0, "outro"):
        mid = (s["start"] + s["end"]) / 2
        assert any(x["start"] - 0.6 <= mid <= x["end"] + 0.6 for x in todas), \
            f"quedó sin letra la voz de {s['start']:.1f}-{s['end']:.1f}s"


def test_lo_recuperado_trae_texto_real():
    wx = _wx_cuerpo() + _wx_bloque(200.0, 230.0, "outro")
    out = wr.reconcile(wx, REFERENCIA)
    rec = [s for s in out if s.get("gap_recovered")]
    assert rec
    assert all((s.get("text") or "").strip() for s in rec), \
        "lo recuperado no puede traer segmentos vacíos"
    assert all(s["end"] > s["start"] for s in rec)


def test_fallback_arma_lineas_desde_las_palabras_si_no_hay_texto():
    """Si los segmentos de la zona no traen `text`, las líneas se arman con
    las palabras huérfanas."""
    wx = _wx_cuerpo() + _wx_bloque(200.0, 230.0, "outro")
    for s in wx[len(LINEAS):]:
        s["text"] = ""
    out = wr.reconcile(wx, REFERENCIA)
    rec = [s for s in out if s.get("gap_recovered")]
    assert rec, "el fallback por palabras no recuperó nada"
    assert all((s.get("text") or "").strip() for s in rec)
    assert all(s["end"] - s["start"] <= wr._TAIL_LINE_MAX_S + 1.0 for s in rec), \
        "las líneas recuperadas no deben quedar interminables"


# ── Que no rompa lo que ya andaba ─────────────────────────────────────────

def test_el_cuerpo_conserva_el_texto_curado_de_la_referencia():
    wx = _wx_cuerpo() + _wx_bloque(200.0, 230.0, "outro")
    out = wr.reconcile(wx, REFERENCIA)
    cuerpo = [s for s in out if not s.get("gap_recovered")]
    assert len(cuerpo) == len(LINEAS)
    assert [s["text"] for s in cuerpo] == LINEAS


def test_segmentos_monotonos_y_sin_solape():
    wx = _wx_cuerpo() + _wx_bloque(200.0, 230.0, "outro")
    out = sorted(wr.reconcile(wx, REFERENCIA), key=lambda s: s["start"])
    for a, b in zip(out, out[1:]):
        assert a["end"] <= b["start"] + 1e-6, f"solape entre {a} y {b}"


def test_no_rompe_cuando_no_hay_word_stamps_en_la_zona():
    wx = _wx_cuerpo() + _wx_bloque(200.0, 230.0, "outro")
    for s in wx[len(LINEAS):]:
        s.pop("words", None)
    out = wr.reconcile(wx, REFERENCIA)
    assert out is not None
    assert len(out) >= len(LINEAS)


# ── Casos negativos: el guardrail NO debe dispararse de más ───────────────

def test_no_dispara_cuando_la_referencia_cubre_toda_la_cancion():
    """No-op verdadero: sin palabras huérfanas no se agrega nada."""
    out = wr.reconcile(_wx_cuerpo(), REFERENCIA)
    assert out is not None
    assert not [s for s in out if s.get("gap_recovered")]
    assert len(out) == len(LINEAS)


def test_no_dispara_sobre_pasajes_instrumentales():
    """El gatillo son las PALABRAS, no el tiempo vacío: donde sólo hay música
    (cero word-stamps) no se inventa ninguna línea. Es lo que evita pintar
    letra sobre un solo instrumental o sobre el fade-out."""
    out = wr.reconcile(_wx_cuerpo(), REFERENCIA)
    assert not [s for s in out if s.get("gap_recovered")]


def test_no_dispara_por_un_resto_minimo():
    """Dos palabras sueltas no son una referencia incompleta."""
    suelto = [{"start": 188.0, "end": 188.4, "word": "oh"},
              {"start": 188.6, "end": 189.0, "word": "oh"}]
    wx = _wx_cuerpo() + [{"start": 188.0, "end": 189.0, "text": "oh oh",
                          "words": suelto}]
    out = wr.reconcile(wx, REFERENCIA)
    assert not [s for s in out if s.get("gap_recovered")], \
        "un resto de <3s / <3 palabras no debe gatillar la recuperación"


def test_kill_switch(monkeypatch):
    monkeypatch.setenv("RECONCILE_GAP_RECOVERY_ENABLED", "0")
    wx = _wx_cuerpo() + _wx_bloque(200.0, 230.0, "outro")
    out = wr.reconcile(wx, REFERENCIA)
    assert out is not None
    assert not [s for s in out if s.get("gap_recovered")]


# ── Gate contra el AUDIO (APAGADO por default) ────────────────────────────

def test_el_gate_viene_apagado_por_default():
    """Decisión explícita: la métrica se mide y se loguea, pero NO gatea
    hasta tener datos reales para calibrar el piso. Ver
    `test_la_metrica_subreporta_si_la_tokenizacion_difiere` para el motivo."""
    assert wr._min_audio_coverage() == 0.0
    wx = (_wx_cuerpo()[:14] + _wx_bloque(155.0, 179.0, "medio")
          + _wx_cuerpo()[14:] + _wx_bloque(200.0, 264.0, "outro"))
    assert wr.reconcile(wx, REFERENCIA) is not None, \
        "con el gate apagado no se puede declinar por cobertura"


def test_el_gate_se_puede_encender_por_env(monkeypatch):
    """Sub-cobertura severa CON el gate encendido: declina y el caller se
    queda con el ASR, que sí oyó el audio."""
    monkeypatch.setenv("RECONCILE_MIN_AUDIO_COVERAGE", "0.55")
    wx = (_wx_cuerpo()[:14] + _wx_bloque(155.0, 179.0, "medio")
          + _wx_cuerpo()[14:] + _wx_bloque(200.0, 264.0, "outro"))
    assert wr.reconcile(wx, REFERENCIA) is None


def test_encendido_no_castiga_una_referencia_sana(monkeypatch):
    monkeypatch.setenv("RECONCILE_MIN_AUDIO_COVERAGE", "0.55")
    assert wr.reconcile(_wx_cuerpo(), REFERENCIA) is not None


def test_la_metrica_subreporta_si_la_tokenizacion_difiere():
    """POR QUÉ EL GATE VIENE APAGADO. Cuando la letra canónica usa una
    palabra compuesta donde el ASR oyó varias, el segmento emitido cubre
    sólo una de ellas y el resto cuenta como 'canto sin letra' — aunque la
    línea esté perfectamente puesta.

    Es el caso Legalícenla del corpus (`test_audio_as_truth_corpus.py`): un
    resultado CORRECTO mide 54 %. Un piso calibrado sobre una sola canción
    (39 %) lo tiraba del lado equivocado. Este test fija el conocimiento:
    la métrica sirve para observar, todavía no para decidir sola."""
    words = [{"word": w, "start": s, "end": e} for w, s, e in [
        ("Le", 17.0, 17.5), ("realizan", 17.5, 18.2), ("la", 18.2, 18.6),
    ]]
    # La línea canónica es UNA palabra: el segmento emitido cubre sólo la
    # primera de las tres que oyó el ASR.
    emitido = [{"start": 17.0, "end": 17.5, "text": "Legalícenla"}]
    cov = wr._audio_coverage(emitido, words)
    assert cov < 0.6, f"esperaba sub-reporte, dio {cov:.2f}"


def test_audio_coverage_baja_cuando_se_pierde_canto():
    """La métrica sólo puede bajar si se pierde canto — al revés que
    `coverage`, que SUBE cuando la referencia viene recortada."""
    cuerpo = _wx_cuerpo()
    words = wr._flatten_words(cuerpo + _wx_bloque(200.0, 264.0, "outro"))
    solo_cuerpo = [{"start": i, "end": f} for _, (i, f) in zip(LINEAS, TIEMPOS)]
    assert wr._audio_coverage(solo_cuerpo, wr._flatten_words(cuerpo)) == 1.0
    assert wr._audio_coverage(solo_cuerpo, words) < 0.75


