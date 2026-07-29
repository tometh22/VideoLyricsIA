"""Re-segmentación de carteles por programación dinámica sobre word-stamps.

LA BRECHA (medida contra Rotor, 28-07-2026)
-------------------------------------------
Nuestros carteles promedian 11-38 palabras y ~10 s en pantalla; Rotor corta
en frases de ~5,8 palabras y ~4,8 s. Mismo contenido, la mitad de
legibilidad: donde Rotor pasa dos versos cortos al ritmo del canto,
nosotros dejamos un bloque clavado. `_split_long_segments` (el corte
actual) solo parte por el gap más grande — no tiene noción de largo
objetivo ni de frase.

EL MÉTODO
---------
DP global sobre el stream de palabras del segmento: elige el reparto de
cortes que minimiza

    costo(cartel) = (n_palabras − target)²
                  + dur_w · max(0, duración − max_dur)²
                  − gap_w · min(silencio_tras_el_corte, gap_cap)

O sea: carteles cerca del largo objetivo, sin bloques eternos, y cortando
donde el cantante respira. Validado en 5 canciones reales de cadencias muy
distintas (baladas y rock): 11-38 pal/cartel → 4,8-5,8, carteles
problemáticos (>7 s o >10 palabras) de 5-17 → 0-3, siempre O(N·max_len).

GUARDAS
-------
- `words_trusted`: NO se parte un segmento cuyas words fueron re-atadas
  por posición (el camino referencia re-ata `words` secuencialmente, no
  por tiempo — partir con esas words mentirosas dispersaría texto
  arbitrario). Se exige monotonía, contención en el segmento y que las
  words SUENEN al texto del cartel.
- Se preserva la puntuación curada: si el texto tiene tantos tokens como
  words, cada carta es un slice del TEXTO original (con sus comas y ¿¡).
- Lead/hold manual SOLO en los bordes nuevos (lead_in.polish no es
  idempotente; la primera carta hereda el start original, que ya lo trae).
- PHRASE_SEGMENTER_ENABLED default off. Puro, nunca levanta desde el
  wrapper (los helpers sí pueden — el wrapper los envuelve).
"""
from __future__ import annotations

import logging
import os

logger = logging.getLogger("genly.phrase_segmenter")

_TRUE = ("1", "true", "yes", "on")

_MIN_GAP_S = 0.01     # mismo gap mínimo entre carteles que usa lead_in
_MIN_CARD_S = 0.1     # duración mínima de una carta tras clamps


def is_enabled() -> bool:
    return os.environ.get(
        "PHRASE_SEGMENTER_ENABLED", "0"
    ).strip().lower() in _TRUE


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default


def _f(v, default: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _tokens(text: str) -> list[str]:
    import unicodedata as _u
    s = _u.normalize("NFD", (text or "").lower())
    s = "".join(c for c in s if c.isalnum() or c.isspace())
    return [t for t in s.split() if t]


def words_trusted(seg: dict) -> bool:
    """¿Las words de este segmento describen de verdad su audio?

    El camino referencia re-ata las words POR POSICIÓN secuencial
    (whisperx_reconcile re-bucketea el stream), así que pueden no
    corresponder ni al tiempo ni al texto del cartel. Partir un cartel
    con words mentirosas dispersa texto arbitrario por la pantalla —
    mejor no partir."""
    words = seg.get("words") or []
    if len(words) < 2 or not all(isinstance(w, dict) for w in words):
        return False
    starts = [_f(w.get("start")) for w in words]
    ends = [_f(w.get("end")) for w in words]
    if any(b < a - 1e-6 for a, b in zip(starts, starts[1:])):
        return False                      # stamps no monótonos
    a, b = _f(seg.get("start")), _f(seg.get("end"))
    if starts[0] < a - 1.0 or ends[-1] > b + 1.0:
        return False                      # words fuera del segmento
    from forced_align import _phonetic_ratio
    wt = _tokens(" ".join(str(w.get("word", "")) for w in words))
    tt = _tokens(seg.get("text", ""))
    if not wt or not tt:
        return False
    return _phonetic_ratio(tt, wt) >= 0.8


def segment_words(words: list[dict], *, target_len: int = 6,
                  max_len: int = 11, len_w: float = 1.0,
                  dur_w: float = 3.0, max_dur: float = 5.0,
                  gap_w: float = 2.5, gap_cap: float = 1.5
                  ) -> list[list[dict]]:
    """Reparto globalmente óptimo del stream de palabras en carteles.
    Puro y determinista. Devuelve la lista de grupos de words, en orden,
    cubriendo todas las palabras exactamente una vez."""
    n = len(words)
    if n == 0:
        return []
    if n == 1:
        return [list(words)]

    def gap_after(i: int) -> float:
        if i + 1 >= n:
            return gap_cap                # fin del stream = corte gratis
        return max(0.0, _f(words[i + 1].get("start"))
                   - _f(words[i].get("end")))

    INF = float("inf")
    best = [INF] * (n + 1)
    prev = [0] * (n + 1)
    best[0] = 0.0
    for j in range(1, n + 1):
        for i in range(max(0, j - max_len), j):
            length = j - i
            dur = _f(words[j - 1].get("end")) - _f(words[i].get("start"))
            cost = (len_w * (length - target_len) ** 2
                    + dur_w * max(0.0, dur - max_dur) ** 2
                    - gap_w * min(gap_after(j - 1), gap_cap))
            if best[i] + cost < best[j]:
                best[j] = best[i] + cost
                prev[j] = i
    cuts: list[tuple[int, int]] = []
    j = n
    while j > 0:
        cuts.append((prev[j], j))
        j = prev[j]
    cuts.reverse()
    return [list(words[i:j]) for i, j in cuts]


def resegment(segments: list[dict], *, lead_s: float = 0.0,
              hold_s: float = 0.0) -> list[dict]:
    """Parte los carteles largos en frases. Los cortos, los que no tienen
    words y los de words no confiables pasan intactos (mismo objeto)."""
    target_len = _env_int("PHRASE_SEG_TARGET_LEN", 6)
    max_len = _env_int("PHRASE_SEG_MAX_LEN", 11)
    max_dur = _env_float("PHRASE_SEG_MAX_DUR_S", 5.0)
    gap_cap = _env_float("PHRASE_SEG_GAP_CAP_S", 1.5)
    lead = max(0.0, float(lead_s or 0.0))
    hold = max(0.0, float(hold_s or 0.0))

    out: list[dict] = []
    for seg in segments or []:
        if not isinstance(seg, dict):
            out.append(seg)
            continue
        words = seg.get("words") or []
        n = len(words)
        dur = _f(seg.get("end")) - _f(seg.get("start"))
        needs_split = n > max_len or dur > max_dur
        if n < 4 or not needs_split or not words_trusted(seg):
            out.append(seg)
            continue
        groups = segment_words(words, target_len=target_len,
                               max_len=max_len, max_dur=max_dur,
                               gap_cap=gap_cap)
        if len(groups) <= 1:
            out.append(seg)
            continue

        toks = (seg.get("text") or "").split()
        use_slice = len(toks) == n        # preservar puntuación curada
        raw_s = [_f(g[0].get("start")) for g in groups]
        raw_e = [_f(g[-1].get("end")) for g in groups]

        cards: list[dict] = []
        wi = 0
        prev_end: float | None = None
        for gi, g in enumerate(groups):
            new = dict(seg)
            new.pop("ctc_lr", None)           # telemetría de la línea entera
            new.pop("repetition_group", None)  # se re-anota tras partir
            if use_slice:
                new["text"] = " ".join(toks[wi:wi + len(g)]).strip()
            else:
                new["text"] = " ".join(
                    str(w.get("word", "")).strip() for w in g
                ).strip()
            wi += len(g)
            new["words"] = [dict(w) for w in g]
            new["phrase_split"] = True

            if gi == 0:
                # La primera carta hereda el start original: ya trae el
                # lead del pipeline (lead_in.polish no es idempotente).
                start = min(_f(seg.get("start")), raw_s[0])
            else:
                start = max(prev_end + _MIN_GAP_S, raw_s[gi] - lead)
            if gi == len(groups) - 1:
                end = max(raw_e[gi], _f(seg.get("end")))
            else:
                end = raw_e[gi] + hold
                nxt = raw_s[gi + 1] - lead
                end = min(end, max(nxt, start + _MIN_CARD_S) - _MIN_GAP_S)
            end = max(end, start + _MIN_CARD_S)
            new["start"] = round(start, 3)
            new["end"] = round(end, 3)
            prev_end = end
            cards.append(new)
        out.extend(cards)
    return out
