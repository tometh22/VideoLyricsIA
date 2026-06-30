"""Tests del core determinista de multi-escena (scenes.py).

No tocan Veo/Gemini/ffmpeg — sólo la detección de secciones, el cap de
escenas únicas y el mapeo de energía, que es lo testeable offline.
"""

import scenes


def _line(start, end, text):
    return {"start": start, "end": end, "text": text}


def _song_with_repeated_chorus():
    """Intro instrumental + V1 + Coro + V2 + Coro (misma letra) + puente + Coro.

    Secciones de ~16s (4 líneas × 4s) como en canciones reales, para no chocar
    con MIN_SCENE_DURATION.
    """
    return [
        # intro instrumental: la primera línea recién entra a los 8s → intro [0,8)
        # verso 1 (16s)
        _line(8.0, 12.0, "camino solo por la ciudad"),
        _line(12.0, 16.0, "buscando una señal en la oscuridad"),
        _line(16.0, 20.0, "las luces no me dejan ver"),
        _line(20.0, 24.0, "hacia donde tengo que volver"),
        # coro (16s) — se repite literal más abajo → misma escena
        _line(24.0, 28.0, "y vuelvo a ti, siempre a ti"),
        _line(28.0, 32.0, "no hay nadie mas, solo tu y yo"),
        _line(32.0, 36.0, "y vuelvo a ti, siempre a ti"),
        _line(36.0, 40.0, "no hay nadie mas, solo tu y yo"),
        # verso 2 (16s)
        _line(40.0, 44.0, "la lluvia cae sobre el cristal"),
        _line(44.0, 48.0, "y pienso en lo que fue de mas"),
        _line(48.0, 52.0, "el tiempo se nos escapo"),
        _line(52.0, 56.0, "y nada de eso ya importo"),
        # coro repetido (16s, mismo texto → misma escena)
        _line(56.0, 60.0, "y vuelvo a ti, siempre a ti"),
        _line(60.0, 64.0, "no hay nadie mas, solo tu y yo"),
        _line(64.0, 68.0, "y vuelvo a ti, siempre a ti"),
        _line(68.0, 72.0, "no hay nadie mas, solo tu y yo"),
        # puente: hueco instrumental de 72 a 80 (8s) antes de la próxima línea
        # coro final (16s)
        _line(80.0, 84.0, "y vuelvo a ti, siempre a ti"),
        _line(84.0, 88.0, "no hay nadie mas, solo tu y yo"),
        _line(88.0, 92.0, "y vuelvo a ti, siempre a ti"),
        _line(92.0, 96.0, "no hay nadie mas, solo tu y yo"),
    ]


def test_chorus_repetition_shares_scene():
    secs = scenes.detect_sections(_song_with_repeated_chorus(), audio_duration=100.0)
    choruses = [s for s in secs if s.type == "coro"]
    assert len(choruses) >= 2, "deberían detectarse varios coros"
    # Todos los coros con la MISMA letra comparten recurrence_key (1 sola escena).
    keys = {c.recurrence_key for c in choruses}
    assert len(keys) == 1, f"los coros idénticos deben compartir escena, got {keys}"


def test_intro_and_bridge_detected_from_gaps():
    secs = scenes.detect_sections(_song_with_repeated_chorus(), audio_duration=100.0)
    types_ = [s.type for s in secs]
    assert "intro" in types_, "el hueco inicial de 8s debe dar un intro"
    assert "puente" in types_, "el hueco instrumental intermedio debe dar un puente"


def test_sections_cover_full_timeline_contiguously():
    dur = 100.0
    secs = scenes.detect_sections(_song_with_repeated_chorus(), audio_duration=dur)
    assert secs[0].start == 0.0
    assert abs(secs[-1].end - dur) < 0.01
    # contiguas, sin huecos ni solapes
    for a, b in zip(secs, secs[1:]):
        assert abs(a.end - b.start) < 0.01, f"hueco/solape entre {a} y {b}"


def test_no_section_shorter_than_min_after_merge():
    secs = scenes.detect_sections(_song_with_repeated_chorus(), audio_duration=100.0)
    # Tras la fusión, ninguna sección baja de su mínimo EFECTIVO (los
    # instrumentales —intro/puente/outro— admiten ser bookends más cortos).
    if len(secs) > 1:
        for s in secs:
            assert s.duration >= scenes._effective_min(s) - 0.01, \
                f"sección {s.type} dura {s.duration:.1f}s < min efectivo {scenes._effective_min(s):.1f}"


def test_empty_segments_yields_single_scene():
    secs = scenes.detect_sections([], audio_duration=120.0)
    assert len(secs) == 1
    assert secs[0].start == 0.0 and secs[0].end == 120.0


def test_cap_unique_scenes_collapses_verses(monkeypatch):
    monkeypatch.setattr(scenes, "MAX_UNIQUE_SCENES", 3)
    monkeypatch.setattr(scenes, "MIN_SCENE_DURATION", 4.0)
    # 6 versos únicos, sin repetición → 6 escenas únicas, debe colapsar a ≤3.
    segs = []
    t = 0.0
    for i in range(6):
        segs.append(_line(t, t + 6.0, f"verso numero {i} totalmente distinto aqui"))
        t += 6.0
    secs = scenes.detect_sections(segs, audio_duration=t)
    unique = {s.recurrence_key for s in secs}
    assert len(unique) <= 3, f"esperaba ≤3 escenas únicas, got {len(unique)}: {unique}"


def test_short_verses_keep_their_own_scene():
    """Regresión 2026-06-18: versos de ~12s (entre el piso duro ≈8s y el
    objetivo blando 14s) NO deben ser absorbidos por el coro. Antes del fix
    esta canción colapsaba a 2 escenas y los versos desaparecían;
    ahora se preserva la progresión de versos."""
    segs = []
    t = 6.0  # intro instrumental de 6s
    def _add4(lines):
        nonlocal t
        for ln in lines:
            segs.append(_line(t, t + 3.0, ln))
            t += 3.0
    _add4(["verso uno linea a", "verso uno linea b", "verso uno linea c", "verso uno linea d"])  # V1 12s
    _add4(["y vuelvo a ti", "siempre vuelvo a ti", "mi corazon te llama", "otra vez a ti"])       # Coro 12s
    _add4(["verso dos linea a", "verso dos linea b", "verso dos linea c", "verso dos linea d"])   # V2 12s
    _add4(["y vuelvo a ti", "siempre vuelvo a ti", "mi corazon te llama", "otra vez a ti"])       # Coro 12s
    secs = scenes.detect_sections(segs, audio_duration=t + 4.0)

    verse_keys = {s.recurrence_key for s in secs if s.type == "verso"}
    chorus_keys = {s.recurrence_key for s in secs if s.type == "coro"}
    # Los dos versos distintos sobreviven como escenas propias.
    assert len(verse_keys) >= 2, \
        f"los versos cortos no deben colapsar en el coro: {[(s.type, s.recurrence_key) for s in secs]}"
    # Los coros idénticos siguen compartiendo una sola escena (rima estructural).
    assert len(chorus_keys) == 1, f"los coros deben compartir escena, got {chorus_keys}"
    # Ninguna sección quedó por debajo del piso duro.
    for s in secs:
        assert s.duration >= scenes._hard_min(s) - 0.01


def test_sections_from_plan_roundtrip():
    """El re-stitch de un edit reconstruye Section desde el plan persistido."""
    secs = scenes.detect_sections(_song_with_repeated_chorus(), audio_duration=100.0)
    plan = {"sections": [s.to_dict() for s in secs]}
    rebuilt = scenes.sections_from_plan(plan)
    assert len(rebuilt) == len(secs)
    for a, b in zip(secs, rebuilt):
        assert (a.type, a.start, a.end, a.recurrence_key) == (b.type, b.start, b.end, b.recurrence_key)
        assert abs(a.duration - b.duration) < 0.01
    # Ignora claves extra (p.ej. duration) sin romper.
    extra = scenes.Section.from_dict({"type": "coro", "start": 0.0, "end": 5.0,
                                      "energy": 0.85, "recurrence_key": "coro_1",
                                      "text": "x", "duration": 999})
    assert extra.duration == 5.0


def test_section_from_dict_tolerates_malformed():
    """Audit LOW: un scene_plan truncado/parcial no debe tumbar el re-stitch."""
    # dict vacío → defaults, sin TypeError.
    s = scenes.Section.from_dict({})
    assert s.type == "verso" and s.start == 0.0 and s.end == 0.0
    # campos parciales / None → coerción segura.
    s2 = scenes.Section.from_dict({"type": "coro", "start": "5", "end": None, "energy": None})
    assert s2.type == "coro" and s2.start == 5.0 and s2.end == 0.0 and s2.energy == 0.5
    # sections_from_plan sobre un plan con sección vacía no levanta.
    assert scenes.sections_from_plan({"sections": [{}, {"type": "coro", "start": 0, "end": 3}]})


def test_build_scene_plan_respects_operator_static(monkeypatch):
    """Bug 2026-06-19: el operador pedía 'estático' pero las escenas se movían
    (el movimiento salía de la energía). Ahora estatico/sutil overridean todo."""
    secs = scenes.detect_sections(_song_with_repeated_chorus(), audio_duration=100.0)
    bible = {"world": "w", "palette": "p", "texture": "t", "camera": "c", "motif": "m"}
    pf = lambda **k: {"style": "video", "prompt": "x"}
    # estatico → TODAS las escenas estaticas (override del energy-derived).
    plan = scenes.build_scene_plan(secs, bible, pf, operator_movement="estatico")
    assert all(s["movement_style"] == "estatico" for s in plan["scenes"])
    # sutil → todas sutiles.
    plan_s = scenes.build_scene_plan(secs, bible, pf, operator_movement="sutil")
    assert all(s["movement_style"] == "sutil" for s in plan_s["scenes"])
    # vacío/estandar → energy-derived (el coro de alta energía NO es estatico).
    plan0 = scenes.build_scene_plan(secs, bible, pf, operator_movement="")
    movs = {s["movement_style"] for s in plan0["scenes"]}
    assert movs != {"estatico"}, "sin override debe variar por energía"


def test_build_scene_plan_respects_operator_animado(monkeypatch):
    """Bug 2026-06-30: el operador elegía 'Animado' (ilustración 2D) y salía
    fotorrealista con paneo, porque 'animado' caía al energy-derived. Es una
    ESTÉTICA, no un nivel de cámara → debe forzarse en TODAS las escenas."""
    secs = scenes.detect_sections(_song_with_repeated_chorus(), audio_duration=100.0)
    bible = {"world": "w", "palette": "p", "texture": "t", "camera": "c", "motif": "m"}
    seen = []
    pf = lambda **k: seen.append(k.get("movement_style")) or {"style": "video", "prompt": "x"}
    plan = scenes.build_scene_plan(secs, bible, pf, operator_movement="animado")
    # Cada escena queda en "animado" (no dinamico/sutil/estatico del energy-derived).
    assert all(s["movement_style"] == "animado" for s in plan["scenes"])
    # Y el prompt_fn recibió "animado" → la estética 2D se hornea en el prompt.
    assert seen and all(m == "animado" for m in seen)


def test_build_scene_plan_respects_operator_foto(monkeypatch):
    """Bug 2026-06-30 (gemelo de animado): 'Foto fija' (foto-parallax) caía al
    energy-derived → salía fotorrealista con paneo. Es una estética → se fuerza
    en TODAS las escenas (foto fija, sin sujetos en movimiento)."""
    secs = scenes.detect_sections(_song_with_repeated_chorus(), audio_duration=100.0)
    bible = {"world": "w", "palette": "p", "texture": "t", "camera": "c", "motif": "m"}
    pf = lambda **k: {"style": "photo", "prompt": "x"}
    plan = scenes.build_scene_plan(secs, bible, pf, operator_movement="foto-parallax")
    assert all(s["movement_style"] == "foto-parallax" for s in plan["scenes"])


def test_energy_to_movement_mapping():
    assert scenes.energy_to_movement(0.9) == "dinamico"
    assert scenes.energy_to_movement(0.5) == "sutil"
    assert scenes.energy_to_movement(0.2) == "estatico"


def test_build_scene_plan_inherits_bible_and_dedups():
    secs = scenes.detect_sections(_song_with_repeated_chorus(), audio_duration=100.0)
    bible = {
        "world": "neon-soaked rainy metropolis at night",
        "palette": "deep blues and magenta",
        "texture": "35mm grain, anamorphic",
        "camera": "slow deliberate moves",
        "motif": "reflections on wet asphalt",
    }
    calls = []

    def fake_prompt_fn(background_hint="", movement_style="", section_type="", energy=0.0):
        calls.append(background_hint)
        return {"style": "video", "prompt": background_hint + " | rendered"}

    plan = scenes.build_scene_plan(secs, bible, fake_prompt_fn, artist="X", song_title="Y")
    # Una escena por recurrence_key única (no por sección).
    unique_keys = {s.recurrence_key for s in secs}
    assert len(plan["scenes"]) == len(unique_keys)
    # Toda escena heredó la biblia (el "mundo" aparece en el hint enviado).
    assert all("neon-soaked rainy metropolis" in c for c in calls)
    assert plan["bible"] == bible


def test_scene_hint_carries_coherence_not_texture_or_camera():
    """La biblia sólo inyecta MUNDO/PALETA/MOTIVO en el hint por-escena; la
    textura y la cinematografía las deriva el motor por-escena desde la canción
    (como Auto/Inspirado). Regresión 2026-06-19: inyectar texture="16mm film
    grain" propagaba el sesgo a TODAS las escenas y dibujaba un fotograma físico."""
    secs = scenes.detect_sections(_song_with_repeated_chorus(), audio_duration=100.0)
    bible = {
        "world": "neon-soaked rainy metropolis at night",
        "palette": "deep blues and magenta",
        "texture": "gritty 16mm film grain, sprocket holes",   # NO debe inyectarse
        "camera": "handheld camcorder, slow zoom",             # NO debe inyectarse
        "motif": "reflections on wet asphalt",
    }
    calls = []

    def fake_prompt_fn(background_hint="", movement_style="", section_type="", energy=0.0):
        calls.append(background_hint)
        return {"style": "video", "prompt": "ok"}

    scenes.build_scene_plan(secs, bible, fake_prompt_fn)
    blob = " ".join(calls).lower()
    # Coherencia presente.
    assert "neon-soaked rainy metropolis" in blob and "deep blues" in blob
    assert "reflections on wet asphalt" in blob
    # Textura/cámara de la biblia AUSENTES del hint (no se imponen).
    for leaked in ["16mm", "film grain", "sprocket", "camcorder"]:
        assert leaked not in blob, f"la biblia filtró {leaked!r} al hint por-escena"
