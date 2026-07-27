"""`render_params.movement_style` se persiste SIEMPRE canónico.

El campo entra como texto libre (`Form("", max_length=64)`, y el body de /edit y
/variant lo declaran `str`), y hasta ahora se guardaba tal cual. El pipeline lo
normaliza al RENDERIZAR, así que el video siempre salía bien — pero la fila
quedaba con un valor que ninguna otra capa entiende:

- el wizard no puede resaltar la opción (ningún código del catálogo matchea), así
  que el operador ve la galería sin nada seleccionado;
- `admin_insights` agrega el valor crudo (`_DISTRIBUTION_KEYS`), así que
  "dinamico" y "estandar" cuentan como buckets distintos aunque el render los
  trate igual;
- y cada lector nuevo tiene que acordarse de normalizar por su cuenta.

Normalizar al ESCRIBIR mata la clase entera: es la misma función que el pipeline
ya corre, así que no cambia ningún render, y deja una invariante simple —
`render_params.movement_style` es un código de `_MOVEMENT_STYLE_RULES` o "".

Nota de alcance: `scene_plan.scenes[].movement_style` es OTRO campo, con su
propio vocabulario (incluye "dinamico"), consumido por la regeneración por
escena. Este test no lo toca.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from pipeline import _MOVEMENT_STYLE_RULES, _normalize_movement_style  # noqa: E402

CANONICAL = set(_MOVEMENT_STYLE_RULES.keys()) | {""}

# Todo lo que puede llegar al campo: los códigos, los alias que el propio
# normalizador acepta, lo que emiten el editor de escena y el derivado por
# energía, variantes de capitalización/acento, y basura.
INPUTS = [
    "", "  ", "estatico", "sutil", "estandar", "foto-parallax", "animado",
    "dinamico", "dinámico", "dynamic", "static", "estatica", "estática",
    "fija", "fixed", "tripod", "locked", "still", "camara-fija",
    "subtle", "minimal", "minimo", "standard", "default",
    "photo", "parallax", "foto+parallax", "foto_parallax",
    "animated", "illustration", "cartoon",
    "ESTATICO", "  Sutil  ", "estático", "zoom-in", "basura", "'; drop table",
    "constructor", "__proto__",
]


@pytest.mark.parametrize("raw", INPUTS)
def test_normalizacion_siempre_devuelve_un_codigo_canonico(raw):
    assert _normalize_movement_style(raw) in CANONICAL


def test_los_codigos_canonicos_son_punto_fijo():
    """Normalizar dos veces da lo mismo — condición para poder normalizar al
    escribir sin degradar el valor en cada edición sucesiva."""
    for raw in INPUTS:
        once = _normalize_movement_style(raw)
        assert _normalize_movement_style(once) == once


def test_los_tres_puntos_de_escritura_normalizan():
    """Guard de código: los 3 lugares que escriben render_params.movement_style
    tienen que pasar por el normalizador.

    Es una aserción sobre el SOURCE y normalmente eso es un antipatrón — pero
    acá el valor de la invariante está en que se sostenga en TODOS los caminos
    de escritura, y no hay forma de ejercitar los tres sin levantar el worker,
    la API y la cola. Si aparece un cuarto punto de escritura, este test no lo
    va a ver: la defensa real es que el frontend también normaliza al leer.
    """
    import inspect

    import main
    import pipeline

    # create: run_pipeline arma render_params
    src_pipeline = inspect.getsource(pipeline)
    assert '"movement_style": _normalize_movement_style(movement_style)' in src_pipeline

    # edit + variant
    src_main = inspect.getsource(main)
    assert "_mv = _normalize_movement_style(" in src_main
    assert '_value = _normalize_movement_style(_value or "")' in src_main
