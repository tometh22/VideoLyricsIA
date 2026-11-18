"""Avisos de sincronización de la Revisión rápida.

POR QUÉ EXISTE
--------------
De los pedidos de cambio de UMG de sep-nov 2026, ~18 % de los puntos son de
timing y ninguna regla los veía: la línea aparece antes de que empiece la voz
("comienza antes de escuchar la voz, que aparezca en 1:23"), se va antes de
que termine el canto ("alargar hasta 3:13"), llega tarde ("entra muy tarde").

La vieja ``lyric_review._rule_timing`` buscaba lo mismo con los tiempos por
palabra del testigo whisper-1, que no sirven para esto (un prototipo con ellos
puso el 92 % de sus cambios en tiempos que nadie corrigió), y estaba
desconectada desde el 30-09. Esta la reemplaza.
Acá el tiempo sale de los alineadores que ya corrieron y quedaron guardados:

- ``ctc``: las palabras alineadas a la fuerza de la propia línea (``words``
  del segmento, ``ctc_align``/``forced_align``), sólo si son exactamente las
  palabras de la línea;
- ``whisperx``: las palabras de WhisperX sobre la canción completa
  (``machine_evidence``), alineadas con la línea por texto;
- ``witness``: el testigo whisper-1 de canción entera. Su reloj es grueso;
  sólo confirma o desmiente, nunca da el tiempo.

CTC y WhisperX usan el mismo modelo acústico (wav2vec2 español), así que su
acuerdo no prueba nada por sí solo. Lo que sí es informativo es el patrón de
falla conocido del alineado forzado: la PRIMERA palabra de la línea se estira
sobre el silencio o el instrumental previo ("El" de 1,2 s, "Mucho" de 2,9 s)
y la línea aparece cuando todavía no canta nadie.

QUÉ DEVUELVE
------------
Hallazgos por línea, con el tiempo medido y el arreglo de un clic (mover el
inicio o el fin). Nunca cambia tiempos solo; el arreglo respeta a la línea
vecina (0,05 s) y no toca el texto ni los cortes. ``required`` marca lo
inequívoco: el testigo, que es otro modelo, confirma lo que miden los
alineadores. Puro, sin I/O.

``LYRIC_REVIEW_TIMING_MODE`` (``observe`` por defecto | ``enforce`` | ``off``):
``observe`` muestra los avisos como sugerencias, sin frenar nada; ``enforce``
además frena la aprobación con los inequívocos; ``off`` no los calcula. El
default es ``observe`` porque sobre las versiones que UMG aceptó deja ~0,2
avisos por canción y ninguno bloquea ni cambia tiempos: se puede validar en
staging sin tocar variables. Medición y decisión en
``docs/LYRIC_TIMING_REVIEW_2026-11-18.md``.
"""
from __future__ import annotations

import bisect
import os
from typing import Any

from heard_words import _f, norm_token, witness_words

MODES = ("off", "observe", "enforce")

# La línea aparece así de antes de la voz (umbrales medidos contra los
# pedidos de UMG y las versiones que aceptó; ver el doc).
EARLY_START_S = 0.6
# Primera palabra CTC que dura al menos esto y contiene el comienzo que oye
# WhisperX: se estiró sobre el silencio previo.
STRETCHED_WORD_S = 0.8
# Sin el testigo, el patrón de la palabra estirada sólo vale con al menos
# esto de pantalla sin voz ("El" de 1,2 s, "Hoy" de 4,3 s). Por debajo, en
# las versiones aceptadas había 3 veces más avisos que correcciones.
STRETCHED_ONLY_EARLY_S = 1.0
# Desde acá ya no es "un poco antes": la línea está en pantalla sin voz.
NO_VOICE_S = 2.0
NO_VOICE_SHARE = 0.5
# La línea empieza así de tarde respecto de la voz.
LATE_START_S = 0.5
# La línea se va así de antes de que termine la última palabra.
EARLY_END_S = 1.0
# Dos alineadores "coinciden" en un borde si los separa menos que esto.
AGREE_S = 0.3
AGREE_END_S = 0.5
# Lo que el reloj del testigo whisper-1 puede errar y aun así confirmar.
WITNESS_SLACK_S = 0.4
# El arreglo deja la línea así de antes de la voz / así después del canto.
LEAD_IN_S = 0.1
HOLD_S = 0.15
# Igual que ``retime`` del editor: nunca pisar a la vecina.
NEIGHBOUR_GAP_S = 0.05
# Ventana alrededor de la línea para buscar sus palabras en un oído.
SEARCH_PAD_S = 3.0
# Un arreglo que mueve menos que esto no vale un aviso.
MIN_MOVE_S = 0.3
MIN_LINE_S = 0.3
# Una línea de una sola palabra ("Abrázame", "Sola") no se puede ubicar por
# texto con seguridad: cualquier repetición cercana coincide. Medido: 0 de 11
# avisos así correspondían a una corrección.
MIN_TOKENS = 2
MAX_FINDINGS = 40


def mode() -> str:
    value = os.environ.get("LYRIC_REVIEW_TIMING_MODE", "observe").strip().lower()
    return value if value in MODES else "observe"


def _tokens(text: Any) -> list[str]:
    return [t for t in (norm_token(p) for p in str(text or "").split()) if t]


def _word_rows(raw: Any) -> list[dict]:
    out = []
    for w in raw or []:
        if not isinstance(w, dict) or w.get("start") is None or w.get("end") is None:
            continue
        tok = norm_token(w.get("word") if w.get("word") is not None else w.get("text"))
        if not tok:
            continue
        start, end = _f(w.get("start")), _f(w.get("end"))
        out.append({"tok": tok, "start": start, "end": max(start, end)})
    return out


def whisperx_words(machine_evidence: Any) -> list[dict]:
    """Palabras de WhisperX sobre la canción entera, en tiempo absoluto.

    Las ventanas acotadas (``replicate_raw:offset=…``) guardan tiempos
    relativos al recorte y no sirven para ubicar una línea."""
    if not isinstance(machine_evidence, dict):
        return []
    best: list[dict] = []
    for hyp in machine_evidence.get("hypotheses_by_family") or []:
        if not isinstance(hyp, dict) or hyp.get("kind") != "segments":
            continue
        if "whisperx" not in str(hyp.get("family") or ""):
            continue
        if hyp.get("transformation") not in {"replicate_raw", "cache_hit_raw"}:
            continue
        rows = []
        for ev in hyp.get("events") or []:
            if isinstance(ev, dict):
                rows.extend(_word_rows(ev.get("words")))
        if len(rows) > len(best):
            best = rows
    return sorted(best, key=lambda w: w["start"])


def _witness(machine_evidence: Any) -> list[dict]:
    rows = [{"tok": norm_token(w["word"]), "start": w["start"], "end": w["end"]}
            for w in witness_words(machine_evidence)]
    return sorted((w for w in rows if w["tok"]), key=lambda w: w["start"])


def _ctc_words(seg: dict) -> list[dict]:
    """Las palabras alineadas de la línea, sólo si son exactamente su texto
    y están donde está la línea (una persona pudo moverla o reescribirla
    después: entonces esos relojes ya no describen lo que se ve)."""
    rows = _word_rows(seg.get("words"))
    if not rows or [w["tok"] for w in rows] != _tokens(seg.get("text")):
        return []
    start, end = _f(seg.get("start")), _f(seg.get("end"))
    if rows[0]["start"] >= end or rows[-1]["end"] <= start:
        return []
    if abs(rows[0]["start"] - start) > SEARCH_PAD_S or abs(rows[-1]["end"] - end) > SEARCH_PAD_S + 1:
        return []
    return rows


class _Stream:
    def __init__(self, words: list[dict]):
        self.words = words
        self.starts = [w["start"] for w in words]

    def window(self, lo: float, hi: float) -> list[dict]:
        a = bisect.bisect_left(self.starts, lo)
        b = bisect.bisect_right(self.starts, hi)
        return self.words[a:b]


def _expected_times(tokens: list[str], start: float, end: float, ctc: list[dict]) -> list[float]:
    if ctc:
        return [w["start"] for w in ctc]
    step = (end - start) / max(1, len(tokens))
    return [start + k * step for k in range(len(tokens))]


def _locate(tokens: list[str], expected: list[float], stream: _Stream,
            lo: float, hi: float) -> dict[int, tuple[dict, int]]:
    """Palabra del oído para cada palabra de la línea, en orden, con el largo
    del tramo seguido que coincide.

    Es un alineamiento por texto que, entre dos apariciones iguales (un coro
    que se repite dentro de la ventana), prefiere la más cercana a donde
    debería estar la palabra: un emparejamiento por texto solo agarraba la
    primera repetición y la línea parecía corrida varios segundos."""
    cand = stream.window(lo, hi)
    n, m = len(tokens), len(cand)
    if not n or not m or n * m > 20000:
        return {}
    # score[i][j]: mejor puntaje alineando tokens[:i] con cand[:j].
    score = [[0.0] * (m + 1) for _ in range(n + 1)]
    for i in range(1, n + 1):
        row, up = score[i], score[i - 1]
        tok, at = tokens[i - 1], expected[i - 1]
        for j in range(1, m + 1):
            best = max(up[j], row[j - 1])
            if cand[j - 1]["tok"] == tok:
                gain = 1.0 - min(abs(cand[j - 1]["start"] - at), 6.0) / 12.0
                best = max(best, up[j - 1] + gain)
            row[j] = best
    pairs: list[tuple[int, int]] = []
    i, j = n, m
    while i > 0 and j > 0:
        if cand[j - 1]["tok"] == tokens[i - 1]:
            gain = 1.0 - min(abs(cand[j - 1]["start"] - expected[i - 1]), 6.0) / 12.0
            if abs(score[i][j] - (score[i - 1][j - 1] + gain)) < 1e-9:
                pairs.append((i - 1, j - 1))
                i, j = i - 1, j - 1
                continue
        if score[i][j - 1] >= score[i - 1][j]:
            j -= 1
        else:
            i -= 1
    pairs.reverse()
    out: dict[int, tuple[dict, int]] = {}
    k = 0
    while k < len(pairs):
        run = 1
        while k + run < len(pairs) and pairs[k + run][0] == pairs[k][0] + run \
                and pairs[k + run][1] == pairs[k][1] + run:
            run += 1
        for r in range(run):
            a, b = pairs[k + r]
            out[a] = (cand[b], run)
        k += run
    return out


def _edge(found: dict, index: int, n_tokens: int) -> dict | None:
    """El borde de la línea según un oído, sólo si esa palabra coincide como
    parte de un tramo de dos o más (una palabra suelta en una ventana de
    seis segundos puede ser cualquier repetición)."""
    hit = found.get(index)
    if hit is None:
        return None
    word, run = hit
    if run < 2 and n_tokens > 1:
        return None
    return word


def _fmt(seconds: float) -> str:
    seconds = max(0.0, seconds)
    m, s = divmod(seconds, 60)
    return f"{int(m)}:{s:04.1f}".replace(".", ",")


def find_timing_issues(segments: list[dict], machine_evidence: Any = None) -> list[dict]:
    """Hallazgos de sincronización sobre la letra actual.

    Cada hallazgo: ``line``, ``kind`` (``starts_before_voice``,
    ``starts_after_voice``, ``ends_before_voice``), ``field``
    (``start``/``end``), ``current``, ``voice`` (tiempo medido), ``proposed``
    (el arreglo, ya acotado por las vecinas), ``sources`` y ``required``."""
    lines = [s for s in (segments or []) if isinstance(s, dict)]
    if not lines:
        return []
    wx = _Stream(whisperx_words(machine_evidence))
    wit = _Stream(_witness(machine_evidence))
    if not wx.words:
        # Sin un segundo alineador no hay nada inequívoco ni contrastable.
        return []
    out: list[dict] = []
    for i, seg in enumerate(lines):
        tokens = _tokens(seg.get("text"))
        start, end = _f(seg.get("start")), _f(seg.get("end"))
        if len(tokens) < MIN_TOKENS or end - start < MIN_LINE_S:
            continue
        prev_end = _f(lines[i - 1].get("end")) if i > 0 else None
        next_start = _f(lines[i + 1].get("start")) if i + 1 < len(lines) else None
        ctc = _ctc_words(seg)
        lo = min(start, ctc[0]["start"] if ctc else start) - SEARCH_PAD_S
        hi = max(end, ctc[-1]["end"] if ctc else end) + SEARCH_PAD_S
        expected = _expected_times(tokens, start, end, ctc)
        x = _locate(tokens, expected, wx, lo, hi)
        w = _locate(tokens, expected, wit, lo, hi)
        last = len(tokens) - 1
        x0, w0 = _edge(x, 0, len(tokens)), _edge(w, 0, len(tokens))
        xl, wl = _edge(x, last, len(tokens)), _edge(w, last, len(tokens))
        c0 = ctc[0] if ctc else None
        cl = ctc[-1] if ctc else None
        floor = (prev_end + NEIGHBOUR_GAP_S) if prev_end is not None else 0.0
        ceiling = (next_start - NEIGHBOUR_GAP_S) if next_start is not None else None
        start_finding = _early_start(start, end, floor, c0, x0, w0) or \
            _late_start(start, floor, c0, x0, w0)
        if start_finding is not None:
            out.append({"line": i, "field": "start", "current": round(start, 2), **start_finding})
        end_finding = _early_end(end, ceiling, cl, xl, wl)
        if end_finding is not None:
            out.append({"line": i, "field": "end", "current": round(end, 2), **end_finding})
    return out[:MAX_FINDINGS]


def _early_start(start, end, floor, c0, x0, w0) -> dict | None:
    """Entra antes de la voz. WhisperX da el comienzo; tiene que confirmarlo
    el testigo (otro modelo) o, sin testigo, la falla típica del alineado
    forzado: la primera palabra CTC estirada sobre el silencio previo y con
    el comienzo de WhisperX adentro, por al menos un segundo."""
    if x0 is None or x0["start"] - start < EARLY_START_S:
        return None
    onset = x0["start"]
    stretched = c0 is not None and c0["end"] - c0["start"] >= STRETCHED_WORD_S \
        and c0["start"] <= start + AGREE_S and onset <= c0["end"] + 0.1
    ctc_agrees = c0 is not None and abs(c0["start"] - onset) <= AGREE_S
    if c0 is not None and not (stretched or ctc_agrees):
        return None  # el alineador dice otra cosa: no es inequívoco
    witness = w0 is not None and w0["start"] - start >= EARLY_START_S - WITNESS_SLACK_S
    if not witness and not (stretched and onset - start >= STRETCHED_ONLY_EARLY_S):
        return None
    proposed = round(max(onset - LEAD_IN_S, floor), 2)
    if proposed - start < MIN_MOVE_S or end - proposed < MIN_LINE_S:
        return None
    silent = onset - start
    kind = "no_voice" if silent >= NO_VOICE_S or silent >= NO_VOICE_SHARE * (end - start) \
        else "starts_before_voice"
    return {"kind": kind, "voice": round(onset, 2), "proposed": proposed,
            "sources": ["whisperx"] + (["ctc"] if stretched or ctc_agrees else [])
            + (["witness"] if witness else []),
            # CTC y WhisperX comparten modelo acústico: sin el testigo, sugerencia.
            "required": witness}


def _late_start(start, floor, c0, x0, w0) -> dict | None:
    """Entra tarde: el alineador y WhisperX coinciden en que la primera
    palabra se canta antes de que aparezca la línea."""
    if x0 is None or c0 is None or c0["end"] - c0["start"] >= STRETCHED_WORD_S:
        return None  # una primera palabra estirada no da un comienzo confiable
    if abs(c0["start"] - x0["start"]) > AGREE_S or start - max(c0["start"], x0["start"]) < LATE_START_S:
        return None
    onset = min(c0["start"], x0["start"])
    proposed = round(max(onset - LEAD_IN_S, floor), 2)
    if start - proposed < MIN_MOVE_S:
        return None  # la línea anterior no deja lugar
    witness = w0 is not None and start - w0["start"] >= LATE_START_S - WITNESS_SLACK_S
    return {"kind": "starts_after_voice", "voice": round(onset, 2), "proposed": proposed,
            "sources": ["ctc", "whisperx"] + (["witness"] if witness else []),
            "required": witness}


def _early_end(end, ceiling, cl, xl, wl) -> dict | None:
    """Se va antes de que termine la voz: WhisperX y el testigo siguen oyendo
    la última palabra (el alineador, si la tiene, no lo contradice). Sin el
    testigo no se avisa: CTC+WhisperX solos acertaron 1 de 7."""
    if xl is None or xl["end"] - end < EARLY_END_S:
        return None
    offset = xl["end"]
    if cl is not None:
        if abs(cl["end"] - offset) > AGREE_END_S:
            return None
        offset = max(offset, cl["end"])
    if wl is None or wl["end"] - end < EARLY_END_S - WITNESS_SLACK_S - 0.3:
        return None
    proposed = offset + HOLD_S if ceiling is None else min(offset + HOLD_S, ceiling)
    proposed = round(proposed, 2)
    if proposed - end < MIN_MOVE_S:
        return None  # la línea siguiente no deja lugar
    return {"kind": "ends_before_voice", "voice": round(offset, 2), "proposed": proposed,
            "sources": ["whisperx"] + (["ctc"] if cl is not None else []) + ["witness"],
            "required": True}


_TEXT = {
    "starts_before_voice": ("Entra antes de la voz", "Mover el inicio"),
    "no_voice": ("Línea en pantalla sin voz", "Mover el inicio"),
    "starts_after_voice": ("Entra tarde", "Adelantar el inicio"),
    "ends_before_voice": ("Se va antes de que termine la voz", "Alargar"),
}
_SOURCE = {"ctc": "el alineador", "whisperx": "WhisperX", "witness": "el testigo"}


def describe(finding: dict) -> dict:
    """Título, explicación y botón en castellano para el editor."""
    title, action = _TEXT[finding["kind"]]
    names = [_SOURCE[s] for s in ("ctc", "whisperx", "witness") if s in finding["sources"]]
    who = names[0] if len(names) == 1 else ", ".join(names[:-1]) + " y " + names[-1]
    voice, current = _fmt(finding["voice"]), _fmt(finding["current"])
    if finding["kind"] == "starts_before_voice":
        why = f"Aparece en {current} y la voz empieza en {voice} (según {who})"
    elif finding["kind"] == "no_voice":
        silent = f"{finding['voice'] - finding['current']:.1f}".replace(".", ",")
        why = f"Está {silent} s en pantalla sin que se cante: la voz empieza en {voice} (según {who})"
    elif finding["kind"] == "starts_after_voice":
        why = f"La voz empieza en {voice} y la línea recién en {current} (según {who})"
    else:
        why = f"Se va en {current} y la voz sigue hasta {voice} (según {who})"
    return {"title": title, "why": why, "action": f"{action} a {_fmt(finding['proposed'])}"}
