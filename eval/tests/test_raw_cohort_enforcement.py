"""La exclusión de crudo no confiable la impone el CÓDIGO, no la convención.

Hallazgo 2026-08-30: el rewind de diffs no reproduce el checkpoint exacto
(mediana 7,5% de WER de error propio, hasta 29,3 s de deriva de timing, y el
caso testigo `c54adb6de148` declara cero limitaciones mientras cambia 17 líneas
y la segmentación). Por eso `reconstructed` se degradó a `estimated` y sólo
`raw_quality == 'exact'` sirve para métricas.

Antes de esto, seis módulos repetían `{"exact", "reconstructed"}` cada uno por
su cuenta. Con la definición dispersa, degradar una cohorte obliga a acordarse
de seis lugares — y olvidarse de uno no rompe nada visible: simplemente produce
un número contaminado que parece correcto. Estos tests hacen que ese olvido
falle en CI.
"""
from __future__ import annotations

import pathlib
import re

import pytest

EVAL_DIR = pathlib.Path(__file__).resolve().parents[1]

# El literal viejo, en cualquier orden y con comillas simples o dobles.
LITERAL_VIEJO = re.compile(
    r"""\{\s*["'](exact|reconstructed)["']\s*,\s*["'](exact|reconstructed)["']\s*\}"""
)

# `raw_cohort.py` es el único autorizado a nombrar las categorías; los tests
# también, porque justamente verifican la regla.
EXENTOS = {"raw_cohort.py", "build_from_snapshot.py"}


def _fuentes():
    for p in sorted(EVAL_DIR.rglob("*.py")):
        if "__pycache__" in p.parts or p.name.startswith("test_"):
            continue
        if p.name in EXENTOS:
            continue
        yield p


def test_ningun_modulo_reescribe_la_cohorte_a_mano():
    culpables = []
    for p in _fuentes():
        texto = p.read_text(encoding="utf-8", errors="ignore")
        if LITERAL_VIEJO.search(texto):
            culpables.append(p.relative_to(EVAL_DIR).as_posix())
    assert not culpables, (
        "Estos módulos definen la cohorte por su cuenta con "
        '{"exact", "reconstructed"}:\n  ' + "\n  ".join(culpables) +
        "\n\nEsa combinación quedó refutada el 2026-08-30: 'reconstructed' se "
        "degradó a 'estimated'. Usá eval.raw_cohort.filter_raw_trusted() o "
        "require_raw_trusted() en vez de un literal."
    )


def test_reconstructed_no_es_cohorte_de_metricas():
    from eval.raw_cohort import RAW_DIAGNOSTIC_ONLY, RAW_TRUSTED

    assert RAW_TRUSTED == {"exact"}
    assert "reconstructed" in RAW_DIAGNOSTIC_ONLY
    assert "estimated" in RAW_DIAGNOSTIC_ONLY


def test_require_raw_trusted_falla_fuerte_en_vez_de_recortar():
    """Recortar en silencio produce un promedio sobre menos casos de los que el
    autor cree: un número plausible y equivocado. Preferimos que explote."""
    from eval.raw_cohort import RawCohortViolation, require_raw_trusted

    casos = [{"song_id": "a", "raw_quality": "exact"},
             {"song_id": "b", "raw_quality": "estimated"}]
    with pytest.raises(RawCohortViolation) as exc:
        require_raw_trusted(casos, metric="wer_limpio")
    assert "wer_limpio" in str(exc.value)
    assert "b" in str(exc.value)


def test_el_manifest_real_quedo_degradado():
    """El corpus en disco debe reflejar 23 / 42 / 8, no la clasificación vieja."""
    import json

    manifest = EVAL_DIR / "golden" / "manifest.json"
    if not manifest.exists():
        pytest.skip("corpus no extraído en este worktree")
    counts = json.loads(manifest.read_text())["raw_quality_counts"]
    assert counts.get("exact") == 23
    # 18 reconstructed + 16 estimated = 34. (No 42: eso sumaria 73 de 65.)
    assert counts.get("estimated") == 34, (
        "las 18 'reconstructed' deben estar degradadas dentro de 'estimated', "
        "sumando 34 con las 16 que ya lo eran")
    assert sum(counts.values()) == 65
    assert counts.get("none") == 8
    assert not counts.get("reconstructed"), (
        "'reconstructed' ya no es una categoría válida del corpus")
