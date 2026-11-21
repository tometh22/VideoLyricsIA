"""Fin de línea = fin de la voz, medido sobre el stem de voz cacheado.

WHY
---
Hoy cada línea termina en ``última palabra + LYRIC_HOLD_S`` (0,5 s, con tope
en la línea siguiente; ``lead_in.apply_hold``). Ese número fijo no sabe si la
nota se sostiene dos segundos o si la voz se cortó en seco. Medido el
20-11-2026 sobre los stems ``mdx_extra`` ya cacheados en R2 de 71 canciones
aprobadas de UMG Argentina (holdout de 40 canciones al azar, parámetros
congelados antes de mirarlo), terminar la línea donde termina la energía de
la voz sube las líneas a ≤0,3 s del fin que dejó el operador de 63 % a ~79 %;
las que el operador alargó (notas sostenidas) de 43 % a 70 %, y los cortes
antes de tiempo bajan de 10 % a 5 %. ``STABLE_PITCH_TAIL_ENABLED=1`` (65 %) y
``VOCAL_CONTINUITY_MODE=apply`` (64 %) no llegan.

REGLA (congelada; cambiarla exige volver a medir con
``scripts/backtest_voice_end_timing.py``)
-----------------------------------------------------------------------
- Energía del stem en banda 250–4000 Hz (STFT de 1024 a 16 kHz), paso 10 ms.
- Reloj: el stem llega ~25 ms tarde respecto del audio original. El desfase
  se mide por correlación con la mezcla (``vocal_continuity.verify_stem_clock``)
  y se corrige; si no se puede verificar, se usa 0 (igual que la medición).
- Ancla = percentil 75 de la energía en los últimos 0,3 s de la última
  palabra (sin salir de la palabra). Umbral = 0,35 × ancla.
- La voz terminó cuando la energía queda bajo el umbral más de 0,10 s; el fin
  es el último cuadro con voz + 0,20 s. La búsqueda llega hasta la última
  palabra + 4 s o la línea siguiente − 0,05 s.
- Piso: la última palabra. Tope: inicio de la línea siguiente − 0,05 s (o el
  fin del audio en la última línea).
- Sin stem, sin ventana de ancla (palabra de <30 ms) o ancla inaudible
  (< 2,5 % del p90 de la canción): la línea queda como está (hold actual).

CONTRACT
--------
- ``VOICE_END_TIMING_MODE``: ``off`` (default, no calcula nada), ``observe``
  (calcula y registra la evidencia por línea, no cambia tiempos) o ``apply``.
- Sólo mueve ``end`` (alarga o recorta). Nunca toca ``start``, texto, palabras
  ni líneas bloqueadas (``locked``/``operator_locked``/``timing_locked``).
- Es parte de ``observability.runtime_timing_config``: la flota entera
  (api, Worker, ShortWorker, quality-worker, BatchWorker, BatchShortWorker)
  tiene que ver el mismo valor, cargado como variable COMPARTIDA.
- Precedencia en ``apply``: esta regla es la dueña del fin de línea. La cola
  de pitch (``STABLE_PITCH_TAIL_ENABLED``) y ``VOCAL_CONTINUITY_MODE=apply`` no
  corren; donde la regla no tiene evidencia queda el hold de ``lead_in``.
- Las funciones de cálculo son puras (numpy) y no levantan excepciones hacia
  el pipeline: ante cualquier falla devuelven los segmentos originales.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import logging
import math
import os
from typing import Any, Iterable, Mapping, Sequence

import numpy as np

logger = logging.getLogger("genly.voice_end_timing")

MODES = ("off", "observe", "apply")
POLICY_VERSION = "voice-end-v1"

# Parámetros congelados (medición del 20-11-2026).
SAMPLE_RATE = 16000
HOP_LENGTH = 160            # 10 ms
N_FFT = 1024
BAND_HZ = (250.0, 4000.0)
ANCHOR_LOOKBACK_S = 0.30
ANCHOR_PERCENTILE = 75.0
MIN_ANCHOR_FRAMES = 3
RELEASE_RATIO = 0.35
RELEASE_GAP_S = 0.10
END_PAD_S = 0.20
NEXT_LINE_GAP_S = 0.05
MAX_EXTENSION_S = 4.0
INAUDIBLE_FRACTION = 0.025  # del percentil 90 de la canción
INAUDIBLE_PERCENTILE = 90.0

LOCK_KEYS = ("locked", "operator_locked", "timing_locked")
# Una línea cuya última palabra termina pasado su fin por más que esto tiene
# un reloj de palabras viejo (p. ej. re-anclado sin words): no se toca.
_WORD_PAST_END_S = 0.02
# Cambios menores a esto no cuentan como "cambiar el fin" (redondeo).
_CHANGE_EPS_S = 0.005
# Tope del detalle por línea que se persiste en observe/apply.
_MAX_LINE_EVIDENCE = 400


def mode() -> str:
    value = os.environ.get("VOICE_END_TIMING_MODE", "off").strip().lower()
    return value if value in MODES else "off"


def owns_line_end() -> bool:
    """True cuando esta regla reemplaza a la cola de pitch y a vocal continuity."""
    return mode() == "apply"


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


@dataclass(frozen=True)
class VoiceEnvelope:
    """Energía en banda vocal del stem, en el reloj del STEM."""
    energy: np.ndarray
    hop_s: float
    offset_s: float           # tiempo del stem − tiempo del audio original
    duration_s: float         # duración del audio original
    clock: dict = field(default_factory=dict)

    @property
    def inaudible_floor(self) -> float:
        if not len(self.energy):
            return math.inf
        return float(np.percentile(self.energy, INAUDIBLE_PERCENTILE)) * INAUDIBLE_FRACTION

    def frame(self, t: float) -> int:
        return int(round((t + self.offset_s) / self.hop_s))

    def time(self, index: int) -> float:
        return index * self.hop_s - self.offset_s


def band_energy(waveform: np.ndarray, sample_rate: int = SAMPLE_RATE) -> np.ndarray:
    """sqrt(Σ potencia) de la STFT en 250–4000 Hz, un valor cada 10 ms."""
    import librosa

    spectrum = np.abs(librosa.stft(
        np.asarray(waveform, dtype=np.float32), n_fft=N_FFT, hop_length=HOP_LENGTH,
    )) ** 2
    freqs = librosa.fft_frequencies(sr=sample_rate, n_fft=N_FFT)
    band = (freqs >= BAND_HZ[0]) & (freqs <= BAND_HZ[1])
    return np.sqrt(spectrum[band].sum(axis=0)).astype(np.float32)


def load_voice_envelope(stem_path: str, mix_path: str | None = None) -> VoiceEnvelope:
    """Decodifica el stem (y la mezcla, para el reloj) a 16 kHz y mide la energía.

    El reloj del stem se verifica contra la mezcla con el mismo criterio que
    ``vocal_continuity`` (tres regiones, lag consistente). Sin mezcla o sin
    verificación el desfase queda en 0 y ``clock.verified`` en False.
    """
    import librosa

    voice, _ = librosa.load(str(stem_path), sr=SAMPLE_RATE, mono=True)
    duration = len(voice) / SAMPLE_RATE
    clock: dict = {"verified": False, "reason": "mix_unavailable", "offset_s": None}
    if mix_path:
        try:
            from vocal_continuity import verify_stem_clock

            mix, _ = librosa.load(str(mix_path), sr=SAMPLE_RATE, mono=True)
            duration = len(mix) / SAMPLE_RATE
            measured = verify_stem_clock(mix, voice, SAMPLE_RATE)
            clock = {k: measured.get(k) for k in ("verified", "reason", "offset_s",
                                                  "duration_difference_s")}
            del mix
        except Exception as exc:  # el reloj es una corrección, no un requisito
            clock = {"verified": False, "reason": f"clock_failed:{type(exc).__name__}",
                     "offset_s": None}
    offset = _number(clock.get("offset_s")) if clock.get("verified") else None
    return VoiceEnvelope(
        energy=band_energy(voice, SAMPLE_RATE),
        hop_s=HOP_LENGTH / SAMPLE_RATE,
        offset_s=offset or 0.0,
        duration_s=duration,
        clock=clock,
    )


def line_voice_end(word_start: float, word_end: float, next_start: float | None,
                   envelope: VoiceEnvelope) -> tuple[float | None, dict]:
    """Fin de la voz de una línea. ``(fin, evidencia)``; fin None = sin evidencia.

    ``next_start`` None = última línea (tope: fin del audio).
    """
    limit = (next_start - NEXT_LINE_GAP_S) if next_start is not None else envelope.duration_s
    evidence: dict = {"word_end": round(word_end, 3)}
    energy = envelope.energy
    a0 = envelope.frame(max(word_start, word_end - ANCHOR_LOOKBACK_S))
    a1 = envelope.frame(word_end)
    if a1 - a0 < MIN_ANCHOR_FRAMES or a0 < 0 or a1 >= len(energy):
        evidence["status"] = "fallback_no_anchor_window"
        return None, evidence
    anchor = float(np.percentile(energy[a0:a1 + 1], ANCHOR_PERCENTILE))
    evidence["anchor"] = round(anchor, 5)
    if anchor <= 0.0 or anchor < envelope.inaudible_floor:
        evidence["status"] = "fallback_inaudible_anchor"
        return None, evidence
    threshold = RELEASE_RATIO * anchor
    search_end = min(limit, word_end + MAX_EXTENSION_S)
    stop = min(len(energy), envelope.frame(search_end))
    last = a1
    released = False
    i = a1
    while i < stop:
        if energy[i] >= threshold:
            last = i
        elif (i - last) * envelope.hop_s > RELEASE_GAP_S:
            released = True
            break
        i += 1
    release = envelope.time(last)
    end = max(word_end, min(release + END_PAD_S, limit))
    evidence.update({
        "status": "released" if released else "censored",
        "release_s": round(release, 3),
        "proposed_end": round(end, 3),
    })
    return end, evidence


def _last_word(seg: Mapping[str, Any]) -> tuple[float, float] | None:
    words = seg.get("words")
    if not isinstance(words, list):
        return None
    best = None
    for word in words:
        if not isinstance(word, Mapping):
            continue
        start, end = _number(word.get("start")), _number(word.get("end"))
        if start is None or end is None or end < start:
            continue
        if best is None or end >= best[1]:
            best = (start, end)
    return best


def is_locked(seg: Mapping[str, Any]) -> bool:
    return any(seg.get(key) for key in LOCK_KEYS)


def plan(segments: Sequence[Mapping[str, Any]], envelope: VoiceEnvelope, *,
         eligible: Iterable[int] | None = None) -> tuple[list[dict], dict]:
    """Calcula el fin de voz de cada línea elegible. No decide si se aplica.

    Devuelve ``(segmentos_propuestos, reporte)``. ``eligible`` restringe los
    índices candidatos (None = todos); las bloqueadas nunca lo son.
    """
    allowed = None if eligible is None else set(eligible)
    counts = {"lines": len(segments), "eligible": 0, "proposed": 0,
              "would_extend": 0, "would_trim": 0, "unchanged": 0,
              "fallback_no_anchor_window": 0, "fallback_inaudible_anchor": 0,
              "locked": 0, "invalid_word_clock": 0}
    per_line: list[dict] = []
    out: list[dict] = []
    delta_sum = 0.0
    for index, seg in enumerate(segments):
        if not isinstance(seg, Mapping):
            out.append(seg)
            continue
        new = dict(seg)
        out.append(new)
        if allowed is not None and index not in allowed:
            continue
        if is_locked(seg):
            counts["locked"] += 1
            continue
        start, end = _number(seg.get("start")), _number(seg.get("end"))
        word = _last_word(seg)
        if (start is None or end is None or word is None or end < start
                or word[1] < start or word[1] > end + _WORD_PAST_END_S):
            counts["invalid_word_clock"] += 1
            continue
        next_start = None
        for later in segments[index + 1:]:
            if isinstance(later, Mapping):
                next_start = _number(later.get("start"))
                break
        counts["eligible"] += 1
        proposed, evidence = line_voice_end(word[0], word[1], next_start, envelope)
        evidence = {"index": index, "current_end": round(end, 3), **evidence}
        if proposed is None:
            counts[evidence["status"]] += 1
            per_line.append(evidence)
            continue
        counts["proposed"] += 1
        proposed = round(proposed, 3)
        delta = proposed - end
        if delta > _CHANGE_EPS_S:
            counts["would_extend"] += 1
        elif delta < -_CHANGE_EPS_S:
            counts["would_trim"] += 1
        else:
            counts["unchanged"] += 1
            per_line.append(evidence)
            continue
        delta_sum += delta
        evidence["delta_s"] = round(delta, 3)
        new["end"] = proposed
        new["voice_end_adjustment"] = {
            "policy_version": POLICY_VERSION,
            "original_end": round(end, 3),
            "applied_end": proposed,
            "release_s": evidence.get("release_s"),
            "status": evidence["status"],
            "clock_offset_s": round(envelope.offset_s, 4),
            "word_timestamps_changed": False,
        }
        per_line.append(evidence)
    changed = counts["would_extend"] + counts["would_trim"]
    report = {
        "policy_version": POLICY_VERSION,
        **counts,
        "would_change": changed,
        "mean_delta_s": round(delta_sum / changed, 3) if changed else 0.0,
        "clock": {k: envelope.clock.get(k) for k in ("verified", "reason", "offset_s")},
        "applied_offset_s": round(envelope.offset_s, 4),
        "line_evidence": per_line[:_MAX_LINE_EVIDENCE],
    }
    return out, report


def run(segments: list[dict], envelope: VoiceEnvelope | None, *,
        eligible: Iterable[int] | None = None,
        current_mode: str | None = None) -> tuple[list[dict], dict | None]:
    """Aplica la regla según el modo. ``off`` devuelve ``(segments, None)``.

    Nunca levanta: ante cualquier falla devuelve los segmentos originales con
    un reporte ``abstained``.
    """
    current = current_mode or mode()
    if current == "off":
        return segments, None
    if envelope is None:
        return segments, {"policy_version": POLICY_VERSION, "mode": current,
                          "status": "skipped", "reason": "cached_stem_unavailable",
                          "applied_count": 0, "mutated_segments": False}
    try:
        planned, report = plan(segments, envelope, eligible=eligible)
    except Exception as exc:
        logger.warning("[VOICE_END] plan falló (%s) — segmentos originales", type(exc).__name__)
        return segments, {"policy_version": POLICY_VERSION, "mode": current,
                          "status": "abstained", "reason": "plan_failed",
                          "error_type": type(exc).__name__,
                          "applied_count": 0, "mutated_segments": False}
    apply = current == "apply" and report["would_change"] > 0
    report.update({
        "mode": current,
        "status": "applied" if apply else ("observed" if current == "observe" else "unchanged"),
        "applied_count": report["would_change"] if apply else 0,
        "mutated_segments": apply,
    })
    logger.info(
        "[VOICE_END] mode=%s elegibles=%d propuestas=%d alargaría=%d recortaría=%d "
        "sin_ancla=%d inaudible=%d reloj=%s offset=%.3fs",
        current, report["eligible"], report["proposed"], report["would_extend"],
        report["would_trim"], report["fallback_no_anchor_window"],
        report["fallback_inaudible_anchor"], report["clock"].get("verified"),
        envelope.offset_s,
    )
    return (planned if apply else segments), report


def summary(report: dict | None) -> dict | None:
    """Versión corta (sin detalle por línea) para respuestas HTTP y logs."""
    if not report:
        return report
    return {k: v for k, v in report.items() if k != "line_evidence"}


def envelope_for_audio(audio_path: str) -> tuple[VoiceEnvelope | None, str | None]:
    """Stem cacheado (nunca corre Demucs) → envolvente. ``(envolvente, motivo)``.

    El archivo temporal del stem se borra antes de volver.
    """
    if not audio_path or not os.path.exists(audio_path):
        return None, "audio_unavailable"
    stem = None
    try:
        import vocal_sep

        stem = vocal_sep.separate_vocals(audio_path, cache_only=True)
        if not stem:
            return None, "cached_stem_unavailable"
        return load_voice_envelope(stem, audio_path), None
    except Exception as exc:
        logger.info("[VOICE_END] sin envolvente error_type=%s", type(exc).__name__)
        return None, f"envelope_failed:{type(exc).__name__}"
    finally:
        if stem:
            try:
                os.unlink(stem)
            except OSError:
                pass
