"""Ventana de la letra oficial que corresponde a un fragmento de audio.

POR QUÉ EXISTE
--------------
La letra de catálogo (lrclib, la planilla, la que pega el operador) describe la
canción ENTERA. Cuando el audio es un fragmento (demos de 30 s, Shorts, Canvas)
las dos comparaciones que deciden si esa letra sirve la medían contra la
canción entera y la descartaban siempre:

- la Revisión rápida (``lyric_review``) exige similitud global >= 0,45 y un
  fragmento de 30 s da 0,05–0,34;
- la atestación del pipeline (``reference_attestation``) mide cobertura sobre
  la canción entera: 8–24 %.

Evaluación offline sobre 15 demos de producción (16-nov-2026,
``20_EVAL_DETECTOR_15_DEMOS.md``): lrclib estaba en 12 de 15 y contenía 13 de
las ~22 correcciones de texto reales. Recortando la letra a la ventana que
mejor coincide con el texto de máquina, el recall de texto de la Revisión
rápida sube de 58 % a 71 %, y los puntos de letra oficial aciertan el 84 %. La
letra equivocada (otra canción) da una coincidencia de ventana de 0,39 y se
rechaza; las correctas dan 0,87–0,99.

QUÉ HACE
--------
Busca el tramo de líneas CONSECUTIVAS de la letra oficial cuyo texto más se
parece al de la máquina (similitud de caracteres sobre tokens normalizados,
que tolera palabras mal oídas: "despierto"/"despecho"). Los tramos candidatos
miden entre 0,6x y 1,6x las palabras de la máquina. Si el estribillo se repite,
cualquiera de sus repeticiones sirve (gana la primera). Si la mejor ventana no
llega a ``WINDOW_MIN_SCORE`` se devuelve ``None``: la letra es de otra canción
o de otra versión y no se usa.

Puro: sin I/O, sin red, sin modelos.
"""
from __future__ import annotations

import difflib
import re
import unicodedata
from collections import Counter
from typing import Any, Sequence

# Coincidencia mínima de la ventana. Medido: letra correcta 0,87–0,99 en 12
# demos; otra canción (La Loca) 0,39. El texto de dos canciones distintas del
# mismo idioma da ~0,3–0,45 a nivel de caracteres.
WINDOW_MIN_SCORE = 0.6
# Menos palabras de máquina que esto no alcanzan para ubicar un tramo.
MIN_MACHINE_TOKENS = 8
# Topes de costo: un fragmento tiene decenas de palabras, no cientos. Más que
# esto no es un fragmento y la ventana no se busca.
MAX_MACHINE_TOKENS = 400
MAX_REFERENCE_LINES = 300
_MIN_SPAN = 0.6
_MAX_SPAN = 1.6
# Ventanas (las de más palabras en común) que pasan a la comparación exacta.
_EXACT_CANDIDATES = 24


def window_tokens(text: Any) -> list[str]:
    """Tokens en minúscula, sin tildes ni puntuación."""
    folded = unicodedata.normalize("NFKD", str(text or "").casefold())
    folded = "".join(ch for ch in folded if unicodedata.category(ch) != "Mn")
    return re.findall(r"[^\W_]+", folded, re.UNICODE)


def find_reference_window(
    reference_lines: Sequence[str],
    machine_text: str,
    *,
    min_score: float = WINDOW_MIN_SCORE,
) -> dict | None:
    """Mejor tramo de líneas consecutivas de ``reference_lines`` para el texto
    de máquina.

    Devuelve ``{"start_line", "end_line" (exclusivo), "score",
    "window_tokens", "machine_tokens", "reference_lines"}`` con índices sobre
    ``reference_lines`` tal cual vienen (las líneas vacías cuentan para el
    índice pero no aportan palabras), o ``None`` si no hay un tramo que
    coincida con al menos ``min_score``.
    """
    lines = [window_tokens(line) for line in reference_lines or []]
    machine = window_tokens(machine_text)
    n = len(machine)
    if (
        n < MIN_MACHINE_TOKENS
        or n > MAX_MACHINE_TOKENS
        or not lines
        or len(lines) > MAX_REFERENCE_LINES
    ):
        return None
    lo, hi = _MIN_SPAN * n, _MAX_SPAN * n
    machine_bag = Counter(machine)
    # 1) Todas las ventanas candidatas, ordenadas por palabras en común (barato).
    candidates: list[tuple[float, int, int, int, str]] = []
    for a in range(len(lines)):
        if not lines[a]:
            continue  # la ventana empieza en una línea con palabras
        acc: list[str] = []
        bag: Counter = Counter()
        for b in range(a, len(lines)):
            if not lines[b]:
                continue
            acc.extend(lines[b])
            bag.update(lines[b])
            if len(acc) > hi and b > a:
                break
            if len(acc) < lo:
                continue
            common = sum((bag & machine_bag).values())
            candidates.append((2.0 * common / (n + len(acc)), a, b + 1, len(acc), " ".join(acc)))
    if not candidates:
        return None
    candidates.sort(key=lambda c: (-c[0], c[1], c[2]))
    # 2) Similitud de caracteres (la medida calibrada) sólo sobre las mejores.
    # Mismo orden que la evaluación offline (máquina, ventana): ``ratio`` no
    # es exactamente simétrico y los umbrales se midieron así.
    matcher = difflib.SequenceMatcher(autojunk=False)
    matcher.set_seq1(" ".join(machine))
    best_score, best = 0.0, None
    for _bag_score, a, end, count, text in sorted(
            candidates[:_EXACT_CANDIDATES], key=lambda c: (c[1], c[2])):
        matcher.set_seq2(text)
        if matcher.real_quick_ratio() <= best_score or matcher.quick_ratio() <= best_score:
            continue
        score = matcher.ratio()
        if score > best_score:
            best_score, best = score, (a, end, count)
    if best is None or best_score < min_score:
        return None
    start, end, count = best
    return {
        "start_line": start,
        "end_line": end,
        "score": round(best_score, 4),
        "window_tokens": count,
        "machine_tokens": n,
        "reference_lines": len(lines),
    }


def crop_reference(
    reference_text: str,
    machine_text: str,
    *,
    pad_lines: int = 0,
    min_score: float = WINDOW_MIN_SCORE,
) -> tuple[str, dict] | None:
    """Letra oficial recortada a la ventana del fragmento (más ``pad_lines``
    líneas de cada lado, para no perder la línea que el audio corta a la
    mitad). ``None`` si no hay una ventana confiable."""
    lines = str(reference_text or "").splitlines()
    window = find_reference_window(lines, machine_text, min_score=min_score)
    if window is None:
        return None
    start = max(0, window["start_line"] - max(0, pad_lines))
    end = min(len(lines), window["end_line"] + max(0, pad_lines))
    cropped = "\n".join(line for line in lines[start:end] if line.strip())
    return cropped, {**window, "crop_start_line": start, "crop_end_line": end}
