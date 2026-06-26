"""Tests de la orquestación multi-escena en pipeline.py.

Verificables sin Veo/Gemini reales: el fallback determinista de la biblia
visual y la degradación elegante de la generación de clips (si una escena
falla, se sustituye por una válida en vez de tumbar el video).
"""
import os

import pipeline


def test_visual_bible_fallback_when_gemini_unavailable(monkeypatch):
    def boom():
        raise RuntimeError("no gemini en CI")
    monkeypatch.setattr(pipeline, "_get_genai_client", boom)
    bible = pipeline._build_visual_bible(
        "una letra cualquiera", "Artista", style="neon", genre="rock")
    # Siempre devuelve las 5 claves del look book, no-vacías.
    assert set(bible) == {"world", "palette", "texture", "camera", "motif"}
    assert all(isinstance(v, str) and v.strip() for v in bible.values())
    # La paleta del fallback refleja el style elegido.
    assert "neon" in bible["palette"].lower()
    # El fallback determinista NO debe traer sesgo de film/calibre (no pasa por
    # el sanitizador): un fallback con "16mm/film grain" reintroduce el bug.
    blob = " ".join(bible.values()).lower()
    for bad in ["16mm", "35mm", "film grain", "film stock", "vhs"]:
        assert bad not in blob, f"fallback sesgado contiene {bad!r}: {blob}"


def test_parse_json_object_tolerant():
    assert pipeline._parse_json_object('{"a": 1}') == {"a": 1}
    assert pipeline._parse_json_object('```json\n{"a": 2}\n```') == {"a": 2}
    assert pipeline._parse_json_object("basura sin json") is None
    assert pipeline._parse_json_object("") is None


def test_visual_bible_disables_thinking_and_forces_json(monkeypatch):
    """Regresión 2026-06-18: gemini-2.5-flash es un modelo de *thinking*. Con la
    config vieja (max_output_tokens=500, thinking ON) el JSON de la biblia salía
    TRUNCADO (finish_reason=MAX_TOKENS, ~478 tokens gastados en thinking) → parse
    fallaba → TODA biblia caía al fallback genérico. Confirmado contra Vertex
    real. Este test fija la config que lo evita: thinking apagado, JSON forzado
    y margen de tokens suficiente."""
    captured = {}

    class _Resp:
        text = '{"world":"w","palette":"p","texture":"t","camera":"c","motif":"m"}'

    class _Models:
        def generate_content(self, model, contents, config):
            captured["config"] = config
            return _Resp()

    class _Client:
        models = _Models()

    monkeypatch.setattr(pipeline, "_get_genai_client", lambda: _Client())
    bible = pipeline._build_visual_bible("una letra", "Artista", genre="rock", style="neon")
    # Usó la respuesta de Gemini (no el fallback).
    assert bible["world"] == "w" and bible["motif"] == "m"
    cfg = captured["config"]
    # thinking apagado (si no, vuelve la truncación).
    assert cfg.thinking_config is not None and cfg.thinking_config.thinking_budget == 0
    # JSON puro forzado (sin ```json a parsear).
    assert cfg.response_mime_type == "application/json"
    # margen de tokens por encima del viejo 500 que truncaba.
    assert (cfg.max_output_tokens or 0) >= 800


def test_sanitize_bible_strips_film_formats():
    """Regresión 2026-06-19 (job e5bbc6a63861, Intoxicados): la biblia traía
    texture='16mm film grain' → Veo dibujaba el fotograma físico (sprockets,
    marco negro, UI falsa). El sanitizador saca el calibre/formato pero conserva
    el grano como mood."""
    dirty = {
        "world": "decaying greenhouse",
        "texture": "Gritty, desaturated 16mm film grain with chromatic aberration",
        "camera": "Super 8 found-footage handheld, VHS camcorder viewfinder",
        "palette": "muted greens",
        "motif": "dew drops",
    }
    clean = pipeline._sanitize_bible_film_formats(dirty)
    blob = " ".join(clean.values()).lower()
    # Ningún token de formato/calibre/UI sobrevive.
    for bad in ["16mm", "35mm", "super 8", "vhs", "camcorder", "viewfinder",
                "found-footage", "found footage", "sprocket", "film strip"]:
        assert bad not in blob, f"sobrevivió {bad!r}: {blob}"
    # El grano se preserva como mood (no se borra entero el campo).
    assert "grain" in clean["texture"].lower()
    # Campos sin formato quedan intactos.
    assert clean["world"] == "decaying greenhouse" and clean["motif"] == "dew drops"


def test_visual_bible_sanitizes_film_format_from_gemini(monkeypatch):
    """End-to-end: aunque Gemini emita '16mm film grain', la biblia devuelta ya
    viene limpia (el sanitizador corre post-parse, no depende del LLM)."""
    class _Resp:
        text = ('{"world":"w","palette":"p","texture":"desaturated 16mm film grain",'
                '"camera":"slow dolly","motif":"m"}')

    class _Models:
        def generate_content(self, model, contents, config):
            return _Resp()

    class _Client:
        models = _Models()

    monkeypatch.setattr(pipeline, "_get_genai_client", lambda: _Client())
    bible = pipeline._build_visual_bible("una letra", "Artista", genre="rock", style="neon")
    assert "16mm" not in bible["texture"].lower()
    assert "grain" in bible["texture"].lower()  # mood preservado


def test_scene_clips_graceful_degradation(monkeypatch, tmp_path):
    import veo_breaker
    monkeypatch.setattr(veo_breaker, "is_open", lambda: False)

    def fake_veo(prompt, output_path, **kw):
        # verso_2 falla; el resto genera bien (escribe el archivo).
        if "verso_2" in (kw.get("cache_namespace") or ""):
            raise RuntimeError("veo boom")
        with open(output_path, "w") as f:
            f.write("clip")
        return output_path

    monkeypatch.setattr(pipeline, "_generate_veo_video", fake_veo)
    plan = {"scenes": [
        {"recurrence_key": "coro_1", "prompt": "p", "movement_style": "dinamico"},
        {"recurrence_key": "verso_1", "prompt": "p", "movement_style": "sutil"},
        {"recurrence_key": "verso_2", "prompt": "p", "movement_style": "sutil"},
    ]}
    clip_for_key = pipeline._generate_scene_clips(
        plan, str(tmp_path), artist="A", song_title="S", job_id=None)
    # Todas las escenas quedan mapeadas (la fallida reusa un clip válido).
    assert set(clip_for_key) == {"coro_1", "verso_1", "verso_2"}
    assert all(os.path.exists(p) for p in clip_for_key.values())
    # La escena fallida quedó marcada y apunta a un clip existente.
    failed = next(s for s in plan["scenes"] if s["recurrence_key"] == "verso_2")
    assert failed["status"] == "failed"


def test_scene_clips_all_fail_raises(monkeypatch, tmp_path):
    import veo_breaker
    monkeypatch.setattr(veo_breaker, "is_open", lambda: False)

    def fake_veo(prompt, output_path, **kw):
        raise RuntimeError("veo caído")

    monkeypatch.setattr(pipeline, "_generate_veo_video", fake_veo)
    plan = {"scenes": [{"recurrence_key": "coro_1", "prompt": "p", "movement_style": "x"}]}
    # Si ninguna escena genera, levanta → run_pipeline cae al fondo único.
    import pytest
    with pytest.raises(RuntimeError):
        pipeline._generate_scene_clips(plan, str(tmp_path), artist="A", song_title="S")


def _two_scene_plan():
    return {
        "bible": {"world": "w", "palette": "p", "texture": "t", "camera": "c", "motif": "m"},
        "sections": [
            {"type": "verso", "start": 0.0, "end": 18.0, "energy": 0.5, "recurrence_key": "verso_1", "text": ""},
            {"type": "coro", "start": 18.0, "end": 36.0, "energy": 0.85, "recurrence_key": "coro_1", "text": ""},
        ],
        "scenes": [
            {"recurrence_key": "verso_1", "section_type": "verso", "energy": 0.5,
             "movement_style": "sutil", "prompt": "P1", "status": "generated", "cache_token": ""},
            {"recurrence_key": "coro_1", "section_type": "coro", "energy": 0.85,
             "movement_style": "dinamico", "prompt": "P2", "status": "generated", "cache_token": ""},
        ],
    }


def test_regenerate_scene_busts_only_target(monkeypatch, tmp_path):
    import veo_breaker
    import scenes
    monkeypatch.setattr(veo_breaker, "is_open", lambda: False)
    calls = []

    def fake_veo(prompt, output_path, **kw):
        calls.append({"ns": kw.get("cache_namespace"), "prompt": prompt,
                      "cache_only": kw.get("cache_only", False)})
        with open(output_path, "w") as f:
            f.write("clip")
        return output_path

    monkeypatch.setattr(pipeline, "_generate_veo_video", fake_veo)
    # Evita ffmpeg real en el test offline.
    monkeypatch.setattr(scenes, "stitch_timeline", lambda *a, **k: "/tmp/timeline.mp4")

    plan = _two_scene_plan()
    tl, newplan = pipeline._regenerate_scene_background(
        plan, "coro_1", str(tmp_path), artist="A", song_title="S",
        audio_duration=36.0, prompt_override="NUEVO PROMPT")

    coro = next(s for s in newplan["scenes"] if s["recurrence_key"] == "coro_1")
    verso = next(s for s in newplan["scenes"] if s["recurrence_key"] == "verso_1")
    # La target tomó el prompt nuevo y un cache_token nuevo (bust); la otra no.
    assert coro["prompt"] == "NUEVO PROMPT"
    assert coro["cache_token"] and coro["cache_token"] != ""
    assert verso["cache_token"] == ""
    # GARANTÍA DE COSTO (audit B2): la TARGET genera fresco (cache_only=False),
    # la NO-target es cache_only=True (nunca re-cobra Veo).
    coro_call = next(c for c in calls if c["prompt"] == "NUEVO PROMPT")
    verso_call = next(c for c in calls if c["prompt"] == "P1")
    assert coro_call["cache_only"] is False
    assert verso_call["cache_only"] is True
    assert coro["cache_token"] in coro_call["ns"]
    assert verso_call["ns"] == "A|S|verso_1"
    assert tl == "/tmp/timeline.mp4"


def test_veo_cache_only_raises_on_miss_never_bills(monkeypatch):
    """Audit B2: con cache_only=True y la caché vacía, _generate_veo_video LEVANTA
    (no genera fresco) → un regen de 1 escena no puede re-cobrar las otras N."""
    import storage as _storage
    # Caché disponible pero SIN el objeto → debe levantar, no pagar Veo.
    monkeypatch.setattr(_storage, "is_enabled", lambda: True)
    monkeypatch.setattr(_storage, "object_exists", lambda *a, **k: False)
    # Si llegara a intentar generar, esto lo delataría (no debería llamarse).
    import pytest
    with pytest.raises(RuntimeError, match="cache_only"):
        pipeline._generate_veo_video("un prompt", "/tmp/should_not_exist.mp4",
                                     job_id=None, cache_only=True)


def test_regenerate_scene_unknown_key_raises(monkeypatch, tmp_path):
    import scenes
    monkeypatch.setattr(scenes, "stitch_timeline", lambda *a, **k: "/tmp/t.mp4")
    import pytest
    with pytest.raises(ValueError):
        pipeline._regenerate_scene_background(
            _two_scene_plan(), "no_existe", str(tmp_path),
            artist="A", song_title="S", audio_duration=36.0)


def test_restitch_for_edit_realigns_when_structure_unchanged(monkeypatch, tmp_path):
    """Audit M1: un edit de lyrics que NO cambia la estructura de recurrencia
    re-stitchea desde clips cacheados (cache_only, sin Veo) y re-alinea cortes."""
    import veo_breaker, scenes
    monkeypatch.setattr(veo_breaker, "is_open", lambda: False)
    veo_calls = []

    def fake_veo(prompt, output_path, **kw):
        veo_calls.append(kw.get("cache_only", False))
        with open(output_path, "w") as f:
            f.write("clip")
        return output_path

    monkeypatch.setattr(pipeline, "_generate_veo_video", fake_veo)
    captured = {}
    monkeypatch.setattr(scenes, "stitch_timeline",
                        lambda secs, *a, **k: (captured.update(secs=secs) or "/tmp/tl.mp4"))

    plan = {
        "scenes": [
            {"recurrence_key": "verso_1", "prompt": "P1", "movement_style": "sutil"},
            {"recurrence_key": "coro_1", "prompt": "P2", "movement_style": "dinamico"},
        ],
        "sections": [],
    }
    # Letra: verso (único) + coro (repetido) → keys verso_1 + coro_1 (subset de have).
    segs, t = [], 0.0
    for ln in ["camino solo en la noche", "busco una señal aqui",
               "y vuelvo a ti otra vez", "no hay nadie mas que vos",
               "y vuelvo a ti otra vez", "no hay nadie mas que vos"]:
        segs.append({"text": ln, "start": t, "end": t + 5.0}); t += 5.0

    tl, newplan = pipeline._restitch_scenes_for_edit(
        plan, segs, t, str(tmp_path), artist="A", song_title="S")
    assert tl == "/tmp/tl.mp4"
    # CERO Veo pagado: todas las llamadas fueron cache_only.
    assert veo_calls and all(c is True for c in veo_calls)
    # Las sections del plan se actualizaron (re-detectadas).
    assert newplan["sections"], "debe re-detectar y persistir las nuevas secciones"


def test_restitch_for_edit_bails_when_structure_changed(tmp_path):
    """Si la edición cambia la estructura (key nueva sin clip), NO re-stitch:
    devuelve (None, plan) y el caller usa el timeline cacheado estático."""
    plan = {"scenes": [{"recurrence_key": "coro_1", "prompt": "p"}], "sections": []}
    # Letra sin repeticiones → detect produce verso_1 (no coro_1) → no subset.
    segs = [{"text": f"linea distinta numero {i}", "start": i * 6.0, "end": i * 6.0 + 6.0}
            for i in range(4)]
    tl, newplan = pipeline._restitch_scenes_for_edit(
        plan, segs, 24.0, str(tmp_path), artist="A", song_title="S")
    assert tl is None


def test_scene_cache_ns_token():
    assert pipeline._scene_cache_ns("A", "S", "coro_1") == "A|S|coro_1"
    assert pipeline._scene_cache_ns("A", "S", "coro_1", "ab12") == "A|S|coro_1|ab12"


def test_scene_clips_breaker_open_raises(monkeypatch, tmp_path):
    import veo_breaker
    monkeypatch.setattr(veo_breaker, "is_open", lambda: True)
    plan = {"scenes": [{"recurrence_key": "coro_1", "prompt": "p", "movement_style": "x"}]}
    import pytest
    with pytest.raises(RuntimeError):
        pipeline._generate_scene_clips(plan, str(tmp_path), artist="A", song_title="S")
