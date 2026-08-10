"""Regresiones del cableado de los dos flags de producto de #1086.

Los dos flags estaban bien implementados y mal conectados, que es el peor de
los dos mundos: los tests unitarios pasaban y en producción no hacían nada.

  1. `build_scene_plan(tenant_id=...)` existía pero NINGÚN caller lo pasaba, así
     que `default_movement_for_tenant("")` devolvía siempre "". El canary por
     tenant era inalcanzable: sólo `BG_DEFAULT_MOVEMENT_TENANTS=*` — o sea,
     prenderlo para todos los clientes a la vez — tenía efecto.

  2. El stripper de puntos finales corría sólo en el worker async. Los dos
     caminos síncronos de transcripción (`/transcribe` legacy y
     `/transcribe-uploaded` con `ASYNC_TRANSCRIBE_ENABLED=0`) entregaban el
     texto sin pasar por él → el mismo tenant recibía puntos o no según qué
     endpoint lo atendiera.

Ambos se verifican sobre el código real (wiring), no sobre la función pura:
`test_movement_default.py` y `test_strip_trailing_periods.py` ya cubren la
lógica.
"""

import ast
import inspect

import pipeline
import scenes


# ---------------------------------------------------------------------------
# 1. tenant_id llega a build_scene_plan
# ---------------------------------------------------------------------------

def test_el_pipeline_le_pasa_el_tenant_a_build_scene_plan(monkeypatch, tmp_path):
    """El canary por tenant tiene que ser alcanzable desde el render real."""
    visto = {}

    monkeypatch.setattr(scenes, "detect_sections",
                        lambda segs, dur: [scenes.Section(
                            type="verso", start=0.0, end=10.0, energy=0.9,
                            recurrence_key="v1")])
    monkeypatch.setattr(pipeline, "_build_visual_bible",
                        lambda *a, **kw: {})
    monkeypatch.setattr(pipeline, "_make_scene_prompt_fn",
                        lambda *a, **kw: (lambda **kw2: {"prompt": "p"}))
    monkeypatch.setattr(pipeline, "_generate_scene_clips",
                        lambda plan, *a, **kw: {"v1": str(tmp_path / "c.mp4")})
    monkeypatch.setattr(pipeline, "_tenant_of_job",
                        lambda job_id: "universal_argentina")
    monkeypatch.setattr(scenes, "stitch_timeline",
                        lambda *a, **kw: str(tmp_path / "t.mp4"))

    _real_plan = scenes.build_scene_plan

    def _spy(*a, **kw):
        visto["tenant_id"] = kw.get("tenant_id")
        return _real_plan(*a, **kw)

    monkeypatch.setattr(scenes, "build_scene_plan", _spy)

    pipeline._generate_scene_background(
        [{"start": 0.0, "end": 10.0, "text": "linea"}], 10.0, str(tmp_path),
        style_hint="cinematic", lyrics_text="linea", artist="A",
        job_id="job-1",
    )

    assert visto.get("tenant_id") == "universal_argentina", (
        "sin el tenant, default_movement_for_tenant siempre devuelve '' y el "
        "canary por cliente no existe"
    )


def test_el_canary_por_tenant_cambia_el_plan_de_punta_a_punta(monkeypatch, tmp_path):
    """El test de arriba prueba el cableado; éste prueba el EFECTO: con el flag
    prendido para ese tenant, un estribillo con energía alta sale quieto."""
    monkeypatch.setattr(scenes, "DEFAULT_MOVEMENT_WHEN_AUTO", "estatico")
    monkeypatch.setattr(scenes, "DEFAULT_MOVEMENT_TENANTS",
                        frozenset({"universal_argentina"}))

    def _run(tenant):
        planes = {}
        monkeypatch.setattr(scenes, "detect_sections",
                            lambda segs, dur: [scenes.Section(
                                type="coro", start=0.0, end=10.0, energy=0.95,
                                recurrence_key="c1")])
        monkeypatch.setattr(pipeline, "_build_visual_bible", lambda *a, **kw: {})
        monkeypatch.setattr(pipeline, "_make_scene_prompt_fn",
                            lambda *a, **kw: (lambda **kw2: {"prompt": "p"}))
        monkeypatch.setattr(pipeline, "_generate_scene_clips",
                            lambda plan, *a, **kw: planes.update(plan=plan)
                            or {"c1": str(tmp_path / "c.mp4")})
        monkeypatch.setattr(pipeline, "_tenant_of_job", lambda job_id: tenant)
        monkeypatch.setattr(scenes, "stitch_timeline",
                            lambda *a, **kw: str(tmp_path / "t.mp4"))
        pipeline._generate_scene_background(
            [{"start": 0.0, "end": 10.0, "text": "linea"}], 10.0,
            str(tmp_path), style_hint="cinematic", lyrics_text="linea",
            artist="A", job_id="job-1")
        return [s["movement_style"] for s in planes["plan"]["scenes"]]

    assert _run("universal_argentina") == ["estatico"]
    # El resto de los clientes no se entera: eso es lo que hace que sea canary.
    assert _run("otro_sello") == ["dinamico"]


def test_tenant_of_job_nunca_levanta(monkeypatch):
    """Un flag de producto que no se puede resolver cae al default (apagado),
    nunca tumba el render."""
    class _Boom:
        def __call__(self, *a, **kw):
            raise RuntimeError("db caida")

    monkeypatch.setattr("database.SessionLocal", _Boom())
    assert pipeline._tenant_of_job("job-1") == ""
    # job_id vacío ni siquiera toca la DB.
    assert pipeline._tenant_of_job("") == ""
    assert pipeline._tenant_of_job(None) == ""


# ---------------------------------------------------------------------------
# 2. Los caminos síncronos de transcripción también corren el stripper
# ---------------------------------------------------------------------------

def _funcs_que_llaman(modulo, nombre_llamada):
    """Nombres de las funciones del módulo cuyo cuerpo llama a `nombre_llamada`."""
    tree = ast.parse(inspect.getsource(modulo))
    out = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for sub in ast.walk(node):
            if (isinstance(sub, ast.Call)
                    and isinstance(sub.func, ast.Name)
                    and sub.func.id == nombre_llamada):
                out.add(node.name)
    return out


def test_todo_camino_que_formatea_letra_tambien_saca_los_puntos():
    """Contrato: el texto entregado no puede depender de qué endpoint corrió.

    Toda función que llama al format pass (`_fmt`) tiene que llamar también al
    stripper (`_strip_dots`). Antes de este fix, las dos funciones síncronas de
    main.py llamaban a la primera y no a la segunda.
    """
    import main

    for modulo in (main, __import__("transcription_worker")):
        formatean = _funcs_que_llaman(modulo, "_fmt")
        strippean = _funcs_que_llaman(modulo, "_strip_dots")
        assert formatean, f"{modulo.__name__}: no encontré el format pass"
        assert formatean <= strippean, (
            f"{modulo.__name__}: {sorted(formatean - strippean)} formatea la "
            "letra pero no saca los puntos finales — el resultado dependería "
            "del endpoint"
        )


def test_el_stripper_sincrono_recibe_el_tenant_del_usuario():
    """No alcanza con llamarlo: si no le pasa el tenant, el gate por cliente
    queda apagado siempre y el flag no hace nada por ese camino."""
    import main

    src = inspect.getsource(main)
    llamadas = [ln for ln in src.splitlines() if "_strip_dots(" in ln
                and "import" not in ln]
    assert len(llamadas) >= 2, (
        f"esperaba los dos caminos síncronos, encontré {len(llamadas)}")
    ctx = src.split("_strip_dots(")
    for bloque in ctx[1:]:
        assert "tenant_id=" in bloque[:200], (
            "el stripper síncrono se llamó sin tenant_id → gate siempre off")


# ---------------------------------------------------------------------------
# 3. El default de tenant vale para TODOS los caminos de fondo
# ---------------------------------------------------------------------------

def test_el_default_se_resuelve_antes_de_elegir_el_camino():
    """El default vivía sólo dentro de `build_scene_plan`, así que un tenant
    habilitado SIN el add-on de Escenas —o cuyo multi-escena falla y cae al
    fondo único— seguía recibiendo el Auto en movimiento, que es justo lo que
    el flag existe para sacar.

    Se verifica sobre el código: `_bg_movement` se resuelve antes del `if`
    que elige escenas, y los tres caminos de fondo lo usan.
    """
    src = inspect.getsource(pipeline.run_pipeline)
    i_resuelve = src.index("_bg_movement = _sc_default.effective_movement_for_tenant")
    i_ramifica = src.index("and enable_scenes")
    assert i_resuelve < i_ramifica, (
        "el default tiene que resolverse ANTES de elegir escenas vs fondo único")
    # Los tres generadores y la validación de cache lo usan; ninguno se quedó
    # con el valor crudo.
    assert src.count("movement_style=_bg_movement") == 4, (
        "algún camino de fondo sigue recibiendo el movement_style sin resolver")


def test_el_default_no_pisa_la_eleccion_guardada_del_operador():
    """`render_params.movement_style` guarda lo que eligió el OPERADOR y el
    editor lo pinta desde ahí. Si el default lo sobrescribiera, el operador
    vería "Estático" donde había elegido "Auto"."""
    src = inspect.getsource(pipeline.run_pipeline)
    assert '"movement_style": _normalize_movement_style(movement_style)' in src, (
        "render_params tiene que seguir guardando la elección cruda del "
        "operador, no el default resuelto")


def test_la_validacion_de_cache_usa_el_movimiento_efectivo():
    """Preview y render deben hashear el asset que realmente generan. Usar el
    Auto crudo permitiría reutilizar un video móvil en el tenant canary."""
    src = inspect.getsource(pipeline.run_pipeline)
    i_val = src.index("_validate_bg_cache_key(")
    bloque = src[i_val:i_val + 600]
    assert "movement_style=_bg_movement" in bloque


def test_el_cache_separa_auto_historico_del_default_del_tenant(monkeypatch):
    import bg_preview

    monkeypatch.setattr(scenes, "DEFAULT_MOVEMENT_WHEN_AUTO", "estatico")
    monkeypatch.setattr(scenes, "DEFAULT_MOVEMENT_TENANTS",
                        frozenset({"universal_argentina"}))
    base = {"artist": "Bersuit", "song_title": "La Argentinidad",
            "style": "auto", "movement_style": ""}

    canary = bg_preview.compute_bg_cache_key(
        {**base, "_tenant_id": "universal_argentina"})
    historical = bg_preview.compute_bg_cache_key(
        {**base, "_tenant_id": "otro_sello"})
    explicit_static = bg_preview.compute_bg_cache_key(
        {**base, "movement_style": "estatico", "_tenant_id": "otro_sello"})

    assert canary != historical
    assert canary == explicit_static


def test_el_worker_de_preview_genera_con_el_movimiento_efectivo(monkeypatch):
    import os

    import bg_preview
    import jobs

    monkeypatch.setattr(scenes, "DEFAULT_MOVEMENT_WHEN_AUTO", "estatico")
    monkeypatch.setattr(scenes, "DEFAULT_MOVEMENT_TENANTS",
                        frozenset({"universal_argentina"}))
    monkeypatch.setattr(bg_preview, "cache_check", lambda _key: False)
    monkeypatch.setattr(bg_preview, "cache_put", lambda key, _path: key)
    monkeypatch.setattr(jobs, "update_job", lambda *a, **k: None)
    monkeypatch.setattr(pipeline, "_compute_allow_people", lambda *a, **k: False)
    monkeypatch.setattr(pipeline, "_raise_if_job_timeout", lambda _e: None)
    monkeypatch.setattr(pipeline, "_validate_background_asset_for_job",
                        lambda *a, **k: True)
    captured = {}

    def _fake_ensure(_style, job_dir, **kwargs):
        captured["movement_style"] = kwargs.get("movement_style")
        path = os.path.join(job_dir, "preview.mp4")
        with open(path, "w") as output:
            output.write("preview")
        return path

    monkeypatch.setattr(pipeline, "_ensure_background", _fake_ensure)
    params = {
        "artist": "Bersuit", "song_title": "La Argentinidad",
        "style": "auto", "movement_style": "",
        "_tenant_id": "universal_argentina",
    }
    from background_policy import runtime_rollout_fingerprint
    key = bg_preview.compute_bg_cache_key(params)
    result = bg_preview.run_bg_preview_job(
        "previewcanary", key, params, runtime_rollout_fingerprint())
    assert result["status"] == "bg_preview_done"
    assert captured["movement_style"] == "estatico"


def test_las_ediciones_reaplican_el_default_del_tenant():
    src = inspect.getsource(pipeline.run_edit_pipeline)
    assert "effective_movement_for_tenant" in src
    assert src.count("movement_style=_edit_bg_movement") == 3
    assert "_normalize_movement_style(_edit_bg_movement)" in src
