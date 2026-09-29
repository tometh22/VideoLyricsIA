"""Palabras que se escuchan y no están en la letra.

POR QUÉ EXISTE
--------------
UMG devolvía videos con reclamos del tipo "falta el QUE del principio" o
"falta la frase 'dormite ya' en dos instancias". En los dos casos del
29-09-2026 la máquina HABÍA transcripto esas palabras (score 0,82 y 0,50-0,72)
y se perdieron en una edición humana, al resolver un pedido de cambio
anterior: se pegó la cita parcial del cliente encima de la línea entera.
Ningún control lo veía: los de cobertura corren sobre la salida de la
máquina (no sobre el texto aprobado) y piden huecos de 3 s o más.

El testigo independiente (whisper-1 sobre la canción entera) que ya se
congela en ``machine_evidence`` tenía lo faltante en 4 de 5 reclamos de
omisión, incluidas frases que la máquina principal nunca transcribió.

QUÉ HACE
--------
Compara el texto actual de la letra contra dos oídos: las palabras del
testigo y las de la transcripción original de la máquina (con su score).
Alinea en orden lo oído contra la letra y se queda con lo que SOBRA en lo
oído (borrado), no con lo reemplazado (una corrección de texto es trabajo
del revisor, no un faltante). Cada tramo faltante vuelve como alerta con su
instante y dónde insertarlo. ``lyric_review`` lo muestra en el editor junto
con los demás puntos ("Agregar" / "No se canta"); la decisión se guarda en
la línea (``qa_dismissed``).

Medido el 29-09-2026 sobre 313 canciones aprobadas en staging: 56 alertas
(el 88 % de las canciones no tiene ninguna) y 4 de 5 reclamos de omisión de
UMG atrapados con la alerta exacta.

Puro, sin I/O. El editor y el backtest usan esta misma función.
"""
from __future__ import annotations

import difflib
import hashlib
import unicodedata
from typing import Any, Iterable

# Margen alrededor de cada línea para considerar que una palabra oída "le
# pertenece". Tolera el desfasaje normal entre el tiempo del testigo y el
# tiempo del cartel.
LINE_PAD_S = 1.2
# Dos palabras oídas consecutivas se agrupan si las separa menos que esto.
RUN_SPLIT_S = 1.0
# Score mínimo para que una palabra de la máquina cuente como "oída".
MACHINE_MIN_SCORE = 0.5
# Una palabra de la máquina corrobora a una del testigo si están así de cerca.
CORROBORATION_S = 1.0
MAX_ALERTS = 40

_WITNESS_TRANSFORMATIONS = {
    "live_independent_verify_raw",
    "live_independent_verify_mix_raw",
}
# El testigo alucina estas fórmulas en silencios y aplausos.
_HALLUCINATIONS = (
    "amara", "subtitul", "suscrib", "gracias por ver", "thanks for watching",
)


def _f(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def norm_token(token: str) -> str:
    folded = unicodedata.normalize("NFKD", str(token or "").lower())
    return "".join(ch for ch in folded if ch.isalnum() and not unicodedata.combining(ch))


def _tokens(text: str) -> list[str]:
    return [t for t in (norm_token(part) for part in str(text or "").split()) if t]


def _same(a: str, b: str) -> bool:
    if a == b:
        return True
    if min(len(a), len(b)) < 4:
        return False
    return difflib.SequenceMatcher(a=a, b=b, autojunk=False).ratio() >= 0.8


def witness_words(machine_evidence: Any) -> list[dict]:
    """Palabras del testigo de canción entera, en tiempo absoluto.

    Sólo se usan los streams cuyo origen es la canción completa desde 0 s
    (rol ``independent`` o la verificación en vivo). Las ventanas acotadas
    guardan tiempos relativos a su recorte y no sirven para ubicar nada.
    """
    if not isinstance(machine_evidence, dict):
        return []
    streams: list[list[dict]] = []
    for hyp in machine_evidence.get("hypotheses_by_family") or []:
        if not isinstance(hyp, dict) or hyp.get("kind") != "word_stream":
            continue
        family = str(hyp.get("family") or "")
        if not family.startswith("openai/whisper"):
            continue
        whole_song = (
            hyp.get("role") == "independent"
            or hyp.get("transformation") in _WITNESS_TRANSFORMATIONS
        )
        if not whole_song:
            continue
        rows = []
        for ev in hyp.get("events") or []:
            if not isinstance(ev, dict) or ev.get("start") is None:
                continue
            word = str(ev.get("word") or "").strip()
            if not word:
                continue
            rows.append({
                "word": word, "start": _f(ev.get("start")),
                "end": max(_f(ev.get("end")), _f(ev.get("start"))),
            })
        if rows:
            streams.append(rows)
    # La verificación puede correr sobre el stem y sobre la mezcla: se usa el
    # stream más completo, no la unión (duplicaría cada palabra).
    return max(streams, key=len) if streams else []


def machine_words(original_segments: Any) -> list[dict]:
    out = []
    for seg_index, seg in enumerate(original_segments or []):
        if not isinstance(seg, dict):
            continue
        for w in seg.get("words") or []:
            if not isinstance(w, dict) or w.get("start") is None:
                continue
            word = str(w.get("word") or "").strip()
            if not word:
                continue
            out.append({
                "word": word, "start": _f(w.get("start")),
                "end": max(_f(w.get("end")), _f(w.get("start"))),
                "score": _f(w.get("score"), 0.0),
                "_seg": seg_index,
            })
    return out


def _final_tokens(segments: list[dict]) -> list[dict]:
    out = []
    for i, seg in enumerate(segments):
        if not isinstance(seg, dict):
            continue
        a, b = _f(seg.get("start")), _f(seg.get("end"))
        for tok in _tokens(seg.get("text", "")):
            out.append({"tok": tok, "line": i, "start": a, "end": b})
    return out


def _near_line(word: dict, final: list[dict], pad: float) -> list[str]:
    mid = (word["start"] + word["end"]) / 2
    return [f["tok"] for f in final if f["start"] - pad <= mid <= f["end"] + pad]


def _deleted_words(heard: list[dict], final: list[dict]) -> list[dict]:
    """Palabras oídas que la letra no tiene: borradas, no reemplazadas.

    Alinea en orden la secuencia oída contra la de la letra. Un bloque en el
    que la letra tiene tantas palabras como las oídas es una corrección de
    texto (reloj -> reloco) y no se alerta; sólo cuenta el exceso de lo oído.
    Además la palabra no debe estar en ninguna línea cercana en el tiempo,
    para que un coro repetido no confunda el alineado.
    """
    h_tok = [norm_token(w["word"]) for w in heard]
    f_tok = [f["tok"] for f in final]
    sm = difflib.SequenceMatcher(a=h_tok, b=f_tok, autojunk=False)
    out = []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag not in ("delete", "replace"):
            continue
        block = [w for w, t in zip(heard[i1:i2], h_tok[i1:i2]) if t]
        fin = f_tok[j1:j2]
        if tag == "replace":
            # "por qué" == "porque", "suelta la" == "sueltala".
            if _same("".join(h_tok[i1:i2]), "".join(fin)):
                continue
            if len(block) <= len(fin):
                continue
            block = [w for w in block
                     if not any(_same(norm_token(w["word"]), t) for t in fin)]
            if len(block) > (i2 - i1) - len(fin):
                # Mezcla de reemplazo y borrado: quedan los del borde, que es
                # donde se pierde lo que queda fuera de una cita pegada.
                excess = (i2 - i1) - len(fin)
                block = block[:excess] if block[0] is heard[i1] else block[-excess:]
        for w in block:
            near = _near_line(w, final, LINE_PAD_S)
            tok = norm_token(w["word"])
            joined = ["".join(near[k:k + 2]) for k in range(len(near) - 1)]
            if any(_same(tok, t) for t in near + joined):
                continue
            out.append(w)
    return out


def _known_lyric_ngrams(final: list[dict], n: int = 3) -> set[tuple[str, ...]]:
    toks = [f["tok"] for f in final]
    return {tuple(toks[k:k + n]) for k in range(len(toks) - n + 1)}


def _is_loop(run: list[dict]) -> bool:
    """Whisper entra en bucle ("nadie me apura nadie me apura ...") sobre
    pasajes instrumentales o repetitivos y siembra la frase por toda la
    canción. Un tramo con menos de la mitad de palabras distintas es eso."""
    toks = [norm_token(w["word"]) for w in run]
    return len(toks) >= 6 and len(set(toks)) * 2 < len(toks)


def _runs(words: list[dict]) -> list[list[dict]]:
    runs: list[list[dict]] = []
    for w in words:
        if runs and w["_i"] == runs[-1][-1]["_i"] + 1 and \
                w["start"] - runs[-1][-1]["end"] <= RUN_SPLIT_S:
            runs[-1].append(w)
        else:
            runs.append([w])
    return runs


def alert_key(text: str, start: float) -> str:
    """Identidad estable de una alerta: texto oído + segundo de inicio.

    Las palabras oídas vienen de evidencia congelada, así que la clave no
    cambia entre guardados. Se persiste en la línea al descartar.
    """
    base = " ".join(_tokens(text))
    return f"{base}@{int(round(start))}"


def dismissed_keys(segments: Iterable[dict]) -> set[str]:
    keys: set[str] = set()
    for seg in segments or []:
        if isinstance(seg, dict):
            for key in seg.get("qa_dismissed") or []:
                if isinstance(key, str):
                    keys.add(key)
    return keys


def _nearest_line(segments: list[dict], start: float, end: float) -> int | None:
    best, best_d = None, None
    for i, seg in enumerate(segments):
        if not isinstance(seg, dict):
            continue
        a, b = _f(seg.get("start")), _f(seg.get("end"))
        d = 0.0 if a <= end and start <= b else min(abs(start - b), abs(a - end))
        if best_d is None or d < best_d:
            best, best_d = i, d
    return best


def _placement(segment: dict | None, start: float, end: float) -> str:
    if not segment:
        return "gap"
    a, b = _f(segment.get("start")), _f(segment.get("end"))
    if end <= a + 0.25:
        return "before"
    if start >= b - 0.25:
        return "after"
    return "inside"


def find_missing_heard_words(
    segments: list[dict],
    *,
    witness: list[dict] | None,
    machine: list[dict] | None,
    include_dismissed: bool = False,
) -> list[dict]:
    """Tramos oídos por el testigo y/o la máquina que no están en la letra.

    Cada alerta trae ``sources``: ``both`` (testigo y máquina coinciden),
    ``witness`` (sólo el testigo, y es letra que la canción canta en otra
    parte) o ``machine`` (sólo la máquina con score alto, cuando el job no
    tiene testigo).
    """
    final = _final_tokens(segments)
    witness = [dict(w, _i=i) for i, w in enumerate(witness or [])]
    machine = [dict(w, _i=i) for i, w in enumerate(machine or [])]
    missing_w = _deleted_words(witness, final)
    missing_m = [w for w in _deleted_words(machine, final)
                 if w.get("score", 0.0) >= MACHINE_MIN_SCORE]
    known = _known_lyric_ngrams(final)

    def corroborated(w: dict) -> bool:
        tok = norm_token(w["word"])
        return any(
            abs(m["start"] - w["start"]) <= CORROBORATION_S
            and _same(tok, norm_token(m["word"]))
            for m in missing_m
        )

    def known_elsewhere(run: list[dict]) -> bool:
        toks = [norm_token(w["word"]) for w in run]
        grams = [tuple(toks[k:k + 3]) for k in range(len(toks) - 2)]
        return bool(grams) and sum(g in known for g in grams) * 2 >= len(grams)

    def in_gap(run: list[dict]) -> bool:
        spans = [(_f(s.get("start")), _f(s.get("end")))
                 for s in segments if isinstance(s, dict)]
        outside = sum(
            not any(a <= (w["start"] + w["end"]) / 2 <= b for a, b in spans)
            for w in run
        )
        return outside * 10 >= len(run) * 7

    candidates = []
    for run in _runs(missing_w):
        if sum(corroborated(w) for w in run) * 2 >= len(run):
            candidates.append((run, "both", witness))
        elif known_elsewhere(run) and in_gap(run) and not _is_loop(run):
            # Sólo el testigo: se exige que sea letra que la canción canta en
            # otra parte (un coro o una repetición que falta en esta pasada) y
            # que caiga donde no hay cartel. Dentro de una línea ya revisada,
            # el que se equivoca suele ser el testigo.
            candidates.append((run, "witness", witness))
    covered = [(r[0]["start"], r[-1]["end"]) for r, _, _ in candidates]
    for run in _runs(missing_m):
        a, b = run[0]["start"], run[-1]["end"]
        if any(a <= cb + CORROBORATION_S and ca - CORROBORATION_S <= b for ca, cb in covered):
            continue
        # Si hay testigo y no oyó esto, la máquina probablemente alucinó y el
        # revisor hizo bien en borrarlo. Sin testigo, una palabra suelta es
        # casi siempre una corrección de ortografía o de un artículo.
        if witness or len(run) < 2:
            continue
        if sum(w.get("score", 0.0) for w in run) / len(run) < 0.6:
            continue
        candidates.append((run, "machine", machine))

    def display_text(run: list[dict], sources: str) -> str:
        # La máquina escribe con la ortografía de la letra ya alineada; el
        # testigo inventa tildes ("dormíte"). Si la máquina oyó cada palabra,
        # se propone su versión.
        if sources == "both":
            spelled = []
            for w in run:
                tok = norm_token(w["word"])
                match = next((m for m in missing_m
                              if abs(m["start"] - w["start"]) <= CORROBORATION_S
                              and _same(tok, norm_token(m["word"]))), None)
                if match is None:
                    break
                spelled.append(match["word"])
            else:
                return " ".join(spelled).strip()
        return " ".join(w["word"] for w in run).strip()

    dismissed = dismissed_keys(segments)
    alerts = []
    for run, sources, stream in sorted(candidates, key=lambda c: c[0][0]["start"]):
        heard_text = " ".join(w["word"] for w in run).strip()
        low = heard_text.lower()
        if any(h in low for h in _HALLUCINATIONS):
            continue
        start, end = round(run[0]["start"], 2), round(run[-1]["end"], 2)
        key = alert_key(heard_text, start)
        if key in dismissed and not include_dismissed:
            continue
        placed_run, placed_stream = run, stream
        if sources == "both":
            # La máquina sabe a qué verso pertenecía cada palabra (el testigo
            # sólo sabe cuándo sonó): "Que" abría "Que hace un año...", no
            # cerraba la línea anterior.
            matched = [m for m in missing_m if any(
                abs(m["start"] - w["start"]) <= CORROBORATION_S
                and _same(norm_token(w["word"]), norm_token(m["word"]))
                for w in run)]
            if matched:
                placed_run, placed_stream = matched, machine
        suggestion = _suggest_fix(segments, placed_run, placed_stream,
                                  gap=in_gap(run))
        alerts.append({
            "id": hashlib.sha1(key.encode("utf-8")).hexdigest()[:12],
            "key": key,
            "text": display_text(run, sources),
            "heard_text": heard_text,
            "start": start,
            "end": end,
            "sources": sources,
            **suggestion,
            "dismissed": key in dismissed,
        })
        if len(alerts) >= MAX_ALERTS:
            break
    return alerts


def _suggest_fix(segments: list[dict], run: list[dict], stream: list[dict], *,
                 gap: bool) -> dict:
    """Dónde va lo que falta, para que "Agregar" sea un solo click.

    Si el tramo cae donde no hay cartel y es una frase, se propone una línea
    nueva en ese hueco. Si no, se inserta en la línea que contiene la palabra
    oída justo antes (o justo después) del tramo: así "Que" va al principio de
    "Hace un año atrás" y no al final de la línea anterior, aunque el testigo
    lo ubique unas décimas antes.
    """
    start, end = run[0]["start"], run[-1]["end"]
    first, last = run[0]["_i"], run[-1]["_i"]
    before = stream[first - 1] if first > 0 else None
    after = stream[last + 1] if last + 1 < len(stream) else None
    if before is not None and (start - before["end"] > 2.0
                               or before.get("_seg", 0) != run[0].get("_seg", 0)):
        before = None
    if after is not None and (after["start"] - end > 2.0
                              or after.get("_seg", 0) != run[-1].get("_seg", 0)):
        after = None
    anchor_before = norm_token(before["word"]) if before else ""
    anchor_after = norm_token(after["word"]) if after else ""

    rows = [(i, s) for i, s in enumerate(segments) if isinstance(s, dict)]
    if gap and len(run) >= 3:
        prev_end = max((_f(s.get("end")) for _, s in rows if _f(s.get("end")) <= start + 0.3),
                       default=0.0)
        next_start = min((_f(s.get("start")) for _, s in rows if _f(s.get("start")) >= end - 0.3),
                         default=end + 1.0)
        new_start = max(start, prev_end + 0.05)
        new_end = min(end + 0.3, next_start - 0.05)
        if new_end - new_start >= 0.5:
            idx = _nearest_line(segments, start, end)
            return {
                "action": "new_line",
                "line_index": idx,
                "line_segment_id": _segment_id(segments, idx),
                "new_line": {"start": round(new_start, 3), "end": round(new_end, 3)},
                "anchor_before": anchor_before, "anchor_after": anchor_after,
                "placement": "gap",
            }

    nearby = [(i, s) for i, s in rows
              if _f(s.get("start")) - 2.0 <= end and start <= _f(s.get("end")) + 2.0]
    for i, s in nearby:
        toks = [norm_token(t) for t in str(s.get("text") or "").split()]
        if anchor_before and _f(s.get("start")) <= start + 0.5:
            hits = [k for k, t in enumerate(toks) if t and _same(anchor_before, t)]
            if hits:
                return _insert(segments, i, hits[-1] + 1, anchor_before, anchor_after)
    for i, s in nearby:
        toks = [norm_token(t) for t in str(s.get("text") or "").split()]
        if anchor_after and _f(s.get("end")) >= end - 0.5:
            hits = [k for k, t in enumerate(toks) if t and _same(anchor_after, t)]
            if hits:
                return _insert(segments, i, hits[0], anchor_before, anchor_after)
    idx = _nearest_line(segments, start, end)
    seg = segments[idx] if idx is not None else None
    placement = _placement(seg, start, end)
    words = len(str((seg or {}).get("text") or "").split())
    return _insert(segments, idx, 0 if placement == "before" else words,
                   anchor_before, anchor_after)


def _segment_id(segments: list[dict], idx: int | None) -> str | None:
    if idx is None:
        return None
    value = segments[idx].get("segment_id") if isinstance(segments[idx], dict) else None
    return str(value) if value else None


def _insert(segments, idx, word_index, anchor_before, anchor_after) -> dict:
    seg = segments[idx] if idx is not None else None
    words = len(str((seg or {}).get("text") or "").split())
    return {
        "action": "insert",
        "line_index": idx,
        "line_segment_id": _segment_id(segments, idx),
        "insert_at_word": word_index,
        "anchor_before": anchor_before,
        "anchor_after": anchor_after,
        "placement": ("before" if word_index == 0 and words
                      else "after" if word_index >= words else "inside"),
    }
