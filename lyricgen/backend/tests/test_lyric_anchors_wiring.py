"""Cableado del modo anclado dentro de pipeline.py.

El test que más importa acá es el primero: con BG_LYRIC_ANCHORS apagado (el
default de despliegue) el prompt tiene que salir BIT-IDÉNTICO al de siempre.
Mergear este cambio no puede mover producción; prenderlo es una decisión
separada y explícita.

Se analiza el fuente con AST/texto en vez de ejecutar la pipeline, igual que
test_match_lyrics_feature.py: importar pipeline entero arrastra Veo, Vertex,
moviepy y ffmpeg.
"""

import ast
import contextlib
import hashlib
import io
import json
import os
import re

import lyric_anchors as la

_PIPELINE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         "pipeline.py")
_SRC = io.open(_PIPELINE, encoding="utf-8").read()
_TREE = ast.parse(_SRC)


def _func(name):
    for node in ast.walk(_TREE):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{name} no existe en pipeline.py")


def _body(name):
    node = _func(name)
    return ast.get_source_segment(_SRC, node) or ""


def _args(name):
    node = _func(name)
    return [a.arg for a in node.args.args + node.args.kwonlyargs]


# ── Neutralidad con el flag apagado ────────────────────────────────────────
def test_el_camino_anclado_exige_las_tres_condiciones():
    """`_use_anchors` = anclas presentes AND modo letra AND flag en `on`.

    Las tres son necesarias: sin anclas no hay qué anclar, en Auto la letra no
    debe influir y con "Mi prompt" manda el operador.
    """
    body = _body("_analyze_lyrics_for_background")
    match = re.search(r"_use_anchors = bool\((.*?)\n    \)", body, re.DOTALL)
    assert match, "no se encontró la definición de _use_anchors"
    expr = match.group(1)
    assert "anchors" in expr
    assert 'creative_mode == "lyrics"' in expr
    assert "lyric_anchors.anchors_enabled()" in expr


def test_flag_apagado_no_activa_nada():
    assert la.anchors_enabled(la.anchors_mode({})) is False
    assert la.anchors_observed(la.anchors_mode({})) is False


def test_la_extraccion_no_corre_con_el_flag_apagado():
    """El paso 1 se gatea por `anchors_observed`, que es False en `off`.

    Si no, cada render pagaría una llamada Gemini extra sin usarla.
    """
    for fn in ("_ensure_background", "_generate_scene_background"):
        body = _body(fn)
        assert "_extract_lyric_anchors(" in body, fn
        guard = re.search(
            r'if creative_mode == "lyrics" and lyric_anchors\.anchors_observed\(\):',
            body)
        assert guard, f"{fn}: la extracción no está gateada por el flag"


def test_los_negativos_aflojados_solo_aplican_al_camino_anclado():
    """`_anchored_negatives` viene de `song_negatives is not None`.

    Con el flag apagado `song_negatives` es None, así que la lista fija de
    negativos queda exactamente como estaba para Auto, "Mi prompt" y todo lo
    demás.
    """
    body = _body("_generate_veo_video")
    assert "_anchored_negatives = song_negatives is not None" in body
    # La rama vieja se conserva textualmente.
    assert '"no banners, no graffiti, no shop windows, no street signs, no neon "' in body
    # Y la nueva sólo existe bajo el gate.
    assert "physical objects ONLY when completely blank" in body


def test_song_negatives_es_none_no_lista_vacia_en_el_camino_viejo():
    """`or []` acá rompería la neutralidad: `[]` no es None y prendería
    `_anchored_negatives` para todos los modos."""
    body = _body("_ensure_background")
    assert "_song_negatives: list[str] | None = None" in body
    assert '_song_negatives = result.get("negatives")' in body
    assert '_song_negatives = result.get("negatives") or []' not in body


# ── Contrato del cableado ──────────────────────────────────────────────────
def test_anchors_se_propaga_por_toda_la_cadena():
    for fn in ("_analyze_lyrics_for_background", "_get_unique_prompt",
               "_make_scene_prompt_fn"):
        assert "anchors" in _args(fn), fn


def test_las_anclas_se_extraen_una_sola_vez_por_fondo():
    """Extraerlas por escena costaría N llamadas y, peor, cada escena podría
    anclar en un objeto distinto de la misma canción."""
    for fn in ("_ensure_background", "_generate_scene_background"):
        assert _body(fn).count("_extract_lyric_anchors(") == 1, fn


def test_el_reroll_de_calidad_reusa_las_mismas_anclas():
    """Si el re-roll re-extrajera, el segundo intento podría perseguir un ancla
    distinta y "me cambió todo" sería la queja."""
    body = _body("_ensure_background")
    assert body.count("anchors=_lyric_anchors_data") == 3  # imagen + veo + retry


def test_el_reroll_refresca_negativos_y_cobertura():
    """El re-roll re-deriva el prompt; sin refrescar, el segundo intento viajaría
    con los negativos del primero."""
    body = _body("_ensure_background")
    retry = body[body.rfind("quality_retry_used = True"):]
    assert '_song_negatives = result.get("negatives")' in retry
    assert '_anchor_cov = result.get("anchor_coverage")' in retry


# ── El gate de calidad nuevo ───────────────────────────────────────────────
def test_la_cobertura_baja_dispara_el_reroll():
    """El `score < 7` nunca disparó en 204 mediciones (promedio 9,06) porque
    compara el frame contra el prompt que el propio sistema generó. La cobertura
    compara el prompt contra lo que dice la canción."""
    body = _body("_ensure_background")
    assert "_anchors_thin" in body
    assert "lyric_anchors.coverage_is_sufficient(_anchor_cov)" in body
    assert re.search(r"_needs_retry = \(\s*\n\s*\(score < 7\).*_anchors_thin",
                     body, re.DOTALL)


def test_la_cobertura_se_mide_tambien_en_shadow():
    """Shadow tiene que dar la comparación pareada contra el motor viejo sin
    cambiar ninguna salida."""
    body = _body("_analyze_lyrics_for_background")
    assert "lyric_anchors.anchors_observed()" in body
    assert "anchor_coverage" in body


# ── Formato largo ──────────────────────────────────────────────────────────
def test_el_camino_anclado_sube_el_techo_de_tokens():
    """260-360 palabras más una lista de negativos no entran en 1500 tokens: el
    JSON se cortaba a mitad de frase."""
    body = _body("_analyze_lyrics_for_background")
    assert "max_output_tokens=2600 if _use_anchors else 1500" in body


def test_el_camino_anclado_fuerza_json_estricto():
    """16 de 723 llamadas históricas murieron en parse y cayeron a una escena de
    stock aleatoria sin que nada lo marcara."""
    body = _body("_analyze_lyrics_for_background")
    assert re.search(r"policy_enforces\(atmospherics_policy\) or _use_anchors",
                     body)


def _anchored_prompt(**over):
    """El system prompt anclado REAL, no el fuente. Evita falsos positivos con
    los comentarios que explican qué motivos se sacaron y por qué."""
    import pipeline
    kwargs = dict(
        clause2=pipeline._strip_content_examples(
            "framing only — the viewpoint is LOCKED. CRITICAL: motion lives "
            "WITHIN the scene and MUST be RICH — describe AT LEAST 3 distinct "
            "motion sources, e.g.: moving reflections + flickering candle + "
            "dust motes. A scene with zero movement is INVALID"),
        people_rule=pipeline._ANCHORED_PEOPLE_RULE,
        concept="", concept_guide="", genre="rock",
        for_provider="veo", movement_rule="",
    )
    kwargs.update(over)
    return pipeline._anchored_scene_system_prompt(**kwargs)


def test_el_compositor_anclado_no_lleva_ejemplos_de_escena():
    """Ni del bloque de escenas ni de las cláusulas reusadas. Son literalmente
    las frases que aparecían copiadas en los prompts entregados: 59% golden
    hour, 29% niebla, 6,7% "dust motes"."""
    prompt = _anchored_prompt()
    for motivo in ("dust motes", "rain-slicked", "misty mountain", "cinematic 4k",
                   "stormy desert highway", "gauze curtains", "empty bar at dawn",
                   "falling petals", "empty chair", "coffee cups",
                   "northern lights", "neon"):
        assert motivo not in prompt, f"el compositor anclado menciona {motivo!r}"


def test_el_atardecer_solo_aparece_como_prohibicion():
    """"golden hour" SÍ está, pero del lado de la prohibición: era el default
    por descarte en el 59% de los fondos medidos. Que aparezca como sugerencia
    es exactamente la regresión que este test cuida."""
    prompt = _anchored_prompt()
    for match in re.finditer(r"golden hour", prompt):
        contexto = prompt[max(0, match.start() - 120):match.start()]
        assert "no caigas" in contexto or "No inventes" in contexto, (
            f"'golden hour' sin prohibición cerca: ...{contexto[-90:]!r}")

    block = la.anchors_constraint_block(
        {"lugar": "una plaza", "objetos": [{"objeto": "banco", "linea": ""}],
         "epoca": None})
    assert "NO uses atardecer/golden hour" in block


def test_el_genero_no_puede_elegir_el_lugar():
    """El género entra como paleta, no como vocabulario de escena — que era
    justo el canal del `_GENRE_SCENE_GUIDE` viejo."""
    prompt = _anchored_prompt(genre="rock")
    assert "SOLO para la paleta" in prompt
    assert "No puede elegir el lugar ni los objetos" in prompt


def test_imagen_pide_foto_y_saca_el_movimiento():
    prompt = _anchored_prompt(for_provider="imagen")
    assert '"style":"photo"' in prompt
    assert "IMAGEN FIJA" in prompt


def test_el_compositor_anclado_pide_el_formato_largo_y_los_negativos():
    src = _SRC[_SRC.find("_ANCHORED_SCENE_STRUCTURE = "):
               _SRC.find("def _extract_lyric_anchors")]
    assert "260 a 360 palabras" in src
    assert "negativos" in src
    # Los seis bloques del formato objetivo.
    for bloque in ("LUGAR", "INVENTARIO", "LA AUSENCIA", "LUZ Y ATMÓSFERA",
                   "REFERENCIA", "ENCUADRE"):
        assert bloque in src, bloque


def test_el_anticliche_se_invierte_en_el_camino_anclado():
    """La regla vieja mandaba sustituir por metáfora ante temas sensibles, que
    es justo lo que impedía el resultado que el operador pide."""
    src = _SRC[_SRC.find("_ANCHORED_SCENE_STRUCTURE = "):
               _SRC.find("def _extract_lyric_anchors")]
    assert "ESPECÍFICO ANTES QUE SIMBÓLICO" in src
    assert "NO lo abstraigas" in src


# ── Riel anti-callejón ─────────────────────────────────────────────────────
def test_el_reroll_de_callejon_respeta_una_letra_urbana():
    body = _body("_analyze_lyrics_for_background")
    assert "_anchored_urban" in body
    assert "lyric_anchors.has_urban_anchor(anchors)" in body


def test_el_negativo_de_callejon_no_pelea_con_la_escena_anclada():
    body = _body("_generate_veo_video")
    assert re.search(r"no_alley = \"\" if \(normalized_concept == \"urbano\" or verbatim\s*\n\s*or _anchored_negatives\)",
                     body)


# ── Extractor ──────────────────────────────────────────────────────────────
def test_el_extractor_nunca_tumba_un_render():
    body = _body("_extract_lyric_anchors")
    assert "except Exception" in body
    assert "return None" in body
    # Un job_timeout de RQ sí tiene que propagarse.
    assert "_raise_if_job_timeout(e)" in body


def test_el_extractor_verifica_las_citas_contra_la_letra():
    body = _body("_extract_lyric_anchors")
    assert "lyric_anchors.verify_anchors(parsed, lyrics_text)" in body


def test_el_extractor_usa_temperatura_baja_y_json_estricto():
    body = _body("_extract_lyric_anchors")
    assert "temperature=0.2" in body
    assert 'response_mime_type="application/json"' in body
    assert "thinking_budget=0" in body


def test_el_extractor_deja_rastro_de_provenance():
    body = _body("_extract_lyric_anchors")
    assert 'step="lyric_anchors"' in body


# ── Caché ──────────────────────────────────────────────────────────────────

@contextlib.contextmanager
def _anchors_env(value):
    prev = os.environ.get("BG_LYRIC_ANCHORS")
    os.environ["BG_LYRIC_ANCHORS"] = value
    try:
        yield
    finally:
        if prev is None:
            os.environ.pop("BG_LYRIC_ANCHORS", None)
        else:
            os.environ["BG_LYRIC_ANCHORS"] = prev


def test_off_no_invalida_el_cache_existente():
    """Con el flag apagado las claves quedan byte-idénticas.

    Si el modo entrara siempre al hash, mergear esto tiraría a la basura todo
    el caché de fondos y de escenas en R2 y se re-pagaría Veo sin motivo. Es la
    diferencia entre "output-neutral" y "output-neutral pero carísimo".
    """
    import bg_preview
    import pipeline
    params = {"artist": "Bersuit", "song_title": "Sr Cobranza", "match_lyrics": True}
    with _anchors_env("off"):
        key_off = bg_preview.compute_bg_cache_key(params)
        ns_off = pipeline._scene_cache_ns("A", "S", "coro_1")
    # La forma histórica del namespace de escena, sin sufijo alguno.
    assert ns_off == "A|S|coro_1|auto|background-v5:off:deny"
    # Y el hash coincide con el que produce el canónico sin la clave nueva.
    from background_policy import (cache_policy_fingerprint,
                                   resolve_atmospherics_policy,
                                   resolve_creative_mode)
    canonical = {
        "_cache_version": bg_preview.CACHE_VERSION,
        "_creative_mode": resolve_creative_mode(match_lyrics=True, operator_prompt="",
                                                verbatim=False),
        "_policy_fingerprint": cache_policy_fingerprint(resolve_atmospherics_policy("")),
        "artist": "Bersuit", "song_title": "Sr Cobranza", "style": "",
        "movement_style": "", "effect": "", "custom_colors": "", "genre": "",
        "concept": "", "background_hint": "", "bg_verbatim": False,
        "background_mode": "veo", "animate_image": False, "match_lyrics": True,
    }
    expected = hashlib.sha256(
        json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()[:12]
    assert key_off == expected


def test_on_separa_el_cache_del_motor_viejo():
    import bg_preview
    import pipeline
    params = {"artist": "Bersuit", "song_title": "Sr Cobranza", "match_lyrics": True}
    with _anchors_env("off"):
        key_off = bg_preview.compute_bg_cache_key(params)
        ns_off = pipeline._scene_cache_ns("A", "S", "coro_1")
    with _anchors_env("on"):
        key_on = bg_preview.compute_bg_cache_key(params)
        ns_on = pipeline._scene_cache_ns("A", "S", "coro_1")
    assert key_off != key_on
    assert ns_off != ns_on
    assert ns_on.endswith("|anchors:on")


def test_shadow_tampoco_comparte_cache_con_on():
    """Shadow no cambia la salida, pero `on` sí: no pueden cruzarse."""
    import bg_preview
    params = {"artist": "x", "song_title": "y", "match_lyrics": True}
    with _anchors_env("shadow"):
        shadow = bg_preview.compute_bg_cache_key(params)
    with _anchors_env("on"):
        on = bg_preview.compute_bg_cache_key(params)
    assert shadow != on


# ── Ejemplos de contenido dentro de las cláusulas reusadas ─────────────────
def test_la_clausula_de_camara_pierde_sus_ejemplos_en_el_camino_anclado():
    """`_clause2` del registro estático trae "dust motes", "falling petals",
    "rain on glass". Sangran igual que el bloque de escenas: "dust motes"
    aparece en el 6,7% de los prompts entregados. La REGLA se conserva, los
    ejemplos se van."""
    import pipeline
    original = pipeline._MOVEMENT_STYLE_RULES  # noqa: F841 — sanity de import
    clause = ('framing only — the viewpoint is LOCKED. CRITICAL: motion lives '
              'WITHIN the scene and MUST be RICH — describe AT LEAST 3 distinct '
              'motion sources, e.g.: moving reflections + flickering candle + '
              'dust motes; or rolling waves + shifting clouds + falling petals. '
              'A scene with zero movement is INVALID')
    out = pipeline._strip_content_examples(clause)
    for ejemplo in ("dust motes", "falling petals", "rolling waves",
                    "flickering candle"):
        assert ejemplo not in out, ejemplo
    # La norma sobrevive.
    assert "LOCKED" in out
    assert "AT LEAST 3 distinct motion sources" in out
    assert "INVALID" in out


def test_strip_no_rompe_una_clausula_sin_ejemplos():
    import pipeline
    clause = "framing and composition, treated as a STILL PHOTOGRAPH; never zoom"
    assert pipeline._strip_content_examples(clause) == clause


def test_la_regla_de_personas_anclada_no_trae_ejemplos_de_objetos():
    """La versión vieja sugiere "the empty chair, the worn stage, the two coffee
    cups, the rain they walked through" — cuatro escenas listas para copiar.
    Acá el entorno ya lo dan las anclas."""
    import pipeline
    rule = pipeline._ANCHORED_PEOPLE_RULE
    for ejemplo in ("empty chair", "worn stage", "coffee cups", "worn"):
        assert ejemplo not in rule, ejemplo
    assert "Sin personas" in rule


def test_la_regla_de_personas_anclada_permite_carteles_en_blanco():
    """Tiene que coincidir con los negativos aflojados del provider boundary: si
    el system prompt dijera "sin carteles" y el riel dijera "carteles en blanco
    sí", el modelo recibe instrucciones contradictorias."""
    import pipeline
    rule = pipeline._ANCHORED_PEOPLE_RULE
    assert "en blanco" in rule
    assert "texto legible" in rule


def test_con_allow_people_no_se_inyecta_regla_de_personas():
    body = _body("_analyze_lyrics_for_background")
    assert 'people_rule=("" if allow_people else _ANCHORED_PEOPLE_RULE)' in body
