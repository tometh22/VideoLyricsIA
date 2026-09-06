"""Casos de compliance del fondo, tal como los definió el operador.

Decisión de producto (2026-09-03): lugares reales nombrados SÍ cuando la letra
los nombra o los implica fuerte; pero sin texto legible, sin logos ni marcas,
sin símbolos políticos como protagonistas y sin rostros reconocibles. Las
pancartas EN BLANCO pasan; cualquier cosa legible falla.

El validador Vision es la red. Estos tests fijan su contrato sobre la salida —
no sobre lo que el prompt dice pedir, que es donde estaba el agujero: el riel
del prompt decía "no text, no words, no letters" desde siempre y el gate no lo
hacía cumplir.
"""

import content_validator as cv


def _evaluate(detections, issues=(), **kw):
    return cv._evaluate_frame_result(
        {"detections": detections, "issues": list(issues)},
        allow_people=kw.get("allow_people", False),
        allow_atmospherics=kw.get("allow_atmospherics", True),
        enforce_atmospherics=kw.get("enforce_atmospherics", False),
        enforce_text=kw.get("enforce_text", False),
    )


def _clean(**over):
    d = {"people": False, "atmospherics": False, "brand": False, "text": False}
    d.update(over)
    return d


# ── Los dos casos que definió el operador ──────────────────────────────────
def test_pancarta_en_blanco_pasa():
    """El caso del ejemplo objetivo: una plaza después de una marcha necesita
    pancartas de cartón EN BLANCO. El objeto es legítimo."""
    v = _evaluate(_clean())
    assert v["passed"] is True
    assert v["issues"] == []
    assert v["observations"] == []


def test_texto_legible_se_detecta():
    """Cualquier cosa legible incumple. Hoy se REPORTA; el bloqueo se prende
    aparte (ver test_el_texto_arranca_en_observacion)."""
    v = _evaluate(_clean(text=True),
                  [{"category": "text", "reason": "cartel con la palabra HOTEL"}])
    assert v["detections"]["text"] is True
    assert any("HOTEL" in o for o in v["observations"])


def test_texto_legible_bloquea_cuando_se_lo_pide():
    v = _evaluate(_clean(text=True),
                  [{"category": "text", "reason": "pancarta con consigna legible"}],
                  enforce_text=True)
    assert v["passed"] is False
    assert any(i.startswith("text:") for i in v["issues"])


def test_el_texto_arranca_en_observacion_y_no_cambia_el_veredicto():
    """Decisión deliberada: medir antes de bloquear. Este repo ya tuvo un
    incidente de sobre-bloqueo del validador (jul-2026) y una escena urbana
    legítima puede traer cartelería de fondo. Sin `enforce_text`, el veredicto
    es idéntico al de antes de existir esta categoría."""
    v = _evaluate(_clean(text=True),
                  [{"category": "text", "reason": "letras en una vidriera"}])
    assert v["passed"] is True
    assert v["observations"], "el texto tiene que quedar registrado igual"


# ── Caras: la red que ya existía y tiene que seguir ────────────────────────
def test_rostro_reconocible_bloquea():
    v = _evaluate(_clean(people=True),
                  [{"category": "people", "reason": "rostro reconocible en primer plano"}])
    assert v["passed"] is False
    assert any(i.startswith("people:") for i in v["issues"])


def test_figura_lejana_e_incidental_no_bloquea():
    """"Sin rostros reconocibles" no es "sin nadie": una cenital de una avenida
    con gente diminuta es aceptable (criterio 2026-07-24)."""
    v = _evaluate(_clean())
    assert v["passed"] is True


# ── Marcas: sin cambios ────────────────────────────────────────────────────
def test_marca_comercial_bloquea_siempre():
    v = _evaluate(_clean(brand=True),
                  [{"category": "brand", "reason": "logo de Coca-Cola"}])
    assert v["passed"] is False


def test_una_categoria_no_arrastra_a_otra():
    """Texto en observación no puede volver a bloquear una marca, ni al revés."""
    v = _evaluate(_clean(text=True, brand=True),
                  [{"category": "text", "reason": "letras"},
                   {"category": "brand", "reason": "logo Nike"}])
    assert v["passed"] is False
    assert any(i.startswith("brand:") for i in v["issues"])
    assert not any(i.startswith("text:") for i in v["issues"])


# ── Contrato del clasificador ──────────────────────────────────────────────
def test_el_clasificador_pide_la_categoria_text():
    import inspect
    src = inspect.getsource(cv._check_frame_with_gemini)
    assert "detections.text=true" in src
    assert '"text":true/false' in src
    assert "people|atmospherics|brand|text" in src


def test_el_clasificador_ya_no_acepta_garabatos_de_ia():
    """"Invented / gibberish / stylized text strings" seguía listado como
    aceptable — y es el artefacto típico de un generador de video."""
    import inspect
    src = inspect.getsource(cv._check_frame_with_gemini)
    assert "AI-hallucinated glyphs that still read as writing" in src


def test_el_clasificador_deja_pasar_la_superficie_en_blanco():
    import inspect
    src = inspect.getsource(cv._check_frame_with_gemini)
    assert "blank cardboard sign" in src
    assert "must not be reported" in src


def test_una_respuesta_legacy_sigue_fallando_cerrado():
    """El shape viejo {"safe","issues"} no puede usarse para saltear el gate."""
    v = cv._evaluate_frame_result(
        {"safe": True, "issues": ["algo"]},
        allow_people=False, allow_atmospherics=True, enforce_atmospherics=False)
    assert v["passed"] is False
    assert v["detections"]["text"] is False
