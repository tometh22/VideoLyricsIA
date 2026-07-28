"""Cobertura de la letra medida contra el AUDIO.

POR QUÉ EXISTE
--------------
Toda métrica de calidad que había en el backend se mide contra la letra de
REFERENCIA (`coverage = len(out)/len(lines)` en whisperx_reconcile, y las
variantes en main.py). Esa métrica tiene un defecto estructural: si la
referencia viene recortada, el denominador se achica y la cobertura SUBE.
El fallo se autocertifica como éxito.

Medido en producción (28-07-2026): un job reportaba 15/15 = 100 % de
cobertura mientras sólo el 39 % de lo cantado tenía una línea encima. En un
segundo tema el gap fue 100 % reportado vs 62 % real. Y sobre 68 canciones
con la duración real del audio, el 22 % termina la letra a más de 30 s del
final y el 13 % a más de 45 s — de las 10 peores colas, 7 tenían canto real.

Esta métrica es la inversa: qué fracción de las palabras que el ASR oyó
queda debajo de alguna línea emitida. Sólo puede BAJAR si se pierde canto,
así que no se puede autocertificar.

DISEÑO
------
- Se mide contra las PALABRAS del ASR, no contra el tiempo: donde no hubo
  palabras (pasajes instrumentales, fade-outs) no cuenta como pérdida. Eso
  evita el falso positivo de "outro instrumental largo".
- Una palabra cuenta como cubierta si su punto medio cae dentro de algún
  segmento; el punto medio tolera el clamp monótono de los bordes.
- Puro, sin I/O, sin dependencias. Sirve para observabilidad y para gatear.
"""
from __future__ import annotations


def _f(v, default: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _intervals(segments) -> list[tuple[float, float]]:
    out = []
    for s in segments or []:
        if isinstance(s, dict):
            out.append((_f(s.get("start")), _f(s.get("end"))))
    return out


def _mid(w) -> float:
    a, b = _f(w.get("start")), _f(w.get("end"))
    return (a + b) / 2 if b > a else a


def audio_coverage(segments, words, *, pad: float = 0.05) -> float:
    """Fracción [0..1] de las palabras del ASR que alguna línea reclama.

    Devuelve 1.0 cuando no hay palabras contra las cuales medir (no se puede
    afirmar pérdida) y 0.0 cuando hay palabras pero ninguna línea."""
    ws = [w for w in (words or []) if isinstance(w, dict)]
    if not ws:
        return 1.0
    iv = _intervals(segments)
    if not iv:
        return 0.0
    n = 0
    for w in ws:
        m = _mid(w)
        if any(a - pad <= m <= b + pad for a, b in iv):
            n += 1
    return n / len(ws)


def uncovered_spans(segments, words, *, min_gap_s: float = 3.0,
                    min_words: int = 3, split_s: float = 2.5,
                    pad: float = 0.05) -> list[tuple[float, float, int]]:
    """Regiones con canto que ninguna línea reclama, como
    `(inicio, fin, n_palabras)`. Sirve para el log y para alertar: dice
    DÓNDE se perdió letra, no sólo cuánto."""
    ws = [w for w in (words or []) if isinstance(w, dict)]
    if not ws:
        return []
    iv = _intervals(segments)
    huerfanas: list[list[dict]] = []
    actual: list[dict] = []
    for w in ws:
        m = _mid(w)
        if any(a - pad <= m <= b + pad for a, b in iv):
            if actual:
                huerfanas.append(actual)
                actual = []
            continue
        if actual and (_f(w.get("start")) - _f(actual[-1].get("end"))) > split_s:
            huerfanas.append(actual)
            actual = []
        actual.append(w)
    if actual:
        huerfanas.append(actual)

    out = []
    for run in huerfanas:
        ini, fin = _f(run[0].get("start")), _f(run[-1].get("end"))
        if len(run) >= min_words and (fin - ini) >= min_gap_s:
            out.append((round(ini, 2), round(fin, 2), len(run)))
    return out


def summarize(segments, words) -> dict:
    """Resumen listo para loguear/persistir."""
    spans = uncovered_spans(segments, words)
    duraciones = [b - a for a, b, _ in spans]
    return {
        "audio_coverage": round(audio_coverage(segments, words), 4),
        "uncovered_spans": len(spans),
        "uncovered_seconds": round(sum(duraciones), 2),
        "worst_span_s": round(max(duraciones), 2) if duraciones else 0.0,
    }
