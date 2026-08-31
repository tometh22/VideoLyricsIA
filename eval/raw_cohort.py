"""Cohorte con crudo confiable — ÚNICA fuente de verdad para métricas.

POR QUÉ EXISTE (hallazgo 2026-08-30)
------------------------------------
El corpus clasifica el baseline pre-humano en `exact` / `estimated` / `none`.
Hasta hoy había una cuarta categoría, `reconstructed`, que se consideraba tan
buena como `exact` — y seis módulos repetían el literal
`{"exact", "reconstructed"}` por su cuenta.

Esa equivalencia se refutó midiendo el rewind contra los casos que SÍ tienen
checkpoint exacto:

  * WER del rewind vs el original: mediana **7,5%**, máximo 28,9%
  * 4 de 5 casos difieren en TEXTO (no sólo timing)
  * 3 de 5 cambian la SEGMENTACIÓN
  * delta de timing de hasta **29,3 segundos**
  * caso testigo `c54adb6de148`: declara cero limitaciones
    (`out_of_range=0, reorders=0, truncated=0`) y aun así mueve 17 líneas y
    cambia la segmentación → la pérdida es sistemática y el mecanismo NO la
    registra.

Con una mediana de error propio de 7,5%, el instrumento es del mismo orden que
la señal que se quiere medir (baseline histórico 8,3%). Por eso `reconstructed`
se degradó a uso diagnóstico —sin destruir su etiqueta de procedencia— y
**sólo `exact` sirve para métricas**.

CONTRATO
--------
- `RAW_TRUSTED` es la única definición de "crudo confiable". No la repitas.
- Toda métrica que dependa del baseline pre-humano usa `filter_raw_trusted()`
  o `require_raw_trusted()`. Hay un test que falla si alguien vuelve a escribir
  el literal por su cuenta (`tests/test_raw_cohort_enforcement.py`).
- El crudo de las `estimated` SIGUE en disco y se puede leer para diagnóstico
  cualitativo — vía `iter_diagnostic_only()`, que deja explícito que ese
  material no puede alimentar un número.
"""
from __future__ import annotations

# Única cohorte cuyo baseline pre-humano es un checkpoint inmutable real
# (`editor_documents.original_segments`), no una reconstrucción.
RAW_TRUSTED = frozenset({"exact"})

# Excluidas de métricas el 2026-08-31. Conservan su etiqueta de procedencia y
# su crudo en disco para diagnóstico, pero NO pueden alimentar ningún gate.
RAW_DIAGNOSTIC_ONLY = frozenset({"reconstructed", "estimated"})

RAW_ABSENT = frozenset({"none"})


class RawCohortViolation(RuntimeError):
    """Se intentó calcular una métrica sobre crudo no confiable."""


def _quality(case) -> str:
    if isinstance(case, dict):
        return str(case.get("raw_quality") or "")
    return str(getattr(case, "raw_quality", "") or "")


def is_raw_trusted(case) -> bool:
    """True sólo si el baseline pre-humano de este caso es un checkpoint real."""
    return _quality(case) in RAW_TRUSTED


def filter_raw_trusted(cases):
    """Los casos aptos para métricas. Usar SIEMPRE esto en vez de un literal."""
    return [c for c in cases if is_raw_trusted(c)]


def require_raw_trusted(cases, *, metric: str):
    """Como `filter_raw_trusted`, pero explota si la entrada trae casos no
    confiables. Para métricas donde recortar en silencio sería peor que fallar:
    un promedio calculado sobre menos casos de los que el autor cree es un
    número plausible y equivocado."""
    malos = [c for c in cases if not is_raw_trusted(c)]
    if malos:
        ids = ", ".join(sorted(str(_id(c)) for c in malos)[:5])
        raise RawCohortViolation(
            f"la métrica '{metric}' recibió {len(malos)} caso(s) sin crudo "
            f"confiable (ej.: {ids}). Sólo raw_quality=='exact' sirve para "
            f"métricas — ver eval/raw_cohort.py."
        )
    return list(cases)


def iter_diagnostic_only(cases):
    """Casos cuyo crudo existe pero NO es confiable (reconstruido por rewind).

    Sirve para inspección cualitativa. Si lo que estás por hacer produce un
    número que alguien va a citar, no es el iterador que buscás.
    """
    return [c for c in cases if _quality(c) in RAW_DIAGNOSTIC_ONLY]


def _id(case):
    if isinstance(case, dict):
        return case.get("song_id") or case.get("job_id") or "?"
    return getattr(case, "song_id", None) or getattr(case, "job_id", "?")


def cohort_summary(cases) -> dict:
    """Conteos por cohorte, para encabezar cualquier reporte."""
    trusted = filter_raw_trusted(cases)
    diag = iter_diagnostic_only(cases)
    return {
        "total": len(cases),
        "raw_trusted": len(trusted),
        "diagnostic_only": len(diag),
        "no_raw": len([c for c in cases if _quality(c) in RAW_ABSENT]),
        "metrics_cohort": "raw_quality == 'exact'",
    }
