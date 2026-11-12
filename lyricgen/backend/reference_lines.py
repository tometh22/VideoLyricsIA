"""Contrato de líneas de la letra oficial pegada por el operador.

INCIDENTE (prod, 11-nov-2026): en el wizard se eligió "Tengo la letra
oficial" y se pegaron 8 líneas (fragmento de 30 s, portugués, con una
interjección corta tipo "Uh oh uh oh" y una línea de estribillo repetida).
El editor recibió líneas FUNDIDAS y las 2 últimas directamente no estaban.
El workaround fue un PATCH /editor/{job} con los segmentos correctos.

Dos causas, independientes entre sí:

1. Los alineadores pierden la cola. El forced-align hosted corta la
   referencia cuando "el audio se terminó" (guard de pile-up de
   ``forced_align.wordstamps_to_segments``); Whisper-DP empuja las líneas
   de cola más allá del fin del audio (``end <= start`` tras el clamp) y
   CTC deja las líneas no ancladas de la cola en ``(dur, dur)``. Para una
   letra oficial pegada eso no es "texto que el audio no canta": es un
   fragmento que termina pegado al final del archivo. ``complete_alignment``
   repone esas líneas DENTRO del audio, interpoladas y marcadas para
   revisión, en vez de borrarlas.

2. Los post-pases de calidad (ad-libs, reconciliación de repeticiones,
   rescate de huecos, voto de palabras, snap de coros, segmentador, formato,
   filtro de créditos) fueron escritos para ASR o letras de catálogo dudosas:
   pueden fundir, partir, reescribir o descartar filas. Sobre una letra
   oficial ninguna de esas decisiones es legítima — el operador eligió esa
   opción justamente "cuando necesitás que el texto coincida exactamente".
   ``enforce_contract`` corre en el último borde (``_maybe_timing_consistency``)
   y restituye exactamente una fila por línea pegada, en orden, conservando
   el timing refinado de las filas que sobrevivieron intactas.

Puro: sin I/O, sin red. Nunca levanta.
"""
from __future__ import annotations

import logging
import math
import re
import unicodedata

logger = logging.getLogger("genly.reference_lines")

CONTRACT_KEY = "_reference_line_contract"
REVIEW_REASON = "reference_line_unaligned"
# Duración mínima de una línea repuesta, y la cadencia supuesta (s/línea)
# cuando no hay vecino del otro lado (misma que usa Whisper-DP en la cola).
MIN_LINE_S = 0.5
FLOOR_LINE_S = 0.2
CADENCE_S = 3.0
_GAP_S = 0.05


def split_reference_lines(text: str) -> list[str]:
    """Una línea por renglón no vacío, sin espacios de borde.

    Pegar desde una web suele dejar espacios al final de cada renglón; sin
    el strip, la comparación exacta de ``_safe_alignment`` rechazaba a todos
    los alineadores de respaldo."""
    return [line.strip() for line in str(text or "").splitlines() if line.strip()]


def line_key(text: str) -> str:
    """Clave de comparación: minúsculas, sin puntuación ni espacios extra.

    Conserva los acentos (en portugués "e"/"é" son palabras distintas)."""
    folded = unicodedata.normalize("NFC", str(text or "")).casefold()
    return " ".join(re.findall(r"[^\W_]+", folded, re.UNICODE))


def _num(value):
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def _valid_timing(segment, audio_duration):
    start, end = _num(segment.get("start")), _num(segment.get("end"))
    if start is None or end is None or start < 0:
        return False
    if audio_duration and start >= audio_duration:
        return False
    if audio_duration and end > audio_duration:
        end = audio_duration
    return end > start


def _clip_words(segment: dict, *, lo: float | None = None,
                hi: float | None = None) -> None:
    """Keep ``segment["words"]`` inside ``[lo, hi]`` (in place).

    Words wholly outside the bound are dropped, words straddling it are
    clipped. With no word left the key is removed, so the renderer and the
    timing pass fall back to the line window."""
    words = segment.get("words")
    if not isinstance(words, list) or not words:
        return
    kept = []
    for word in words:
        if not isinstance(word, dict):
            continue
        start, end = _num(word.get("start")), _num(word.get("end"))
        if start is None or end is None:
            kept.append(word)
            continue
        if hi is not None and start >= hi:
            continue
        if lo is not None and end <= lo:
            continue
        if (hi is not None and end > hi) or (lo is not None and start < lo):
            word = dict(word)
            if hi is not None and end > hi:
                word["end"] = round(hi, 3)
            if lo is not None and start < lo:
                word["start"] = round(lo, 3)
        kept.append(word)
    if kept != words:
        if kept:
            segment["words"] = kept
        else:
            segment.pop("words", None)


def _map_rows(lines: list[str], rows: list, window: int = 8):
    """Asigna cada fila del alineador a su línea pegada, en orden.

    Los alineadores emiten las líneas en el orden de la referencia (o
    descartan algunas); un emparejamiento codicioso hacia adelante con
    ventana corta resuelve las repeticiones del estribillo sin cruzarlas."""
    keys = [line_key(line) for line in lines]
    mapped: list = [None] * len(lines)
    i = 0
    for row in rows:
        if not isinstance(row, dict):
            continue
        key = line_key(row.get("text") or "")
        for j in range(i, min(len(lines), i + window)):
            if keys[j] == key:
                mapped[j] = row
                i = j + 1
                break
    return mapped


def complete_alignment(lines: list[str], aligned, *,
                       audio_duration: float | None = None):
    """Devuelve ``(segmentos, stats)`` con exactamente una fila por línea.

    Las filas que el alineador devolvió con timing válido se conservan tal
    cual (sólo se recorta un ``end`` que pasa el fin del audio). Las que
    faltan, o que quedaron sin ventana (``end <= start``, o arrancando
    después del fin del audio), se reponen interpoladas entre sus vecinos
    ancladas, dentro del audio, con ``interpolated=True`` y
    ``review_reasons=[reference_line_unaligned]`` — el presupuesto de
    interpoladas de ``_safe_alignment`` sigue siendo el que decide si el
    resultado entero es aceptable.

    Si ``aligned`` no es una lista, o no hay lugar físico para las líneas
    faltantes, devuelve ``aligned`` intacto (el caller declina como antes).
    Si no hacía falta reponer nada, devuelve el MISMO objeto.
    """
    stats = {"missing": 0, "invalid_timing": 0, "interpolated": 0}
    if not isinstance(aligned, list) or not lines:
        return aligned, stats
    try:
        dur = _num(audio_duration)
        dur = dur if dur and dur > 0 else None
        mapped = _map_rows(lines, aligned)
        rows: list = []
        changed = len(aligned) != len(lines)
        for line, row in zip(lines, mapped):
            if row is None:
                stats["missing"] += 1
                rows.append(None)
                continue
            if not _valid_timing(row, dur):
                stats["invalid_timing"] += 1
                rows.append(None)
                changed = True
                continue
            seg = dict(row)
            if str(seg.get("text") or "").strip() != line:
                seg["text"] = line
                changed = True
            if dur and float(seg["end"]) > dur:
                seg["end"] = round(dur, 3)
                changed = True
            rows.append(seg)
        if not changed:
            return aligned, stats

        n = len(rows)
        idx = 0
        while idx < n:
            if rows[idx] is not None:
                idx += 1
                continue
            first = idx
            while idx < n and rows[idx] is None:
                idx += 1
            last = idx - 1
            k = last - first + 1
            prev = rows[first - 1] if first > 0 else None
            nxt = rows[last + 1] if last + 1 < n else None
            if nxt is not None:
                hi = float(nxt["start"])
            else:
                lo_guess = float(prev["end"]) if prev is not None else 0.0
                hi = lo_guess + k * CADENCE_S
                if dur:
                    hi = min(dur, hi)
                    if hi - lo_guess < k * MIN_LINE_S:
                        hi = dur
            if prev is not None:
                lo = float(prev["end"])
            else:
                lo = max(0.0, hi - k * CADENCE_S)
            lo = min(lo, hi)
            if hi - lo < k * MIN_LINE_S and prev is not None:
                # Sin lugar (p. ej. la línea anterior llega hasta el fin del
                # audio): se le acorta la cola a la anterior, nunca su inicio.
                new_lo = max(float(prev["start"]) + MIN_LINE_S, hi - k * MIN_LINE_S)
                if new_lo < lo:
                    prev["end"] = round(new_lo, 3)
                    lo = new_lo
            step = (hi - lo) / k
            if step < FLOOR_LINE_S:
                logger.warning(
                    "[REF-LINES] sin lugar para %d línea(s) entre %.2f y %.2f s",
                    k, lo, hi,
                )
                return aligned, stats
            # The neighbours' word stamps must stay out of the reserved window:
            # the final timing pass re-frames every line to its own words, so a
            # stamp left inside [lo, hi] would stretch the neighbour back over
            # the restored lines and undo the reservation.
            if prev is not None:
                _clip_words(prev, hi=lo)
            if nxt is not None:
                _clip_words(nxt, lo=hi)
            gap = _GAP_S if step > 3 * _GAP_S else 0.0
            for offset, pos in enumerate(range(first, last + 1)):
                start = lo + step * offset
                rows[pos] = {
                    "start": round(start, 3),
                    "end": round(start + step - gap, 3),
                    "text": lines[pos],
                    "interpolated": True,
                    "review": True,
                    "review_reasons": [REVIEW_REASON],
                }
                stats["interpolated"] += 1
        if stats["interpolated"]:
            logger.info(
                "[REF-LINES] %d línea(s) repuestas por interpolación "
                "(faltaban %d, sin ventana %d)",
                stats["interpolated"], stats["missing"], stats["invalid_timing"],
            )
        return rows, stats
    except Exception as exc:  # pragma: no cover - nunca romper el anclado
        logger.warning("[REF-LINES] completar declinó: %r", exc)
        return aligned, stats


def build_contract(segments: list[dict]) -> dict:
    """Foto de las filas ancladas: la forma que el editor tiene que recibir."""
    return {
        "schema": "reference-line-contract-v1",
        "segments": [dict(seg) for seg in segments if isinstance(seg, dict)],
    }


def _match_rows(final: list[dict], expected: list[dict]) -> list[tuple[int, int]]:
    """Order-preserving pairs ``(final_index, expected_index)`` of equal text.

    Text alone cannot tell two occurrences of a chorus apart: with the anchor
    ``A B A B C`` and survivors ``A B C``, a longest-block match pairs the
    survivors with the SECOND ``A B`` and the deleted rows get restored at the
    wrong place. This DP keeps the maximum number of equal-text pairs and,
    among those, the assignment whose starts lie closest to the anchored
    occurrence — the survivors' own timing identifies which occurrence they
    are."""
    final_keys = [line_key(row.get("text") or "") for row in final]
    expected_keys = [line_key(row.get("text") or "") for row in expected]

    def _start(row):
        value = _num(row.get("start"))
        return value if value is not None else 0.0

    final_starts = [_start(row) for row in final]
    expected_starts = [_start(row) for row in expected]
    n, m = len(final), len(expected)
    # best[i][j] = (matches, -distance) over final[i:] and expected[j:].
    best = [[(0, 0.0)] * (m + 1) for _ in range(n + 1)]
    for i in range(n - 1, -1, -1):
        row_next = best[i + 1]
        row_here = best[i]
        for j in range(m - 1, -1, -1):
            candidate = max(row_next[j], row_here[j + 1])
            if final_keys[i] and final_keys[i] == expected_keys[j]:
                matches, neg_distance = row_next[j + 1]
                paired = (matches + 1, neg_distance
                          - abs(final_starts[i] - expected_starts[j]))
                if paired > candidate:
                    candidate = paired
            row_here[j] = candidate
    pairs: list[tuple[int, int]] = []
    i = j = 0
    while i < n and j < m:
        if final_keys[i] and final_keys[i] == expected_keys[j]:
            matches, neg_distance = best[i + 1][j + 1]
            paired = (matches + 1, neg_distance
                      - abs(final_starts[i] - expected_starts[j]))
            if paired == best[i][j]:
                pairs.append((i, j))
                i += 1
                j += 1
                continue
        if best[i + 1][j] == best[i][j]:
            i += 1
        else:
            j += 1
    return pairs


def enforce_contract(segments, contract):
    """Restituye una fila por línea pegada, en orden. ``(segmentos, stats|None)``.

    Las filas finales cuyo texto coincide (misma clave) con su línea se
    conservan con el timing que les dejaron los post-pases, y su texto vuelve
    a ser el pegado, verbatim. Las líneas que un post-pase fundió, partió,
    reescribió o borró vuelven desde la foto del anclado. ``stats`` es None
    cuando no hubo nada que restituir."""
    if not isinstance(contract, dict):
        return segments, None
    expected = [row for row in (contract.get("segments") or []) if isinstance(row, dict)]
    if not expected or not isinstance(segments, list):
        return segments, None
    final = [row for row in segments if isinstance(row, dict)]
    if [str(row.get("text") or "") for row in final] == [
        str(row.get("text") or "") for row in expected
    ]:
        return segments, None

    taken = {j: final[i] for i, j in _match_rows(final, expected)}

    out: list[dict] = []
    restored = text_restored = 0
    for j, reference in enumerate(expected):
        row = taken.get(j)
        if row is None:
            seg = dict(reference)
            seg["reference_line_restored"] = True
            restored += 1
        else:
            seg = dict(row)
            if str(seg.get("text") or "") != str(reference.get("text") or ""):
                seg["text"] = reference.get("text")
                text_restored += 1
        out.append(seg)

    from segment_timing import normalize_segments_timing
    out = normalize_segments_timing(out)
    stats = {
        "reference_rows": len(expected),
        "final_rows_before": len(final),
        "restored": restored,
        "text_restored": text_restored,
    }
    return out, stats
