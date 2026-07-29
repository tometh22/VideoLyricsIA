"""Rescate de huecos donde el ASR quedó sordo.

Caso real (job dcf773b5): 30,7 s sin un solo cartel mientras el audio canta.
Ni la recuperación de huecos ni la reconciliación de repeticiones lo taparon
— ambas colocan texto donde el ASR oyó algo, y whisperX no oyó nada ahí.
Medido: sobre la MEZCLA el ASR devuelve 1 palabra; sobre el STEM de voz, 16.
"""
import pytest

import gap_rescue as gr


def _seg(ini, fin, texto="linea"):
    return {"start": ini, "end": fin, "text": texto}


def _words(texto, ini, paso=0.5):
    out, t = [], ini
    for w in texto.split():
        out.append({"word": w, "start": round(t, 2), "end": round(t + 0.4, 2)})
        t += paso
    return out


# ── find_gaps (puro) ──────────────────────────────────────────────────────

def test_encuentra_hueco_interno():
    segs = [_seg(10, 20), _seg(50, 60)]
    assert gr.find_gaps(segs, min_gap_s=12.0) == [(20.0, 50.0)]


def test_ignora_huecos_chicos():
    segs = [_seg(10, 20), _seg(25, 35)]
    assert gr.find_gaps(segs, min_gap_s=12.0) == []


def test_encuentra_la_cola():
    segs = [_seg(10, 20)]
    assert gr.find_gaps(segs, 60.0, min_gap_s=12.0) == [(20.0, 60.0)]


def test_sin_duracion_no_reporta_cola():
    """Sin saber dónde termina el audio no se puede afirmar que falte cola."""
    assert gr.find_gaps([_seg(10, 20)], None, min_gap_s=12.0) == []


def test_ordena_segmentos_desordenados():
    segs = [_seg(50, 60), _seg(10, 20)]
    assert gr.find_gaps(segs, min_gap_s=12.0) == [(20.0, 50.0)]


# ── filtros de alucinación ────────────────────────────────────────────────

@pytest.mark.parametrize("texto", [
    "",
    "Subtítulos realizados por la comunidad de Amara.org",
    "ahi ahi ahi ahi ahi",           # bucle de una sola palabra
])
def test_descarta_texto_sospechoso(texto):
    assert gr._texto_sospechoso(texto) is True


def test_acepta_texto_real():
    assert gr._texto_sospechoso("estuve girando por alla sin parar") is False


# ── agrupado en líneas ────────────────────────────────────────────────────

def test_corta_lineas_por_silencio():
    ws = _words("uno dos tres", 10.0) + _words("cuatro cinco seis", 20.0)
    assert len(gr._agrupar_en_lineas(ws)) == 2


def test_no_corta_frase_continua():
    assert len(gr._agrupar_en_lineas(_words("uno dos tres cuatro", 10.0))) == 1


# ── rescue (con el ASR mockeado) ──────────────────────────────────────────

@pytest.fixture
def audio(tmp_path):
    p = tmp_path / "a.wav"
    p.write_bytes(b"RIFF" + b"\0" * 128)
    return str(p)


def test_rescata_el_hueco_y_no_pisa_lo_existente(audio, monkeypatch):
    segs = [_seg(10, 20, "antes"), _seg(60, 70, "despues")]
    # Densidad realista: el caso real dio 0,53 palabras/s dentro del hueco.
    monkeypatch.setattr(
        gr, "_transcribe_window",
        lambda *a, **k: _words("uno dos tres cuatro cinco seis siete ocho "
                               "nueve diez once doce trece catorce quince "
                               "dieciseis diecisiete dieciocho", 25.0))
    out, stats = gr.rescue(segs, audio, audio_duration=80.0)
    assert stats["rescued_lines"] >= 1
    nueva = [s for s in out if s.get("gap_rescued")]
    assert nueva
    assert all(20.0 < n["start"] and n["end"] < 60.0 for n in nueva)
    # marcada para revisión del operador: es texto crudo del ASR
    assert nueva[0].get("review") is True
    starts = [s["start"] for s in out]
    assert starts == sorted(starts)


def test_declina_si_la_ventana_no_tiene_canto(audio, monkeypatch):
    """Un hueco instrumental: el ASR devuelve poco y no se inventa nada."""
    segs = [_seg(10, 20), _seg(60, 70)]
    monkeypatch.setattr(gr, "_transcribe_window",
                        lambda *a, **k: _words("uh", 30.0))
    out, stats = gr.rescue(segs, audio, audio_duration=80.0)
    assert stats["rescued_lines"] == 0
    assert any(r == "sin_canto" for _, r in stats["skipped"])
    assert out == segs


def test_descarta_alucinacion_del_asr(audio, monkeypatch):
    segs = [_seg(10, 20), _seg(60, 70)]
    monkeypatch.setattr(
        gr, "_transcribe_window",
        lambda *a, **k: _words("Subtítulos realizados por la comunidad de "
                               "Amara.org gracias por ver el video y "
                               "suscribite al canal para mas contenido", 25.0))
    out, stats = gr.rescue(segs, audio, audio_duration=80.0)
    assert stats["rescued_lines"] == 0
    assert any(r == "alucinacion" for _, r in stats["skipped"])


def test_solo_toma_palabras_DENTRO_del_hueco(audio, monkeypatch):
    """El contexto se manda al ASR para que enganche, pero lo que ya estaba
    cubierto no se re-escribe."""
    segs = [_seg(10, 20, "antes"), _seg(60, 70, "despues")]
    # El ASR devuelve palabras del contexto (15s, 65s) y del hueco (30s).
    monkeypatch.setattr(
        gr, "_transcribe_window",
        lambda *a, **k: (_words("contexto previo aca", 15.0)
                         + _words("uno dos tres cuatro cinco seis siete ocho "
                                  "nueve diez once doce trece catorce", 25.0)
                         + _words("contexto posterior aca", 65.0)))
    out, stats = gr.rescue(segs, audio, audio_duration=80.0)
    for s in out:
        if s.get("gap_rescued"):
            assert 20.0 <= s["start"] and s["end"] <= 60.0


def test_usa_el_stem_cuando_esta(audio, tmp_path, monkeypatch):
    stem = tmp_path / "stem.wav"
    stem.write_bytes(b"RIFF" + b"\0" * 128)
    vistos = []
    monkeypatch.setattr(
        gr, "_transcribe_window",
        lambda path, *a, **k: (vistos.append(path)
                               or _words("uno dos tres cuatro cinco seis "
                                         "siete ocho nueve diez once doce "
                                         "trece catorce", 25.0)))
    _, stats = gr.rescue([_seg(10, 20), _seg(60, 70)], audio,
                         stem_path=str(stem), audio_duration=80.0)
    assert stats["source"] == "stem"
    assert all(p == str(stem) for p in vistos), "debe transcribir el STEM"


def test_sin_huecos_es_no_op(audio, monkeypatch):
    segs = [_seg(10, 20), _seg(22, 30)]
    monkeypatch.setattr(gr, "_transcribe_window",
                        lambda *a, **k: pytest.fail("no debería transcribir"))
    out, stats = gr.rescue(segs, audio, audio_duration=31.0)
    assert out == segs and stats["rescued_lines"] == 0


def test_kill_switch(monkeypatch):
    monkeypatch.delenv("GAP_RESCUE_ENABLED", raising=False)
    assert gr.is_enabled() is False
    monkeypatch.setenv("GAP_RESCUE_ENABLED", "1")
    assert gr.is_enabled() is True


def test_nunca_levanta_con_basura(audio):
    out, stats = gr.rescue([{"start": "x"}, None, 42], audio)  # type: ignore
    assert isinstance(out, list) and isinstance(stats, dict)
