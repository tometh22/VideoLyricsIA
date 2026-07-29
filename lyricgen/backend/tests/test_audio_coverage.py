"""Cobertura de la letra medida contra el AUDIO.

La métrica que existía se mide contra la letra de REFERENCIA y por eso SUBE
cuando la referencia viene recortada: un job de producción reportaba 100 %
teniendo el 39 % del canto sin ninguna línea. Ésta es la inversa y sólo puede
bajar si se pierde canto.
"""
from audio_coverage import audio_coverage, summarize, uncovered_spans


def _w(ini, fin, txt="x"):
    return {"word": txt, "start": ini, "end": fin}


def _palabras(desde, hasta, paso=0.5):
    out, t = [], desde
    while t + 0.4 <= hasta:
        out.append(_w(round(t, 2), round(t + 0.4, 2)))
        t += paso
    return out


def _seg(ini, fin):
    return {"start": ini, "end": fin, "text": "linea"}


# ── audio_coverage ────────────────────────────────────────────────────────

def test_todo_cubierto_da_1():
    words = _palabras(10, 20)
    assert audio_coverage([_seg(9.5, 20.5)], words) == 1.0


def test_nada_cubierto_da_0():
    assert audio_coverage([_seg(100, 110)], _palabras(10, 20)) == 0.0


def test_baja_en_proporcion_al_canto_perdido():
    words = _palabras(10, 20) + _palabras(30, 40)
    cov = audio_coverage([_seg(9.5, 20.5)], words)
    assert 0.45 < cov < 0.55, cov


def test_sin_palabras_no_afirma_perdida():
    """Sin palabras contra las cuales medir no se puede afirmar que se perdió
    nada: devolver 0 haría que un instrumental parezca un fallo total."""
    assert audio_coverage([_seg(0, 10)], []) == 1.0
    assert audio_coverage([_seg(0, 10)], None) == 1.0


def test_no_penaliza_el_outro_instrumental():
    """EL FALSO POSITIVO A EVITAR. Un tema que canta hasta 180s y sigue 90s
    de instrumental NO debe medir mal: no hay palabras ahí. Es lo que separa
    'faltan líneas' de 'hay un solo de guitarra'."""
    words = _palabras(20, 180)
    assert audio_coverage([_seg(19.5, 180.5)], words) == 1.0


def test_el_punto_medio_tolera_el_clamp_de_bordes():
    """El clamp monótono recorta el `end` de cada línea; una palabra que
    arranca justo en el borde no debe contarse como huérfana."""
    w = [_w(10.0, 10.9)]
    assert audio_coverage([_seg(10.4, 12.0)], w) == 1.0


def test_ignora_entradas_corruptas():
    words = [_w(10, 11), {"word": "y"}, "no-dict", None]
    assert 0.0 <= audio_coverage([_seg(9, 12)], words) <= 1.0


# ── uncovered_spans ───────────────────────────────────────────────────────

def test_reporta_donde_se_perdio_la_letra():
    words = _palabras(10, 20) + _palabras(60, 80)
    spans = uncovered_spans([_seg(9.5, 20.5)], words)
    assert len(spans) == 1
    ini, fin, n = spans[0]
    assert 59 <= ini <= 61 and 79 <= fin <= 81 and n > 10


def test_no_reporta_restos_minimos():
    """Dos palabras sueltas no son una zona sin letra."""
    words = _palabras(10, 20) + [_w(50, 50.4), _w(50.6, 51.0)]
    assert uncovered_spans([_seg(9.5, 20.5)], words) == []


def test_separa_zonas_distintas():
    words = _palabras(10, 20) + _palabras(60, 70) + _palabras(120, 130)
    assert len(uncovered_spans([_seg(9.5, 20.5)], words)) == 2


# ── summarize ─────────────────────────────────────────────────────────────

def test_summarize_es_serializable_y_completo():
    words = _palabras(10, 20) + _palabras(60, 80)
    s = summarize([_seg(9.5, 20.5)], words)
    assert set(s) == {"audio_coverage", "uncovered_spans",
                      "uncovered_seconds", "worst_span_s", "text_mismatches"}
    assert all(isinstance(v, (int, float)) for v in s.values())
    assert s["uncovered_spans"] == 1
    assert 18 <= s["worst_span_s"] <= 21
    assert s["audio_coverage"] < 0.5


def test_summarize_sin_perdida():
    s = summarize([_seg(9.5, 20.5)], _palabras(10, 20))
    assert s["audio_coverage"] == 1.0
    assert s["uncovered_spans"] == 0
    assert s["worst_span_s"] == 0.0


# ── el caso real ──────────────────────────────────────────────────────────

def test_caso_real_referencia_recortada():
    """Job b3a51559: la letra terminaba en 186s y el tema cantaba hasta 265s.
    La métrica vieja daba 15/15 = 100 %; ésta tiene que delatar la pérdida."""
    cantado = _palabras(24, 186) + _palabras(200, 265)
    lineas = [_seg(24, 186)]
    cov = audio_coverage(lineas, cantado)
    assert cov < 0.75, f"debería delatar la pérdida, dio {cov:.2f}"
    assert summarize(lineas, cantado)["worst_span_s"] > 50


# ── text_mismatches: ¿el cartel DICE lo que se canta? ─────────────────────

def _wtxt(texto, ini, paso=0.5):
    out, t = [], ini
    for w in texto.split():
        out.append({"word": w, "start": round(t, 2), "end": round(t + 0.4, 2)})
        t += paso
    return out


def test_mismatch_detecta_cartel_con_texto_equivocado():
    """El caso que el usuario vio a ojo y la cobertura no: cartel de la
    estrofa pintado sobre audio del estribillo."""
    from audio_coverage import text_mismatches
    words = _wtxt("estuve rodando por ahi sin parar", 10.0)
    seg = [{"start": 10.0, "end": 13.5,
            "text": "cuando los meses pasan y mi ropa no cambia"}]
    got = text_mismatches(seg, words)
    assert len(got) == 1 and got[0]["index"] == 0 and got[0]["ratio"] < 0.4


def test_mismatch_ok_cuando_el_texto_coincide():
    from audio_coverage import text_mismatches
    words = _wtxt("estuve rodando por ahi sin parar", 10.0)
    seg = [{"start": 10.0, "end": 13.5,
            "text": "Estuve rodando por ahí sin parar"}]
    assert text_mismatches(seg, words) == []


def test_mismatch_tolera_mishears_foneticos():
    """'le realizan la' vs 'legalícenla' — mismo sonido, tokens distintos.
    No debe acusarse."""
    from audio_coverage import text_mismatches
    words = _wtxt("le realizan la vida entera", 10.0)
    seg = [{"start": 10.0, "end": 13.0, "text": "legalicenla vida entera"}]
    assert text_mismatches(seg, words) == []


def test_mismatch_no_acusa_sin_evidencia():
    """Ventanas con <2 palabras del ASR no se evalúan: sin evidencia no se
    acusa (evita falsos positivos en zonas que el ASR apenas oyó)."""
    from audio_coverage import text_mismatches
    words = [{"word": "eco", "start": 10.0, "end": 10.4}]
    seg = [{"start": 9.0, "end": 14.0, "text": "una linea larga cualquiera"}]
    assert text_mismatches(seg, words) == []


def test_summarize_incluye_text_mismatches():
    from audio_coverage import summarize
    words = _wtxt("estuve rodando por ahi sin parar", 10.0)
    seg = [{"start": 10.0, "end": 13.5, "text": "otra cosa totalmente distinta aqui"}]
    s = summarize(seg, words)
    assert s["text_mismatches"] == 1
