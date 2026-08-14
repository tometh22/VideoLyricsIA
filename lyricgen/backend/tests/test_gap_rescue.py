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


def test_hueco_inicial_solo_se_habilita_para_audio_live():
    segs = [_seg(13.4, 17.0), _seg(19.5, 23.0)]
    assert gr.find_gaps(segs, min_gap_s=8.0) == []
    assert gr.find_gaps(
        segs, min_gap_s=8.0, include_leading=True,
    ) == [(0.0, 13.4)]


def test_ventana_whisper_registra_provenance_del_job(monkeypatch):
    from pathlib import Path
    from types import SimpleNamespace
    import openai
    import provenance

    captured = {}

    class _Recorder:
        def finish(self, **kwargs):
            captured["summary"] = kwargs["response_summary"]

    class _Transcriptions:
        def create(self, **_kwargs):
            return SimpleNamespace(words=[
                SimpleNamespace(word="hola", start=0.1, end=0.5),
            ])

    class _Client:
        audio = SimpleNamespace(transcriptions=_Transcriptions())

    def _ffmpeg(cmd, **_kwargs):
        Path(cmd[-1]).write_bytes(b"audio")

    monkeypatch.setattr("subprocess.run", _ffmpeg)
    monkeypatch.setattr(openai, "OpenAI", _Client)
    monkeypatch.setattr(
        provenance,
        "record_ai_call",
        lambda **kwargs: captured.update(kwargs) or _Recorder(),
    )

    words = gr._transcribe_window(
        "song.wav", 10.0, 20.0, language="es", job_id="gap-job",
    )

    assert words[0]["start"] == 10.1
    assert captured["job_id"] == "gap-job"
    assert captured["tool_name"] == "whisper-1-gap-rescue"
    assert captured["tool_provider"] == "openai"
    assert captured["summary"] == "succeeded"


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


def test_live_rescata_palabra_inicial_sostenida_y_su_onset_vad(
        audio, tmp_path, monkeypatch):
    """Caso Los Pericos: WhisperX pierde 'Hoy' y empieza cerca de 13s,
    mientras el stem confirma que la voz sostenida arrancó cerca de 5.5s."""
    stem = tmp_path / "stem.wav"
    stem.write_bytes(b"RIFF" + b"\0" * 128)
    _con_vad(monkeypatch, [(5.5, 14.0)])
    monkeypatch.setattr(
        gr, "_transcribe_window",
        lambda *a, **k: [{"word": "Hoy", "start": 12.44, "end": 13.84}],
    )
    segs = [
        _seg(13.374, 17.267, "temprano estuve pensando en vos"),
        _seg(19.5, 23.5, "pasó el tiempo y ahora me siento mejor"),
        _seg(25.5, 31.0, "cuando puedo no puedo"),
    ]

    out, stats = gr.rescue(
        segs, audio, stem_path=str(stem), audio_duration=32.0,
        language="es", include_leading=True,
        reference_text="Hoy temprano estuve pensando en vos",
    )

    rescued = [s for s in out if s.get("gap_rescued")]
    assert stats["rescued_lines"] == 1
    assert rescued[0]["text"] == "Hoy"
    assert rescued[0]["start"] == 5.5
    assert rescued[0]["end"] < segs[0]["start"]


def test_palabra_inicial_sola_exige_referencia(audio, tmp_path, monkeypatch):
    stem = tmp_path / "stem.wav"
    stem.write_bytes(b"RIFF" + b"\0" * 128)
    _con_vad(monkeypatch, [(5.5, 14.0)])
    monkeypatch.setattr(
        gr, "_transcribe_window",
        lambda *a, **k: [{"word": "Fantasma", "start": 12.0, "end": 13.0}],
    )
    segs = [_seg(13.4, 17), _seg(20, 24), _seg(26, 30)]
    out, stats = gr.rescue(
        segs, audio, stem_path=str(stem), audio_duration=31,
        include_leading=True, reference_text="Hoy temprano",
    )
    assert stats["rescued_lines"] == 0
    assert out == segs


def test_live_rescata_estribillo_espaciado_si_el_stem_y_referencia_coinciden(
        audio, tmp_path, monkeypatch):
    stem = tmp_path / "stem.wav"
    stem.write_bytes(b"RIFF" + b"\0" * 128)
    starts = [67.2, 73.9, 79.9, 85.9, 91.9, 97.9, 103.9, 109.9]
    _con_vad(monkeypatch, [
        (60.0, 64.5), *((start - 0.3, start + 1.5) for start in starts),
    ])
    monkeypatch.setattr(
        gr, "_transcribe_window",
        lambda *a, **k: [
            {"word": "Real", "start": start, "end": start + 1.0}
            for start in starts
        ],
    )
    segs = [
        _seg(13, 17), _seg(25, 31), _seg(60.8, 64.1, "Real"),
    ]
    out, stats = gr.rescue(
        segs, audio, stem_path=str(stem), audio_duration=116.0,
        language="es", include_leading=True,
        reference_text="Real wow wow\nReal wow wow\nReal wow wow",
    )
    rescued = [s for s in out if s.get("gap_rescued")]
    assert stats["rescued_lines"] == 8
    assert [s["text"] for s in rescued] == ["Real"] * 8
    assert [round(s["start"], 1) for s in rescued] == starts


def test_sparse_singletons_without_reference_are_rejected(
        audio, tmp_path, monkeypatch):
    stem = tmp_path / "stem.wav"
    stem.write_bytes(b"RIFF" + b"\0" * 128)
    _con_vad(monkeypatch, [(60.0, 80.0)])
    monkeypatch.setattr(
        gr, "_transcribe_window",
        lambda *a, **k: [
            {"word": "Fantasma", "start": t, "end": t + 1.0}
            for t in (67.0, 73.0, 79.0)
        ],
    )
    segs = [_seg(13, 17), _seg(25, 31), _seg(60, 64)]
    out, stats = gr.rescue(
        segs, audio, stem_path=str(stem), audio_duration=84,
        include_leading=True, reference_text="Real wow wow",
    )
    assert stats["rescued_lines"] == 0
    assert out == segs


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


# ── gates de VAD y sanidad física (caso Hombre Lobo, job 9e19f29c) ────────

def _con_vad(monkeypatch, regiones):
    monkeypatch.setattr(gr, "_vad_regions", lambda *a, **k: regiones)


def test_vad_descarta_hueco_instrumental(audio, tmp_path, monkeypatch):
    """El outro de saxo hizo alucinar a whisper con el coro en eco (6 líneas
    falsas). Si el stem no canta en el hueco, NO se transcribe ni se emite."""
    stem = tmp_path / "s.wav"; stem.write_bytes(b"RIFF" + b"\0" * 128)
    _con_vad(monkeypatch, [(5.0, 18.0)])          # voz solo ANTES del hueco
    monkeypatch.setattr(gr, "_transcribe_window",
                        lambda *a, **k: pytest.fail("no debería transcribir"))
    segs = [_seg(10, 20), _seg(60, 70)]
    out, stats = gr.rescue(segs, audio, stem_path=str(stem), audio_duration=80.0)
    assert stats["rescued_lines"] == 0
    assert any(r == "sin_voz_vad" for _, r in stats["skipped"])


def test_vad_permite_hueco_cantado(audio, tmp_path, monkeypatch):
    """El hueco histórico de UMG: 10,6s con 12s de canto según el stem."""
    stem = tmp_path / "s.wav"; stem.write_bytes(b"RIFF" + b"\0" * 128)
    _con_vad(monkeypatch, [(15.0, 55.0)])          # canta en el hueco
    monkeypatch.setattr(
        gr, "_transcribe_window",
        lambda *a, **k: _words("vos en un mundo casi ya sin ley nosotros "
                               "somos el amor", 25.0))
    segs = [_seg(10, 20), _seg(60, 70)]
    out, stats = gr.rescue(segs, audio, stem_path=str(stem), audio_duration=80.0)
    assert stats["rescued_lines"] >= 1


def test_umbral_8s_rescata_el_hueco_historico():
    """find_gaps con el default nuevo agarra el hueco de 10,6s que el
    umbral viejo (12s) dejaba pasar."""
    segs = [_seg(110, 120.4), _seg(131.0, 140)]
    assert gr.find_gaps(segs) == [(120.4, 131.0)]


def test_sanidad_fisica_mata_los_slivers(audio, tmp_path, monkeypatch):
    """5 palabras en 0,3s = 16 palabras/s: alucinación, no canto."""
    stem = tmp_path / "s.wav"; stem.write_bytes(b"RIFF" + b"\0" * 128)
    _con_vad(monkeypatch, [(20.0, 55.0)])
    def _sliver(*a, **k):
        t = 30.0
        out = []
        for w in "en el fondo nunca me".split():
            out.append({"word": w, "start": round(t, 2), "end": round(t + 0.05, 2)})
            t += 0.06
        return out
    monkeypatch.setattr(gr, "_transcribe_window", _sliver)
    segs = [_seg(10, 20), _seg(60, 70)]
    out, stats = gr.rescue(segs, audio, stem_path=str(stem), audio_duration=80.0)
    assert stats["rescued_lines"] == 0, "un destello de 0,3s no es una línea"


def test_dedup_del_eco_extiende_en_vez_de_duplicar(audio, tmp_path, monkeypatch):
    stem = tmp_path / "s.wav"; stem.write_bytes(b"RIFF" + b"\0" * 128)
    _con_vad(monkeypatch, [(20.0, 55.0)])
    monkeypatch.setattr(
        gr, "_transcribe_window",
        lambda *a, **k: (_words("en el fondo nunca me imagine", 25.0)
                         + _words("en el fondo nunca me imagine", 29.5)))
    segs = [_seg(10, 20), _seg(60, 70)]
    out, stats = gr.rescue(segs, audio, stem_path=str(stem), audio_duration=80.0)
    rescatadas = [s for s in out if s.get("gap_rescued")]
    assert len(rescatadas) == 1, "el eco repetido se funde, no se duplica"


# ── rescate por MISMATCH: carteles manchados (Hombre Lobo, job ed72b608) ──

def _witness(texto, ini, paso=0.5):
    return _words(texto, ini, paso)


def test_reemplaza_cartel_manchado(audio, tmp_path, monkeypatch):
    """El alineador untó 'En el fondo' sobre 6,8s que cantan otra cosa. Con
    testigo que lo delata (ratio<0.3), la zona se re-transcribe y el cartel
    se reemplaza por lo realmente cantado."""
    stem = tmp_path / "s.wav"; stem.write_bytes(b"RIFF" + b"\0" * 128)
    _con_vad(monkeypatch, [(10.0, 60.0)])
    manchado = _seg(20, 32, "En el fondo")
    segs = [_seg(10, 18, "linea previa bien puesta aqui"), manchado,
            _seg(40, 48, "otra linea posterior bien puesta")]
    testigo = (_witness("linea previa bien puesta aqui", 10.5)
               + _witness("nunca me imagine cantando para vos en un mundo", 21.0)
               + _witness("casi ya sin ley nosotros somos el amor", 26.5)
               + _witness("otra linea posterior bien puesta", 40.5))
    monkeypatch.setattr(
        gr, "_transcribe_window",
        lambda *a, **k: (_witness("nunca me imagine cantando para vos en un mundo", 21.0)
                         + _witness("casi ya sin ley nosotros somos el amor", 26.5)))
    out, stats = gr.rescue(segs, audio, stem_path=str(stem),
                           audio_duration=60.0, asr_words=testigo)
    assert stats["mismatch_replaced"] == 1
    textos = [s["text"] for s in out]
    assert "En el fondo" not in textos, "el cartel manchado debe reemplazarse"
    assert any("nunca me imagine" in t for t in textos)
    assert all(s.get("review") for s in out if s.get("gap_rescued"))


def test_cartel_correcto_no_se_toca(audio, tmp_path, monkeypatch):
    stem = tmp_path / "s.wav"; stem.write_bytes(b"RIFF" + b"\0" * 128)
    _con_vad(monkeypatch, [(10.0, 60.0)])
    seg = _seg(20, 26, "nunca me imagine cantando para vos")
    testigo = (_witness("nunca me imagine cantando para vos", 20.5)
               + _witness("relleno uno dos tres cuatro", 28.5))
    monkeypatch.setattr(gr, "_transcribe_window",
                        lambda *a, **k: pytest.fail("no debería re-transcribir"))
    out, stats = gr.rescue([_seg(10, 18), seg, _seg(28, 36)], audio,
                           stem_path=str(stem), audio_duration=40.0,
                           asr_words=testigo)
    assert stats["mismatch_replaced"] == 0


def test_mismatch_sin_voz_no_reemplaza(audio, tmp_path, monkeypatch):
    """Cartel manchado pero el stem no canta en su zona → se deja (el VAD
    manda: sin voz no hay verdad con qué reemplazar)."""
    stem = tmp_path / "s.wav"; stem.write_bytes(b"RIFF" + b"\0" * 128)
    _con_vad(monkeypatch, [(5.0, 12.0)])              # voz solo al principio
    manchado = _seg(20, 32, "En el fondo")
    testigo = _witness("texto totalmente distinto que delata al cartel aqui", 21.0)
    out, stats = gr.rescue([_seg(10, 12), manchado, _seg(40, 48)], audio,
                           stem_path=str(stem), audio_duration=60.0,
                           asr_words=testigo)
    assert stats["mismatch_replaced"] == 0
    assert any(r == "mismatch_sin_voz" for _, r in stats["skipped"])
    assert any(s.get("text") == "En el fondo" for s in out)


def test_reemplazo_exige_cubrir_la_zona(audio, tmp_path, monkeypatch):
    """Si la re-transcripción devuelve poco, el cartel NO se borra: peor un
    cartel dudoso que un agujero."""
    stem = tmp_path / "s.wav"; stem.write_bytes(b"RIFF" + b"\0" * 128)
    _con_vad(monkeypatch, [(10.0, 60.0)])
    manchado = _seg(20, 32, "En el fondo")
    testigo = _witness("texto totalmente distinto que delata al cartel aqui", 21.0)
    monkeypatch.setattr(gr, "_transcribe_window",
                        lambda *a, **k: _witness("dos palabras", 21.0))
    out, stats = gr.rescue([_seg(10, 18), manchado, _seg(40, 48)], audio,
                           stem_path=str(stem), audio_duration=60.0,
                           asr_words=testigo)
    assert stats["mismatch_replaced"] == 0
    assert any(s.get("text") == "En el fondo" for s in out)


def test_sin_asr_words_no_hay_pasada_mismatch(audio, tmp_path, monkeypatch):
    stem = tmp_path / "s.wav"; stem.write_bytes(b"RIFF" + b"\0" * 128)
    _con_vad(monkeypatch, [(10.0, 60.0)])
    out, stats = gr.rescue([_seg(10, 18), _seg(20, 32, "En el fondo"),
                            _seg(40, 48)], audio, stem_path=str(stem),
                           audio_duration=60.0)
    assert stats["mismatch_replaced"] == 0
