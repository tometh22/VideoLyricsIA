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
                      "uncovered_seconds", "worst_span_s", "text_mismatches",
                      "voiced_gaps", "voiced_gap_s"}
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


def test_mismatch_accepts_fully_verified_structural_vocalization():
    from audio_coverage import text_mismatches
    words = _wtxt("Real Real Real", 60.9, paso=1.2)
    seg = [{
        "start": 60.9,
        "end": 65.1,
        "text": "Real uoo uou",
        "structural_hybrid": True,
        "structural_repair": True,
        "consensus_sources": [
            "gemini_audio_cardinality",
            "ctc_vocal_stem",
            "ctc_original_mix",
            "acoustic_topology_stem_mix",
        ],
    }]
    assert text_mismatches(seg, words) == []


def test_mismatch_does_not_trust_partial_structural_provenance():
    from audio_coverage import text_mismatches
    words = _wtxt("Real Real Real", 60.9, paso=1.2)
    seg = [{
        "start": 60.9,
        "end": 65.1,
        "text": "Real uoo uou",
        "structural_hybrid": True,
        "structural_repair": True,
        "consensus_sources": ["ctc_vocal_stem"],
    }]
    assert len(text_mismatches(seg, words)) == 1


def test_summarize_incluye_text_mismatches():
    from audio_coverage import summarize
    words = _wtxt("estuve rodando por ahi sin parar", 10.0)
    seg = [{"start": 10.0, "end": 13.5, "text": "otra cosa totalmente distinta aqui"}]
    s = summarize(seg, words)
    assert s["text_mismatches"] == 1


# ── voiced_gaps: el guardrail que no depende del ASR ──────────────────────

def _mock_vad(monkeypatch, regiones):
    import anchor_align
    monkeypatch.setattr(anchor_align, "vocal_regions", lambda *a, **k: regiones)


def test_voiced_gap_detecta_canto_sin_cartel(monkeypatch, tmp_path):
    """El caso dcf773b5: hueco de 30s entre carteles, el stem canta ahí,
    audio_coverage no lo veía (el ASR estaba sordo). Este sí."""
    from audio_coverage import voiced_gaps
    stem = tmp_path / "s.wav"; stem.write_bytes(b"x")
    _mock_vad(monkeypatch, [(215.0, 229.0)])         # canto real en el hueco
    segs = [_seg(180, 209), _seg(240, 250)]
    got = voiced_gaps(segs, str(stem))
    assert len(got) == 1
    assert got[0]["voiced_s"] == 14.0


def test_voiced_gap_ignora_hueco_instrumental(monkeypatch, tmp_path):
    """Un solo de guitarra de 30s NO es letra faltante: el VAD del stem no
    marca voz ahí."""
    from audio_coverage import voiced_gaps
    stem = tmp_path / "s.wav"; stem.write_bytes(b"x")
    _mock_vad(monkeypatch, [(100.0, 200.0)])         # voz solo FUERA del hueco
    segs = [_seg(100, 209), _seg(240, 250)]
    assert voiced_gaps(segs, str(stem)) == []


def test_voiced_gap_sin_stem_no_acusa(monkeypatch):
    from audio_coverage import voiced_gaps
    assert voiced_gaps([_seg(10, 20), _seg(60, 70)], None) == []


def test_voiced_gap_vad_vacio_no_acusa(monkeypatch, tmp_path):
    """Stem ilegible / librosa ausente → vocal_regions devuelve [] → sin
    evidencia no se acusa."""
    from audio_coverage import voiced_gaps
    stem = tmp_path / "s.wav"; stem.write_bytes(b"x")
    _mock_vad(monkeypatch, [])
    assert voiced_gaps([_seg(10, 20), _seg(60, 70)], str(stem)) == []


def test_voiced_gap_cola_final(monkeypatch, tmp_path):
    """La cola también cuenta: canto después del último cartel."""
    from audio_coverage import voiced_gaps
    stem = tmp_path / "s.wav"; stem.write_bytes(b"x")
    _mock_vad(monkeypatch, [(205.0, 260.0)])
    got = voiced_gaps([_seg(10, 200)], str(stem), audio_duration=278.0)
    assert len(got) == 1 and got[0]["voiced_s"] > 50


def test_voiced_gap_inicial_solo_cuenta_con_live_hint(monkeypatch, tmp_path):
    from audio_coverage import voiced_gaps
    stem = tmp_path / "s.wav"; stem.write_bytes(b"x")
    _mock_vad(monkeypatch, [(2.0, 11.0)])
    segments = [_seg(13.0, 17.0), _seg(20.0, 24.0)]
    assert voiced_gaps(segments, str(stem), audio_duration=30.0) == []
    got = voiced_gaps(
        segments, str(stem), audio_duration=30.0, include_leading=True,
    )
    assert got and got[0]["start"] == 0.0


def test_summarize_incluye_voiced_gap(monkeypatch, tmp_path):
    from audio_coverage import summarize
    stem = tmp_path / "s.wav"; stem.write_bytes(b"x")
    _mock_vad(monkeypatch, [(30.0, 50.0)])
    words = _palabras(10, 20)
    s = summarize([_seg(9.5, 20.5), _seg(60, 70)], words, stem_path=str(stem))
    assert s["voiced_gaps"] == 1
    assert s["voiced_gap_s"] == 20.0
    # y las claves viejas siguen (compat con el log)
    assert "audio_coverage" in s and "text_mismatches" in s


# ── el breaker no puede confundir fuga instrumental con canto ─────────────
# Batch de 12 (30-07-2026): disparó en 3 canciones y las 8 zonas acusadas
# eran falsas — al transcribirlas sobre el stem devolvieron nada o pura
# alucinación. El stem de demucs arrastra solos de guitarra, vientos y
# colas de reverb: energía sí, canto no.

def _vad(monkeypatch, regiones):
    import audio_coverage as _ac
    monkeypatch.setattr("anchor_align.vocal_regions", lambda *a, **k: regiones)
    return _ac


def test_fuga_instrumental_no_dispara(monkeypatch, tmp_path):
    """Rata Blanca: 76,2s de outro de guitarra. La energía sola lo acusa;
    el veredicto del sondeo (whisper oyó 'Amara.org') lo absuelve."""
    ac = _vad(monkeypatch, [(376.0, 378.0), (400.0, 402.2)])
    stem = tmp_path / "s.wav"; stem.write_bytes(b"RIFF")
    segs = [{"start": 360, "end": 375.2, "text": "ultima linea"}]
    assert ac.voiced_gaps(segs, str(stem), audio_duration=451.4)
    assert ac.voiced_gaps(segs, str(stem), audio_duration=451.4,
                          rescue_skipped=[(375.2, "alucinacion")]) == []


def test_hueco_real_si_dispara(monkeypatch, tmp_path):
    """Hombre Lobo 116,9-131,0: cuatro versos perdidos, 100% cantado."""
    ac = _vad(monkeypatch, [(116.9, 131.0)])
    stem = tmp_path / "s.wav"; stem.write_bytes(b"RIFF")
    segs = [{"start": 100, "end": 116.9, "text": "a"},
            {"start": 131.0, "end": 140, "text": "b"}]
    out = ac.voiced_gaps(segs, str(stem), audio_duration=219.3)
    assert len(out) == 1 and out[0]["voiced_s"] >= 13.0


def test_la_fraccion_NO_discrimina(monkeypatch, tmp_path):
    """Guardián de la tabla del docstring: el hueco fundacional (Rodando
    209-240, 14s cantados de 31 = 45%) tiene MENOS fracción que un falso
    positivo real (Pericos, 65%). Cualquier corte por fracción que mate al
    falso mata también al verdadero — por eso el gate es inerte."""
    ac = _vad(monkeypatch, [(215.0, 229.0)])
    stem = tmp_path / "s.wav"; stem.write_bytes(b"RIFF")
    real = [{"start": 180, "end": 209, "text": "a"},
            {"start": 240, "end": 250, "text": "b"}]
    got = ac.voiced_gaps(real, str(stem), audio_duration=280.0)
    assert len(got) == 1, "el defecto fundacional debe seguir detectándose"
    frac = got[0]["voiced_s"] / (got[0]["end"] - got[0]["start"])
    assert frac < 0.65, "y su fracción es menor que la de un falso positivo"


def test_no_contradice_al_sondeo_de_gap_rescue(monkeypatch, tmp_path):
    """Pericos: 65% de energía en un break de 20s, pero el ASR sobre el stem
    oyó 2 palabras en total. gap_rescue lo descartó; el breaker lo acata."""
    ac = _vad(monkeypatch, [(106.5, 119.7)])
    stem = tmp_path / "s.wav"; stem.write_bytes(b"RIFF")
    segs = [{"start": 96, "end": 106.5, "text": "a"},
            {"start": 126.8, "end": 131, "text": "b"}]
    assert ac.voiced_gaps(segs, str(stem), audio_duration=211.0,
                          min_voiced_frac=0.0)
    assert ac.voiced_gaps(segs, str(stem), audio_duration=211.0,
                          min_voiced_frac=0.0,
                          rescue_skipped=[(106.5, "sin_canto")]) == []


def test_summarize_propaga_los_veredictos(monkeypatch, tmp_path):
    ac = _vad(monkeypatch, [(20.0, 40.0)])
    stem = tmp_path / "s.wav"; stem.write_bytes(b"RIFF")
    segs = [{"start": 10, "end": 20, "text": "hola que tal"},
            {"start": 40, "end": 50, "text": "chau que tal"}]
    words = [{"word": "hola", "start": 11, "end": 12}]
    con = ac.summarize(segs, words, stem_path=str(stem), audio_duration=60.0)
    sin = ac.summarize(segs, words, stem_path=str(stem), audio_duration=60.0,
                       rescue_skipped=[(20.0, "alucinacion")])
    assert con["voiced_gaps"] == 1 and sin["voiced_gaps"] == 0
