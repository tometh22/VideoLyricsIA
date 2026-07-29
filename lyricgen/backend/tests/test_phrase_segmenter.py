"""Re-segmentación DP: carteles de frase corta, cortando en la respiración.

Medido contra Rotor: nuestros carteles promediaban 11-38 palabras / ~10 s;
Rotor ~5,8 palabras / ~4,8 s. El DP validado en 5 canciones lleva todo al
rango 4,8-5,8 sin perder ni reordenar una sola palabra.
"""
import pytest

import phrase_segmenter as ps


def _words(texto, ini, paso=0.5, gaps=None):
    """gaps: {índice_de_palabra: silencio_extra_después}."""
    out, t = [], ini
    for i, w in enumerate(texto.split()):
        out.append({"word": w, "start": round(t, 2), "end": round(t + 0.4, 2)})
        t += paso + (gaps or {}).get(i, 0.0)
    return out


def _seg(texto, ini, fin=None, gaps=None, **extra):
    ws = _words(texto, ini, gaps=gaps)
    d = {"start": ini, "end": fin if fin is not None else ws[-1]["end"],
         "text": texto, "words": ws}
    d.update(extra)
    return d


PARED = ("cuando la tarde cae sobre el rio dorado y las luces "
         "de la costa se encienden una a una mientras el viento "
         "trae voces lejanas que nadie recuerda ya")   # 29 palabras


# ── el DP ─────────────────────────────────────────────────────────────────

def test_dp_parte_la_pared_en_frases_cortas():
    seg = _seg(PARED, 10.0, gaps={9: 1.3, 19: 1.3})
    out = ps.resegment([seg])
    assert len(out) >= 3
    for c in out:
        assert len(c["text"].split()) <= 11
        assert c["end"] - c["start"] <= 8.0
    # Ni una palabra perdida ni reordenada.
    assert " ".join(c["text"] for c in out).split() == PARED.split()


def test_dp_prefiere_cortar_en_la_pausa():
    """Con una respiración clara en la palabra 9, el corte cae ahí."""
    seg = _seg(PARED, 10.0, gaps={9: 1.4})
    out = ps.resegment([seg])
    borde = 10.0 + 10 * 0.5 + 1.4        # start de la palabra 10
    assert any(abs(c["start"] - borde) < 0.6 for c in out[1:]), \
        [round(c["start"], 2) for c in out]


def test_todas_las_palabras_exactamente_una_vez():
    ws = _words(PARED, 0.0)
    groups = ps.segment_words(ws)
    plano = [w["word"] for g in groups for w in g]
    assert plano == [w["word"] for w in ws]


# ── guardas ───────────────────────────────────────────────────────────────

def test_segmento_corto_pasa_intacto():
    seg = _seg("una frase corta y comoda", 10.0)
    out = ps.resegment([seg])
    assert out == [seg] and out[0] is seg


def test_sin_words_pasa_intacto():
    seg = {"start": 10.0, "end": 30.0, "text": PARED}
    assert ps.resegment([seg]) == [seg]


def test_words_reatadas_por_posicion_no_se_parten():
    """El camino referencia re-ata words por posición: si no SUENAN al
    texto del cartel, partir dispersaría texto arbitrario. No tocar."""
    seg = _seg(PARED, 10.0)
    seg["words"] = _words("otra cosa completamente distinta que nada "
                          "tiene que ver con este cartel largo aqui "
                          "puesto por el bucketeo posicional del motor "
                          "de referencia externa", 10.0)
    out = ps.resegment([seg])
    assert out == [seg]


def test_words_no_monotonas_no_se_parten():
    seg = _seg(PARED, 10.0)
    seg["words"][5]["start"] = 3.0        # stamp roto
    out = ps.resegment([seg])
    assert out == [seg]


def test_preserva_la_puntuacion_curada():
    texto = ("¿Cuántas veces, aburrido, al diablo llamé? y cuántas "
             "chicas buenas me habrán amado ya sin que yo lo sepa")
    seg = _seg(texto, 10.0, gaps={7: 1.3})
    out = ps.resegment([seg])
    assert len(out) >= 2
    assert out[0]["text"].startswith("¿Cuántas veces, aburrido,")
    assert " ".join(c["text"] for c in out) == texto


def test_lead_hold_solo_en_bordes_nuevos():
    seg = _seg(PARED, 9.6, gaps={9: 1.3, 19: 1.3})   # start original con lead
    out = ps.resegment([seg], lead_s=0.15, hold_s=0.25)
    assert out[0]["start"] == 9.6                     # hereda el start (lead ya aplicado)
    for prev, c in zip(out, out[1:]):
        assert c["start"] >= prev["end"]              # nunca solapan
        primera = c["words"][0]["start"]
        assert c["start"] >= primera - 0.16           # lead manual acotado


def test_ultimo_cartel_hereda_el_end_original():
    seg = _seg(PARED, 10.0, fin=40.0, gaps={9: 1.3})  # end con hold del pipeline
    out = ps.resegment([seg])
    assert out[-1]["end"] == 40.0


def test_flags_de_procedencia_se_copian():
    seg = _seg(PARED, 10.0, gaps={9: 1.3}, gap_recovered=True, ctc_lr=-0.2)
    out = ps.resegment([seg])
    assert len(out) >= 2
    for c in out:
        assert c.get("gap_recovered") is True         # procedencia viaja
        assert "ctc_lr" not in c                      # telemetría de línea entera no
        assert c.get("phrase_split") is True


# ── regresión: notas sostenidas partían la frase (staging, job ff76ebfa) ──

def _seg_con_nota_sostenida():
    """'Estuve rodando por ahí' con la última nota sostenida 8s — geometría
    exacta de los carteles #17 y #37 del job real: 4 palabras, la última de
    8,00s. El costo por duración empujaba a partir la frase y dejaba la
    palabra sostenida SOLA en un cartel."""
    ws = [{"word": "estuve", "start": 100.0, "end": 100.34},
          {"word": "rodando", "start": 100.4, "end": 101.76},
          {"word": "por", "start": 101.9, "end": 102.14},
          {"word": "ahi", "start": 102.3, "end": 110.30}]   # 8,00s sostenida
    return {"start": 100.0, "end": 110.3, "text": "estuve rodando por ahi",
            "words": ws}


def test_nota_sostenida_no_parte_la_frase():
    out = ps.resegment([_seg_con_nota_sostenida()])
    assert len(out) == 1, \
        f"la frase de 4 palabras no debe partirse, salieron {len(out)}"


def test_nunca_deja_palabras_huerfanas():
    """Ningún cartel con menos de _MIN_LEN palabras (salvo que el segmento
    entero sea más corto que eso)."""
    casos = [_seg_con_nota_sostenida(),
             _seg(PARED, 10.0, gaps={9: 1.3, 19: 1.3}),
             _seg("una frase de siete palabras aca mismo", 10.0)]
    for seg in casos:
        for c in ps.resegment([seg]):
            n = len(c["text"].split())
            assert n >= ps._MIN_LEN or n == len(seg["words"]), \
                f"cartel huérfano de {n} palabra(s): {c['text']!r}"


def test_pared_larga_con_sostenido_final_igual_se_parte():
    """El mínimo no debe impedir partir una pared legítima."""
    seg = _seg(PARED, 10.0, gaps={9: 1.3, 19: 1.3})
    seg["words"][-1]["end"] = seg["words"][-1]["start"] + 7.0
    seg["end"] = seg["words"][-1]["end"]
    out = ps.resegment([seg])
    assert len(out) >= 3
    assert all(len(c["text"].split()) >= ps._MIN_LEN for c in out)
