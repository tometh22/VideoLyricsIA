"""Timing no confiable a nivel canción (detector + re-alineado con letra oficial).

POR QUÉ EXISTE
--------------
Sobre 220 canciones aprobadas en staging (20-nov-2026, 6.728 líneas) el 11 %
de las líneas tenía la primera palabra a más de 1 s de donde la dejó el
operador, y la mitad de esas líneas estaba en 32 canciones (más del 25 % de
sus líneas mal). Las señales por línea que ya existen detectan poco: hace
falta mirar la canción entera y decirle al revisor "esta no, revisala
completa".

Desarmando esas líneas (ver ``docs/SONG_TIMING_TRUST_2026-11-20.md``): en las
32 canciones sólo un tercio es timing corrido de las mismas palabras; el
resto es letra que la máquina no puso ahí (líneas que faltan o que oyó
distinto) y que el operador agregó o reescribió. Por eso el detector combina
confianza acústica con señales de contenido.

QUÉ MIDE (cinco señales, todas guardadas ya en el documento del editor)
----------------------------------------------------------------------
- ``word_score_median``: mediana del score por palabra (CTC/WhisperX).
- ``voiced_gap_s``: segundos de canto sin cartel (``line_signals_v2``).
- ``slow_line_share``: líneas con más de 1,2 s por palabra (palabras
  estiradas sobre silencio o instrumental).
- ``witness_outside_share``: palabras del testigo whisper-1 de canción entera
  que caen fuera de todo cartel (letra cantada que no está en pantalla).
- ``control_similarity``: parecido entre la letra de la máquina y la
  transcripción de control de Gemini sin referencia.

Se combinan con una regresión logística ajustada sobre el banco (pesos y
normalización fijos abajo; sin red ni modelos). Una señal ausente vale su
mediana del banco: no empuja ni hacia "sana" ni hacia "no confiable".
Validación cruzada 5x20 sobre el banco: AUC 0,75; con el umbral de abajo
marca ~16 % de las canciones, precisión 0,34, recall 0,39, 12,7 % de las
sanas marcadas.

QUÉ HACE
--------
- Revisión rápida: ``timing_trust`` con el aviso de canción y las líneas más
  sospechosas primero. Nunca bloquea la aprobación (#1618): es prioridad.
- Pipeline (``SONG_TIMING_TRUST_MODE``): ``observe`` (default) sólo mide y
  guarda la evaluación en ``transcription_quality.song_timing_trust``;
  ``shadow`` además re-alinea contra la letra oficial certificada y guarda
  cuánto movería, sin aplicar; ``apply`` aplica ese timing; ``off`` no
  calcula nada. El re-alineado toma de la letra oficial SÓLO el tiempo: el
  texto de cada línea sigue siendo el de la máquina y lo decide el revisor
  (``transfer_timing``).

Puro, sin I/O.
"""
from __future__ import annotations

import bisect
import difflib
import math
import os
import re
import statistics
import unicodedata
from typing import Any

SCHEMA = "song-timing-trust-v1"
MODES = ("off", "observe", "shadow", "apply")

# Regresión logística sobre el banco de 220 canciones (20-nov-2026).
# (señal, media, desvío, mediana para imputar, peso)
_MODEL = (
    ("word_score_median", 0.7678, 0.1336, 0.782, -0.526),
    ("voiced_gap_log", 1.8679, 1.3005, 2.0028, 0.453),
    ("slow_line_share", 0.1128, 0.1189, 0.0714, 0.413),
    ("witness_outside_share", 0.1775, 0.1937, 0.1028, 0.311),
    ("control_similarity", 0.8154, 0.2428, 0.9055, -0.425),
)
_INTERCEPT = -0.432
# Umbral sobre el puntaje lineal. CV: 0,50 marca 20 % (P 0,33 R 0,46);
# 0,75 marca 16 % (P 0,34 R 0,39); 1,00 marca 13 % (P 0,37 R 0,33).
THRESHOLD = 0.75

SLOW_LINE_S_PER_TOKEN = 1.2
WITNESS_PAD_S = 0.5
MIN_EAR_WORDS = 20
MAX_SUSPICIOUS_LINES = 8

_REASONS = {
    "word_score_median": "el alineador está poco seguro de dónde caen las palabras",
    "voiced_gap_log": "hay canto sin cartel",
    "slow_line_share": "muchas líneas tienen palabras estiradas",
    "witness_outside_share": "el testigo oye letra fuera de los carteles",
    "control_similarity": "la transcripción de control no coincide con la letra",
}


def mode() -> str:
    value = os.environ.get("SONG_TIMING_TRUST_MODE", "observe").strip().lower()
    return value if value in MODES else "observe"


def _fold(text: Any) -> str:
    folded = unicodedata.normalize("NFKD", str(text or "").lower())
    return "".join(ch for ch in folded if not unicodedata.combining(ch))


def tokens(text: Any) -> list[str]:
    return [t.replace("'", "") for t in re.findall(r"[a-zñ0-9']+", _fold(text))]


def _num(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _segments(segments: Any) -> list[dict]:
    return [s for s in (segments or []) if isinstance(s, dict)
            and _num(s.get("start")) is not None and _num(s.get("end")) is not None]


def _timed_words(segment: dict) -> list[dict]:
    return [w for w in (segment.get("words") or []) if isinstance(w, dict)
            and _num(w.get("start")) is not None and _num(w.get("end")) is not None]


def _ear(witness: Any) -> list[tuple[str, float, float]]:
    rows = []
    for w in witness or []:
        if not isinstance(w, dict) or _num(w.get("start")) is None:
            continue
        tok = "".join(tokens(w.get("word")))
        if tok:
            start = float(w["start"])
            rows.append((tok, start, max(start, _num(w.get("end")) or start)))
    return sorted(rows, key=lambda r: r[1])


def signals(segments: Any, *, witness: Any = None, control_text: str = "",
            voiced_gap_s: float | None = None) -> dict[str, float | None]:
    """Las cinco señales de la canción. ``None`` = no hay con qué medirla."""
    segs = _segments(segments)
    if not segs:
        return {name: None for name, *_ in _MODEL}
    scores = [s for seg in segs for w in _timed_words(seg)
              if (s := _num(w.get("score"))) is not None]
    if voiced_gap_s is None:
        rows = [seg.get("line_signals_v2") for seg in segs
                if isinstance(seg.get("line_signals_v2"), dict) and seg.get("line_signals_v2")]
        if rows:
            voiced_gap_s = sum(_num(r.get("voiced_gap_after_s")) or 0.0 for r in rows)
    slow = sum(
        (float(s["end"]) - float(s["start"])) / max(1, len(tokens(s.get("text"))))
        > SLOW_LINE_S_PER_TOKEN for s in segs
    )
    ear = _ear(witness)
    outside = None
    if len(ear) >= MIN_EAR_WORDS:
        merged: list[list[float]] = []
        for lo, hi in sorted((float(s["start"]) - WITNESS_PAD_S, float(s["end"]) + WITNESS_PAD_S)
                             for s in segs):
            if merged and lo <= merged[-1][1]:
                merged[-1][1] = max(merged[-1][1], hi)
            else:
                merged.append([lo, hi])
        starts = [lo for lo, _ in merged]
        out = 0
        for _tok, a, b in ear:
            mid = (a + b) / 2
            k = bisect.bisect_right(starts, mid) - 1
            if k < 0 or mid > merged[k][1]:
                out += 1
        outside = out / len(ear)
    control = tokens(control_text)
    similarity = None
    if len(control) >= MIN_EAR_WORDS:
        machine = [t for s in segs for t in tokens(s.get("text"))]
        similarity = difflib.SequenceMatcher(None, machine, control, autojunk=False).ratio()
    return {
        "word_score_median": statistics.median(scores) if scores else None,
        "voiced_gap_log": math.log1p(max(0.0, voiced_gap_s)) if voiced_gap_s is not None else None,
        "slow_line_share": slow / len(segs),
        "witness_outside_share": outside,
        "control_similarity": similarity,
    }


def score(values: dict[str, float | None]) -> tuple[float, dict[str, float]]:
    total, parts = _INTERCEPT, {}
    for name, mean, sd, median, weight in _MODEL:
        value = values.get(name)
        part = weight * (((median if value is None else value) - mean) / sd)
        parts[name] = part
        total += part
    return total, parts


def suspicious_lines(segments: Any, *, witness: Any = None,
                     limit: int = MAX_SUSPICIOUS_LINES) -> list[dict]:
    """Líneas de la máquina por sospecha (índice en ``segments``).

    En las 32 canciones malas del banco, las 3 primeras de esta lista eran
    líneas que el operador movió el 48 % de las veces (al azar, 29 %)."""
    segs = [s for s in (segments or []) if isinstance(s, dict)]
    ear = _ear(witness)
    ear_starts = [r[1] for r in ear]
    rows = []
    for index, seg in enumerate(segs):
        start, end = _num(seg.get("start")), _num(seg.get("end"))
        if start is None or end is None:
            continue
        nxt = next((_num(s.get("start")) for s in segs[index + 1:]
                    if _num(s.get("start")) is not None), end + 5.0)
        toks = tokens(seg.get("text"))
        word_scores = [s for w in _timed_words(seg) if (s := _num(w.get("score"))) is not None]
        med = statistics.median(word_scores) if word_scores else None
        ls = seg.get("line_signals_v2") if isinstance(seg.get("line_signals_v2"), dict) else {}
        voiced = (_num(ls.get("voiced_gap_after_s")) or 0.0) + (_num(ls.get("voiced_gap_before_s")) or 0.0)
        slow = (end - start) / max(1, len(toks))
        gap_words = (bisect.bisect_right(ear_starts, nxt - WITNESS_PAD_S)
                     - bisect.bisect_left(ear_starts, end + WITNESS_PAD_S)) if ear else 0
        gap_words = max(0, gap_words)
        unheard = None
        if ear and toks:
            near = {ear[k][0] for k in range(bisect.bisect_left(ear_starts, start - 1.0),
                                             bisect.bisect_right(ear_starts, end + 1.0))}
            unheard = 1 - sum(t in near for t in toks) / len(toks)
        value = (min(slow, 3.0) / SLOW_LINE_S_PER_TOKEN
                 + ((0.7 - med) * 3 if med is not None else 0.5)
                 + min(gap_words, 10) / 4 + min(voiced, 10.0) / 4
                 + (unheard or 0.0) * 1.5)
        reasons = []
        if slow > SLOW_LINE_S_PER_TOKEN:
            reasons.append("stretched_words")
        if med is not None and med < 0.5:
            reasons.append("low_word_score")
        if gap_words >= 3 or voiced >= 2.0:
            reasons.append("voice_without_line")
        if unheard is not None and unheard >= 0.67 and len(toks) >= 2:
            reasons.append("witness_hears_other")
        if reasons:
            rows.append({"line": index, "score": round(value, 3), "reasons": reasons,
                         "start": round(start, 2), "end": round(end, 2),
                         "segment_id": seg.get("segment_id")})
    rows.sort(key=lambda r: -r["score"])
    return rows[:limit]


def assess(segments: Any, machine_evidence: Any = None, *, witness: Any = None,
           control_text: str | None = None, voiced_gap_s: float | None = None) -> dict:
    """Veredicto de la canción. Sin texto de la letra: sólo números y motivos."""
    if witness is None or control_text is None:
        from heard_words import witness_words
        if witness is None:
            witness = witness_words(machine_evidence)
        if control_text is None:
            from lyric_review import gemini_text
            control_text = gemini_text(machine_evidence)
    values = signals(segments, witness=witness, control_text=control_text or "",
                     voiced_gap_s=voiced_gap_s)
    if not _segments(segments):
        return {"schema": SCHEMA, "unreliable": False, "score": None,
                "threshold": THRESHOLD, "signals": values, "reasons": [], "lines": []}
    total, parts = score(values)
    unreliable = total > THRESHOLD
    reasons = [name for name, part in sorted(parts.items(), key=lambda kv: -kv[1]) if part > 0.25]
    return {
        "schema": SCHEMA,
        "unreliable": bool(unreliable),
        "score": round(total, 3),
        "threshold": THRESHOLD,
        "signals": {k: (round(v, 4) if v is not None else None) for k, v in values.items()},
        "reasons": reasons,
        "reason_text": [_REASONS[name] for name in reasons],
        "lines": suspicious_lines(segments, witness=witness) if unreliable else [],
    }


# --------------------------------------------------------------------------
# Re-alineado: tiempo de la letra oficial, texto de la máquina.
# --------------------------------------------------------------------------

MIN_LINE_MAPPED = 0.6
MAX_REPLACE_ANCHOR = 2
MIN_SONG_RETIMED = 0.5
MAX_CLAMP_S = 0.3
NEIGHBOUR_GAP_S = 0.02
# Igual que LYRIC_HOLD_S (0,5 s): el cartel queda un poco después de la
# última palabra, sin pisar a la siguiente.
HOLD_S = 0.5
HOLD_GAP_S = 0.05
MIN_REFERENCE_COVERAGE = 0.5


def timing_reference_certified(report: Any, *, is_live: bool) -> tuple[bool, str]:
    """¿Sirve esta letra oficial para tomarle el TIEMPO?

    Usa la atestación de siempre (``reference_attestation``) contra lo que
    oyó el ASR, pero no exige el gate de estructura global
    (``allow_global_forced_alignment``): ese gate protege que la letra
    oficial no reemplace la estructura de la canción, y acá no la reemplaza
    (cada línea de la máquina conserva su texto y sólo toma el tiempo de SUS
    palabras). Medido: en las 15 canciones malas del banco con letra oficial,
    el gate global aprueba 1; éste, 7. Vivos no: el orden lo manda el show.
    """
    if not isinstance(report, dict):
        return False, "no_report"
    if is_live:
        return False, "live"
    if not report.get("allow_vocabulary_reconciliation"):
        return False, "vocabulary_not_attested"
    coverage = _num((report.get("metrics") or {}).get("reference_token_coverage"))
    if coverage is None or coverage < MIN_REFERENCE_COVERAGE:
        return False, "reference_mostly_unheard"
    return True, "vocabulary_attested"


def _official_word_stream(aligned: Any) -> list[dict]:
    out = []
    for seg in aligned or []:
        if not isinstance(seg, dict):
            continue
        for w in _timed_words(seg):
            for tok in tokens(w.get("word") if w.get("word") is not None else w.get("text")):
                out.append({"tok": tok, "start": float(w["start"]),
                            "end": max(float(w["start"]), float(w["end"])),
                            "score": _num(w.get("score"))})
    return out


def _retime_line(seg: dict, times: list[tuple[float, float, float | None] | None]) -> dict | None:
    """``times``: uno por palabra cruda del texto (None = sin ancla)."""
    if not times or times[0] is None or times[-1] is None:
        return None
    raw = str(seg.get("text") or "").split()
    filled: list[tuple[float, float, float | None]] = []
    k = 0
    while k < len(times):
        if times[k] is not None:
            filled.append(times[k])
            k += 1
            continue
        j = k
        while times[j] is None:
            j += 1
        left, right = filled[-1][1], times[j][0]
        step = max(0.0, right - left) / (j - k + 1)
        for m in range(j - k):
            a = left + step * m
            filled.append((a, a + step, None))
        k = j
    words = []
    for word, (a, b, sc) in zip(raw, filled):
        row = {"word": word, "start": round(a, 3), "end": round(max(a, b), 3)}
        if sc is not None:
            row["score"] = round(sc, 4)
        words.append(row)
    for left, right in zip(words, words[1:]):
        if right["start"] < left["start"]:
            return None
    out = dict(seg)
    out["start"], out["end"] = words[0]["start"], max(w["end"] for w in words)
    out["words"] = words
    scored = [w["score"] for w in words if "score" in w]
    if scored:
        out["alignment_score"] = round(statistics.fmean(scored), 4)
    out["official_timing"] = True
    return out


def transfer_timing(machine_segments: list[dict], aligned_segments: list[dict]) -> tuple[list[dict], dict]:
    """Copia a cada línea de la máquina el tiempo que el alineado forzado
    contra la letra oficial le dio a ESAS palabras. El texto no cambia.

    Una línea se re-timea sólo si su primera y su última palabra coinciden
    con la letra oficial y al menos el 60 % de sus palabras está anclado;
    las del medio sin ancla se reparten entre sus vecinas. Si queda menos
    de la mitad de las líneas re-timeadas (otra versión de la canción), no
    se cambia nada. Nunca deja una línea encima de su vecina."""
    from heard_words import aligned_opcodes

    segs = [dict(s) for s in machine_segments or [] if isinstance(s, dict)]
    official = _official_word_stream(aligned_segments)
    stats = {"lines": len(segs), "retimed": 0, "moved_over_1s": 0,
             "median_shift_s": None, "applied": False, "reason": ""}
    if not segs or len(official) < 5:
        stats["reason"] = "no_official_words"
        return segs, stats
    # Una entrada por palabra cruda del texto de cada línea (pueden dar 0 o
    # más tokens); se ancla por el primer token de cada palabra.
    stream: list[tuple[int, int, str]] = []
    for li, seg in enumerate(segs):
        for wi, raw in enumerate(str(seg.get("text") or "").split()):
            toks = tokens(raw)
            if toks:
                stream.append((li, wi, toks[0]))
    ops = aligned_opcodes([t for *_x, t in stream], [o["tok"] for o in official])
    # Ancla = misma palabra en la letra oficial. Una palabra mal oída en el
    # mismo lugar (reemplazo corto, uno a uno) da posición pero no cuenta
    # para decidir si la línea coincide.
    anchor: dict[tuple[int, int], tuple[float, float, float | None]] = {}
    same: set[tuple[int, int]] = set()
    for tag, i1, i2, j1, j2 in ops:
        if tag == "equal" or (tag == "replace" and i2 - i1 == j2 - j1 <= MAX_REPLACE_ANCHOR):
            for k in range(i2 - i1):
                li, wi, _tok = stream[i1 + k]
                o = official[j1 + k]
                anchor[(li, wi)] = (o["start"], o["end"], o["score"] if tag == "equal" else None)
                if tag == "equal":
                    same.add((li, wi))
    proposed: list[dict | None] = []
    for li, seg in enumerate(segs):
        raw = str(seg.get("text") or "").split()
        keys = [(li, wi) for wi, w in enumerate(raw) if tokens(w)]
        if len(keys) < 2:
            proposed.append(None)
            continue
        mapped = sum(k in same for k in keys)
        if mapped / len(keys) < MIN_LINE_MAPPED:
            proposed.append(None)
            continue
        times = [anchor.get((li, wi)) if tokens(w) else None for wi, w in enumerate(raw)]
        # Palabras sin tokens (signos sueltos) no anclan pero cuentan como
        # posición: se interpolan.
        proposed.append(_retime_line(seg, times))
    # Orden y vecinas: un re-timeado que pisa a su vecina más de MAX_CLAMP_S
    # se descarta; si la pisa menos, se recorta el fin de la anterior.
    final = [p if p is not None else s for p, s in zip(proposed, segs)]
    for _ in range(len(final)):
        changed = False
        for i in range(1, len(final)):
            prev, cur = final[i - 1], final[i]
            overlap = float(prev["end"]) - float(cur["start"])
            if overlap <= 0:
                continue
            if overlap <= MAX_CLAMP_S and float(cur["start"]) - NEIGHBOUR_GAP_S > float(prev["start"]):
                trimmed = dict(prev)
                trimmed["end"] = round(float(cur["start"]) - NEIGHBOUR_GAP_S, 3)
                if prev.get("words"):
                    trimmed["words"] = [
                        {**w, "end": max(_num(w.get("start")) or 0.0,
                                         min(_num(w.get("end")) or 0.0, trimmed["end"]))}
                        if _num(w.get("start")) is not None else w
                        for w in prev["words"] if isinstance(w, dict)
                    ]
                final[i - 1] = trimmed
            elif cur is not segs[i] and cur.get("official_timing"):
                final[i] = segs[i]
            elif prev is not segs[i - 1] and prev.get("official_timing"):
                final[i - 1] = segs[i - 1]
            else:
                continue
            changed = True
        if not changed:
            break
    for i, seg in enumerate(final):
        if not seg.get("official_timing"):
            continue
        limit = float(final[i + 1]["start"]) - HOLD_GAP_S if i + 1 < len(final) else float("inf")
        held = min(float(seg["end"]) + HOLD_S, limit)
        if held > float(seg["end"]):
            final[i] = {**seg, "end": round(held, 3)}
    retimed = [i for i, s in enumerate(final) if s.get("official_timing")]
    shifts = [abs(float(final[i]["start"]) - float(segs[i]["start"])) for i in retimed]
    stats.update(
        retimed=len(retimed),
        moved_over_1s=sum(x > 1.0 for x in shifts),
        median_shift_s=round(statistics.median(shifts), 3) if shifts else None,
    )
    if len(retimed) < MIN_SONG_RETIMED * len(segs):
        stats["reason"] = "official_text_does_not_cover_lines"
        return segs, stats
    stats["applied"] = True
    return final, stats
