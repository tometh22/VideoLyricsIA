"""Señales por línea que hoy se calculan y se tiran (fase 5.2). Solo escritura.

Con ``EVIDENCE_PERSIST_V2`` encendida, cada segmento de máquina sale del
worker con ``line_signals_v2`` y las métricas de la canción con
``metrics.line_signals_v2``. Nada lee estas claves todavía: no cambian texto,
timing ni decisiones, y ``segments_hash`` (que ignora claves extra) no se
mueve. Se escriben justo antes de congelar la versión 0 (``revision 0`` /
``machine_evidence``), así quedan como evidencia pre-humana.

Señales por línea:

- ``asr_text_ratio`` / ``independent_text_ratio``: parecido fonético entre el
  texto de la línea y lo que oyó el ASR (o el testigo independiente) en su
  ventana. Es el mismo cálculo de ``audio_coverage.text_mismatches``, que hoy
  solo guarda el índice de las líneas por debajo de 0,4.
- ``voiced_gap_before_s`` / ``voiced_gap_after_s``: segundos de canto sin
  cartel en el hueco anterior/posterior (VAD del stem). Antes solo llegaba un
  total por canción y el ``coverage_warning`` del job, que no se persistía.
- ``ms_per_char`` / ``truncated``: duración por carácter con el umbral de
  ``lyrics_whisper_align`` (70 ms), sobre el timing final de cualquier camino.
- ``word_score``: mediana y mínimo del score por palabra (CTC o WhisperX).
- ``timing_validation``: marcador positivo de la validación de palabras.
  ``diagnose`` solo escribía cuando encontraba algo, y casi nunca encuentra,
  así que la ausencia no distinguía "validado" de "no evaluado".

El ``agreement`` del consenso dirigido se calcula en el quality-worker,
después de congelar la versión 0; se guarda aparte en
``transcription_quality.line_consensus_v2`` (ver ``quality_jobs``).
"""
from __future__ import annotations

import math
import os
import statistics

SCHEMA = "line-signals-v2"
TRUNCATED_MS_PER_CHAR = 70.0       # lyrics_whisper_align._TRUNCATED_MS_PER_CHAR_FLOOR
TEXT_RATIO_PAD_S = 0.3             # audio_coverage.text_mismatches
TEXT_RATIO_MIN_WINDOW_WORDS = 2
_TRUE = {"1", "true", "yes", "on"}


def enabled() -> bool:
    return os.environ.get("EVIDENCE_PERSIST_V2", "0").strip().lower() in _TRUE


def _finite(value, digits: int = 4) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return round(number, digits) if math.isfinite(number) else None


def text_ratio(segment: dict, words: list[dict]) -> dict | None:
    """Ratio fonético de la línea contra las palabras del ASR en su ventana."""
    from audio_coverage import _f, _mid, _norm_tokens
    from forced_align import _phonetic_ratio

    if not words:
        return None
    tokens = _norm_tokens(segment.get("text", ""))
    if not tokens:
        return None
    start, end = _f(segment.get("start")), _f(segment.get("end"))
    window = [w for w in words
              if start - TEXT_RATIO_PAD_S <= _mid(w) <= end + TEXT_RATIO_PAD_S]
    if len(window) < TEXT_RATIO_MIN_WINDOW_WORDS:
        return {"evaluated": False, "window_words": len(window)}
    window_tokens = _norm_tokens(" ".join(str(w.get("word", "")) for w in window))
    return {
        "evaluated": True,
        "ratio": _finite(_phonetic_ratio(tokens, window_tokens), 3),
        "window_words": len(window),
    }


def timing_validation_rows(segments: list[dict]) -> list[dict]:
    from timing_validation import SCHEMA as TV_SCHEMA, _bounds, _words, diagnose

    flagged = {f["segment_index"]: f for f in diagnose(segments)}
    rows = []
    for index, segment in enumerate(segments):
        words = _words(segment)
        bounds = [_bounds(w) for w in words]
        gaps = [right[0] - left[1] for left, right in zip(bounds, bounds[1:]) if left and right]
        finding = flagged.get(index)
        rows.append({
            "schema": TV_SCHEMA,
            "checked": True,
            "word_count": len(words),
            "unusable_words": sum(b is None for b in bounds),
            "max_internal_gap_s": _finite(max(gaps), 3) if gaps else None,
            "flagged": finding is not None,
            "reasons": list(finding["reasons"]) if finding else [],
        })
    return rows


def word_score(segment: dict) -> dict | None:
    scores = [
        s for s in (_finite(w.get("score")) for w in (segment.get("words") or [])
                    if isinstance(w, dict))
        if s is not None
    ]
    if not scores:
        return None
    return {"median": round(statistics.median(scores), 4), "min": round(min(scores), 4),
            "n": len(scores)}


def voiced_gap_neighbours(segments: list[dict], gaps: list[dict]) -> dict[int, dict]:
    """Reparte cada hueco con canto entre la línea anterior y la siguiente."""
    out: dict[int, dict] = {}
    starts = [_finite(s.get("start")) for s in segments]
    for gap in gaps or []:
        g_start, g_end, voiced = (_finite(gap.get("start")), _finite(gap.get("end")),
                                  _finite(gap.get("voiced_s"), 2))
        if g_start is None or g_end is None or voiced is None:
            continue
        before = [i for i, s in enumerate(starts) if s is not None and s < g_start]
        after = [i for i, s in enumerate(starts) if s is not None and s >= g_end - 0.05]
        if before:
            row = out.setdefault(before[-1], {})
            row["voiced_gap_after_s"] = round(row.get("voiced_gap_after_s", 0.0) + voiced, 2)
        if after:
            row = out.setdefault(after[0], {})
            row["voiced_gap_before_s"] = round(row.get("voiced_gap_before_s", 0.0) + voiced, 2)
    return out


def attach(result: dict, metrics: dict, *, asr_words, independent_words,
           voiced_gaps, voiced_gap_warn_s: float) -> bool:
    """Agrega ``line_signals_v2`` a cada segmento y a las métricas. Devuelve si escribió."""
    from transcription_quality import segments_hash

    segments = [s for s in (result.get("segments") or [])]
    if not segments or not all(isinstance(s, dict) for s in segments):
        return False
    asr = [w for w in (asr_words or []) if isinstance(w, dict)]
    independent = [w for w in (independent_words or []) if isinstance(w, dict)]
    validation = timing_validation_rows(segments)
    gaps = voiced_gap_neighbours(segments, voiced_gaps)
    enriched = []
    song_scores: list[float] = []
    truncated = low_ratio = 0
    for index, segment in enumerate(segments):
        start, end = _finite(segment.get("start")), _finite(segment.get("end"))
        chars = len(str(segment.get("text") or "").strip())
        ms_per_char = (
            round(1000.0 * (end - start) / chars, 1)
            if chars and start is not None and end is not None else None
        )
        is_truncated = ms_per_char is not None and ms_per_char < TRUNCATED_MS_PER_CHAR
        truncated += is_truncated
        asr_ratio = text_ratio(segment, asr)
        if asr_ratio and asr_ratio.get("evaluated") and (asr_ratio.get("ratio") or 0) < 0.4:
            low_ratio += 1
        scores = word_score(segment)
        song_scores.extend(
            s for s in (_finite(w.get("score")) for w in (segment.get("words") or [])
                        if isinstance(w, dict)) if s is not None
        )
        signals = {
            **dict(segment.get("line_signals_v2") or {}),
            "schema": SCHEMA,
            "asr_text_ratio": asr_ratio,
            "independent_text_ratio": text_ratio(segment, independent) if independent else None,
            "voiced_gap_before_s": gaps.get(index, {}).get("voiced_gap_before_s", 0.0),
            "voiced_gap_after_s": gaps.get(index, {}).get("voiced_gap_after_s", 0.0),
            "ms_per_char": ms_per_char,
            "truncated": is_truncated,
            "word_score": scores,
            "timing_validation": validation[index],
        }
        enriched.append({**segment, "line_signals_v2": signals})
    if segments_hash(enriched) != segments_hash(segments):
        return False                       # nunca cambiar lo que mide el hash
    result["segments"] = enriched
    voiced_gap_s = _finite(metrics.get("voiced_gap_s"), 2) or 0.0
    metrics["line_signals_v2"] = {
        "schema": SCHEMA,
        "voiced_gap_breaker": {
            "fired": voiced_gap_s >= voiced_gap_warn_s,
            "voiced_gap_s": voiced_gap_s,
            "warn_s": voiced_gap_warn_s,
            "gaps": len(voiced_gaps or []),
        },
        "word_score_median": round(statistics.median(song_scores), 4) if song_scores else None,
        "word_score_n": len(song_scores),
        "lines_truncated": truncated,
        "lines_text_ratio_below_0_4": low_ratio,
        "independent_witness": bool(independent),
    }
    return True


def consensus_row(index: int, window_id: str, evidence: dict, without_lora: dict,
                  agreed: bool) -> dict:
    """Fila de ``line_consensus_v2``: números y familias, nunca texto de la letra."""
    return {
        "segment_index": index,
        "window_id": str(window_id or ""),
        "agreement": _finite(evidence.get("agreement"), 4),
        "agreement_without_lora": _finite((without_lora or {}).get("agreement"), 4),
        "passed": bool(agreed),
        "sources": [str(s) for s in (evidence.get("sources") or [])][:8],
    }
