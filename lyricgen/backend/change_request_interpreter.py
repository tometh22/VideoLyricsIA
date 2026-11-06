"""Interpreta un pedido de cambio de UMG escrito en prosa libre y lo convierte
en cambios concretos sobre la letra, para que el operador los apruebe.

El parser estricto (change_request_parser) sólo entiende frases con comillas
("cambiar "X" por "Y""); en los pedidos #112-#131 reconoció 4 de 77
instrucciones. Acá un modelo lee el comentario junto con las líneas y sus
tiempos y propone reescrituras; después un validador determinístico decide
qué se puede ofrecer:

* toda palabra que entra o sale tiene que estar en el comentario del cliente
  (el modelo no puede inventar letra);
* el cambio tiene que caer cerca de un tiempo citado, o sobre un texto citado;
* el timing nunca lo inventa el modelo: sólo dice qué borde mover y hacia
  dónde, y el tiempo sale de las palabras oídas en el audio.

Nada se aplica solo: el resultado son propuestas para el operador.
"""
from __future__ import annotations

import json
import re
import unicodedata
from typing import Any, Callable

SCHEMA = "change-request-interpreter-v1"
MODEL = "gemini-2.5-pro"
TIME_WINDOW_S = 12.0
MAX_LINES = 400

_TIME_RE = re.compile(r"(?<![\d:])(\d{1,2}):([0-5]\d)(?![\d:])")
_WORD_RE = re.compile(r"[^\W_]+(?:['’][^\W_]+)*", re.UNICODE)


def fold(text: str) -> str:
    text = unicodedata.normalize("NFKD", str(text or "").casefold())
    return "".join(ch for ch in text if not unicodedata.combining(ch))


def words(text: str) -> list[str]:
    return _WORD_RE.findall(unicodedata.normalize("NFC", str(text or "")))


def cited_times(text: str) -> list[float]:
    return [int(m) * 60 + int(s) for m, s in _TIME_RE.findall(text or "")]


def _clock(seconds: Any) -> str:
    try:
        value = max(0.0, float(seconds))
    except (TypeError, ValueError):
        return "?:??"
    return f"{int(value // 60)}:{value % 60:04.1f}"


# --------------------------------------------------------------------------
# Prompt
# --------------------------------------------------------------------------

SYSTEM = """Sos el asistente de un operador que corrige letras sincronizadas de videos musicales.
El cliente (Universal Music) mandó un pedido de cambio en texto libre. Tu trabajo es traducirlo a
cambios concretos sobre las líneas numeradas de la letra. No corregís nada por tu cuenta: sólo lo que
el cliente pide.

Reglas:
- Cada cambio cita el fragmento exacto del comentario del que sale ("quote").
- Los tiempos del cliente (m:ss) son aproximados: buscá la línea que se canta en ese momento o muy cerca.
- Para cambiar texto, cortes de línea, unir o separar líneas, usá "rewrite": el rango de líneas
  [from, to] (inclusive) y cómo deben quedar ("new_lines"). Copiá igual todo lo que no cambia,
  respetando mayúsculas, tildes y signos existentes salvo que el cliente pida cambiarlos.
  Para unir dos líneas, un solo elemento en new_lines; para separar, varios.
- Si el cliente dice que algo se repite ("en todos los coros", "aparece de nuevo en 3:07",
  "en las tres veces"), hacé un rewrite por cada línea afectada. Si dice que otra aparición NO se toca,
  no la toques.
- "Falta la frase X": agregala en la línea donde se canta, con las palabras exactas del cliente.
- Tildes, signos ¿? ¡! y comas: cambialos sólo donde el cliente lo pide.
- El cliente escribe en MAYÚSCULAS para señalar la palabra ("falta el QUE", "TAMBIÉN te perderé"):
  en la letra va con la capitalización normal de la línea (mayúscula sólo al inicio o en nombres propios).
- Si una línea necesita varios cambios pedidos en distintas partes del comentario, hacé UN solo
  rewrite con todos juntos.
- Timing ("alargar", "que aparezca antes", "termina antes de tiempo", "se va antes"): usá "timing"
  con la línea, el borde ("start" o "end") y la dirección ("earlier" o "later"). No inventes segundos.
- Si el pedido es sobre el fondo, la imagen, la portada o algo que no es la letra, usá "other" con
  category "visual". Si no se entiende o no podés ubicarlo, usá "other" con category "unclear".
- "why": una frase corta en español rioplatense para el operador, explicando el cambio.

Respondé SOLO con JSON:
{"changes": [
  {"type": "rewrite", "quote": "...", "from": 12, "to": 12, "new_lines": ["..."], "why": "..."},
  {"type": "timing", "quote": "...", "line": 20, "edge": "end", "direction": "later", "why": "..."},
  {"type": "other", "quote": "...", "category": "visual" | "unclear", "why": "..."}
]}"""


def build_prompt(comment: str, segments: list[dict], title: str = "", artist: str = "") -> str:
    lines = []
    for k, seg in enumerate(segments[:MAX_LINES], start=1):
        lines.append(f"L{k} [{_clock(seg.get('start'))}–{_clock(seg.get('end'))}] {seg.get('text', '')}")
    head = f"Canción: {title} — {artist}\n" if title or artist else ""
    return (f"{head}Pedido del cliente:\n<<<\n{comment.strip()}\n>>>\n\n"
            f"Letra actual ({len(lines)} líneas):\n" + "\n".join(lines))


def parse_response(text: str) -> list[dict]:
    text = (text or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?|```$", "", text, flags=re.M).strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return _salvage(text)
    changes = data.get("changes") if isinstance(data, dict) else data
    return [c for c in changes or [] if isinstance(c, dict)]


def _salvage(text: str) -> list[dict]:
    """Respuesta cortada: se rescatan los cambios completos del principio."""
    start = text.find("[")
    if start < 0:
        return []
    decoder, out, at = json.JSONDecoder(), [], start + 1
    while True:
        while at < len(text) and text[at] in " \n\r\t,":
            at += 1
        if at >= len(text) or text[at] != "{":
            return out
        try:
            item, at = decoder.raw_decode(text, at)
        except json.JSONDecodeError:
            return out
        if isinstance(item, dict):
            out.append(item)


def ask_model(prompt: str, *, client=None, model: str = MODEL) -> str:
    """Una sola llamada; el cliente se inyecta para tests y medición."""
    from google import genai

    if client is None:
        from pipeline import _get_genai_client
        client = _get_genai_client()
    response = client.models.generate_content(
        model=model,
        contents=prompt,
        config=genai.types.GenerateContentConfig(
            system_instruction=SYSTEM,
            temperature=0.0,
            response_mime_type="application/json",
            max_output_tokens=32768,
        ),
    )
    return response.text or ""


# --------------------------------------------------------------------------
# Validación
# --------------------------------------------------------------------------

def _line_times(segments: list[dict], index: int) -> tuple[float, float]:
    seg = segments[index]
    return float(seg.get("start") or 0.0), float(seg.get("end") or 0.0)


def _near_citation(segments: list[dict], i: int, j: int, times: list[float]) -> bool:
    if not times:
        return False
    start = _line_times(segments, i)[0]
    end = _line_times(segments, j)[1]
    return any(start - TIME_WINDOW_S <= t <= end + TIME_WINDOW_S for t in times)


def _word_diff(old: list[str], new: list[str]) -> tuple[list[str], list[str]]:
    """(palabras que entran, palabras que se borran sin reemplazo). Lo que se
    reemplaza por palabras del cliente es la corrección misma."""
    import difflib

    a, b = [fold(w) for w in old], [fold(w) for w in new]
    added, deleted = [], []
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(a=a, b=b, autojunk=False).get_opcodes():
        if tag == "delete":
            deleted += a[i1:i2]
        if tag in {"replace", "insert"}:
            added += b[j1:j2]
    return added, deleted


_TOKEN_RE = re.compile(r"[^\W\d_]+(?:['’´][^\W\d_]+)*", re.UNICODE)


def normal_case(new_line: str, old_lines: list[str]) -> str:
    """El cliente marca en MAYÚSCULAS lo que pide ("falta el QUE", "con TRES
    pasos"); en la letra va con la capitalización de la línea. Las palabras
    que ya estaban en mayúsculas en la pantalla (siglas) se respetan."""
    kept = {w for line in old_lines for w in _TOKEN_RE.findall(line or "") if w.isupper()}
    if all(w.isupper() for line in old_lines for w in _TOKEN_RE.findall(line or "") if len(w) > 1):
        return new_line  # la pantalla ya está toda en mayúsculas
    first = True

    def fix(match: re.Match) -> str:
        nonlocal first
        word = match.group(0)
        at_start = first
        first = False
        if not word.isupper() or word in kept or word == "I" or (len(word) == 1 and at_start):
            return word
        lower = word.lower()
        return lower[:1].upper() + lower[1:] if at_start else lower

    return _TOKEN_RE.sub(fix, new_line)


def validate(changes: list[dict], segments: list[dict], comment: str) -> tuple[list[dict], list[dict]]:
    """(propuestas, manuales). Cada manual lleva el motivo."""
    comment_words = {fold(w) for w in words(comment)}
    comment_folded = " ".join(fold(w) for w in words(comment))
    all_times = cited_times(comment)
    proposals, manual = [], []
    used: set[int] = set()
    n = len(segments)
    for change in changes:
        kind = change.get("type")
        quote = str(change.get("quote") or "").strip()
        why = str(change.get("why") or "").strip()
        base = {"quote": quote, "why": why}
        if kind == "other":
            manual.append({**base, "reason": change.get("category") or "unclear"})
            continue
        times = cited_times(quote) or all_times
        if kind == "rewrite":
            try:
                i, j = int(change.get("from")) - 1, int(change.get("to")) - 1
            except (TypeError, ValueError):
                manual.append({**base, "reason": "unclear"})
                continue
            new_lines = [str(t).strip() for t in change.get("new_lines") or [] if str(t).strip()]
            old_for_case = [segments[k].get("text", "") for k in range(i, j + 1)] if 0 <= i <= j < n else []
            new_lines = [normal_case(t, old_for_case) for t in new_lines]
            if not (0 <= i <= j < n) or not new_lines or j - i > 6:
                manual.append({**base, "reason": "unclear"})
                continue
            old_words = [w for k in range(i, j + 1) for w in words(segments[k].get("text", ""))]
            new_words = [w for t in new_lines for w in words(t)]
            old_text = [segments[k].get("text", "") for k in range(i, j + 1)]
            if old_text == new_lines:
                continue  # no cambia nada
            added, removed = _word_diff(old_words, new_words)
            invented = [w for w in added if w not in comment_words]
            dropped = [w for w in removed if w not in comment_words]
            if invented:
                manual.append({**base, "reason": "invented_words", "detail": invented, "from": i, "to": j})
                continue
            if dropped:
                manual.append({**base, "reason": "drops_sung_words", "detail": dropped, "from": i, "to": j})
                continue
            anchored = _near_citation(segments, i, j, times)
            if not anchored and not cited_times(quote):
                # Sin tiempo en el fragmento: vale si el texto está citado.
                line_folded = " ".join(fold(w) for w in old_words)
                anchored = bool(line_folded) and (line_folded in comment_folded or any(
                    " ".join(fold(w) for w in words(t)) in comment_folded for t in new_lines if len(words(t)) >= 2))
            if not anchored:
                manual.append({**base, "reason": "not_located", "from": i, "to": j})
                continue
            if any(k in used for k in range(i, j + 1)):
                manual.append({**base, "reason": "overlaps_other_change", "from": i, "to": j})
                continue
            used.update(range(i, j + 1))
            proposals.append({**base, "type": "rewrite", "from": i, "to": j,
                              "before": old_text, "after": new_lines})
        elif kind == "timing":
            try:
                i = int(change.get("line")) - 1
            except (TypeError, ValueError):
                manual.append({**base, "reason": "unclear"})
                continue
            edge, direction = change.get("edge"), change.get("direction")
            if not (0 <= i < n) or edge not in {"start", "end"} or direction not in {"earlier", "later"}:
                manual.append({**base, "reason": "unclear"})
                continue
            if not _near_citation(segments, i, i, times):
                manual.append({**base, "reason": "not_located", "from": i, "to": i})
                continue
            proposals.append({**base, "type": "timing", "line": i, "edge": edge, "direction": direction})
        else:
            manual.append({**base, "reason": "unclear"})
    return proposals, manual


# --------------------------------------------------------------------------
# Aplicación (para la vista previa y para medir)
# --------------------------------------------------------------------------

def _word_times(segments: list[dict], i: int, j: int) -> list[tuple[str, float, float]]:
    """Palabras del rango con tiempo: las de la línea si coinciden, si no
    repartidas por largo dentro de la línea."""
    out = []
    for k in range(i, j + 1):
        seg = segments[k]
        toks = words(seg.get("text", ""))
        start, end = _line_times(segments, k)
        timed = [w for w in seg.get("words") or [] if isinstance(w, dict) and "start" in w and "end" in w]
        if len(timed) == len(toks) and toks:
            out += [(t, float(w["start"]), float(w["end"])) for t, w in zip(toks, timed)]
            continue
        total = sum(len(t) for t in toks) or 1
        at = start
        for t in toks:
            span = (end - start) * len(t) / total
            out.append((t, at, at + span))
            at += span
    return out


def _bounds(segments: list[dict], i: int, j: int, extra_lines: int = 0) -> tuple[float, float]:
    """Hasta dónde puede crecer un rango sin pisar las líneas vecinas."""
    lo = float(segments[i - 1].get("end") or 0) + 0.05 if i > 0 else 0.0
    hi = (float(segments[j + 1].get("start") or 0) - 0.05 if j + 1 < len(segments)
          else _line_times(segments, j)[1] + 5.0 + 3.0 * max(0, extra_lines))
    return lo, hi


def _heard_span(line: str, heard: list[dict], lo: float, hi: float) -> tuple[float, float] | None:
    toks = [fold(w) for w in words(line)]
    if not toks:
        return None
    hw = [(fold(str(w.get("word", ""))).strip(".,!?¡¿;:"), float(w.get("start", -1)), float(w.get("end", -1)))
          for w in heard if lo <= float(w.get("start", -1)) <= hi]
    for k in range(len(hw) - len(toks) + 1):
        if [t for t, _, _ in hw[k:k + len(toks)]] == toks:
            return hw[k][1], hw[k + len(toks) - 1][2]
    return None


def _relayout_times(segments: list[dict], i: int, j: int, new_lines: list[str],
                    heard: list[dict] | None = None) -> list[tuple[float, float]]:
    if j - i + 1 == len(new_lines):
        return [_line_times(segments, k) for k in range(i, j + 1)]
    import difflib

    old_start, old_end = _line_times(segments, i)[0], _line_times(segments, j)[1]
    lo, hi = _bounds(segments, i, j, len(new_lines) - (j - i + 1))
    old = _word_times(segments, i, j)
    new_toks = [(n, fold(w)) for n, t in enumerate(new_lines) for w in words(t)]
    matcher = difflib.SequenceMatcher(a=[fold(w) for w, _, _ in old], b=[w for _, w in new_toks], autojunk=False)
    spans: dict[int, tuple[float, float]] = {}
    for block in matcher.get_matching_blocks():
        for d in range(block.size):
            line = new_toks[block.b + d][0]
            _, s, e = old[block.a + d]
            a, b = spans.get(line, (s, e))
            spans[line] = (min(a, s), max(b, e))
    for n, text in enumerate(new_lines):
        if n not in spans and heard:
            found = _heard_span(text, heard, lo, hi)
            if found:
                spans[n] = found
    out: list[tuple[float, float]] = []
    for n, text in enumerate(new_lines):
        prev_end = out[-1][1] + 0.01 if out else lo
        if n in spans:
            s, e = spans[n]
        else:
            nxt = min((spans[m][0] for m in range(n + 1, len(new_lines)) if m in spans), default=hi)
            base = max(prev_end, old_end if not any(m in spans for m in range(n + 1, len(new_lines))) else prev_end)
            if base >= nxt - 0.2:
                base = prev_end
            s, e = base, min(nxt, base + 0.45 * max(1, len(words(text))) + 0.3)
        s = max(s, prev_end, lo)
        e = min(max(e, s + 0.2), hi)
        if e <= s:
            e = min(hi, s + 0.2)
        out.append((round(s, 2), round(e, 2)))
    return out


def heard_words(evidence: Any) -> list[dict]:
    """Palabras con tiempo del testigo (transcripción independiente)."""
    try:
        from heard_words import witness_words
        return witness_words(evidence) or []
    except Exception:
        return []


def _timing_value(segments: list[dict], proposal: dict, heard: list[dict]) -> float | None:
    i = proposal["line"]
    start, end = _line_times(segments, i)
    toks = [fold(w) for w in words(segments[i].get("text", ""))]
    if not toks:
        return None
    near = [w for w in heard if start - 4 <= float(w.get("start", -99)) <= end + 6]
    if proposal["edge"] == "end":
        last = [w for w in near if fold(w.get("word", "")).strip(".,!?¡¿") == toks[-1]]
        if proposal["direction"] == "later":
            target = max((float(w["end"]) for w in last), default=None)
            if target is None or target <= end:
                target = end + 1.0
            nxt = float(segments[i + 1]["start"]) - 0.05 if i + 1 < len(segments) else target
            return round(min(target + 0.3, nxt), 2)
        return round(max(start + 0.4, min((float(w["end"]) for w in last), default=end - 0.5) + 0.1), 2)
    first = [w for w in near if fold(w.get("word", "")).strip(".,!?¡¿") == toks[0]]
    if proposal["direction"] == "earlier":
        target = min((float(w["start"]) for w in first), default=start - 0.5)
        prev = float(segments[i - 1]["end"]) + 0.05 if i > 0 else 0.0
        return round(max(prev, min(target, start) - 0.1), 2)
    return round(min(end - 0.4, max((float(w["start"]) for w in first), default=start + 0.5)), 2)


def apply(segments: list[dict], proposals: list[dict], evidence: Any = None) -> list[dict]:
    out = [dict(s) for s in segments]
    heard = heard_words(evidence) if evidence is not None else []
    # De atrás hacia adelante para que los índices no se muevan.
    for p in sorted(proposals, key=lambda p: -(p.get("from", p.get("line", 0)))):
        if p["type"] == "rewrite":
            i, j = p["from"], p["to"]
            times = _relayout_times(out, i, j, p["after"], heard)
            template = out[i]
            rows = []
            for n, (text, (s, e)) in enumerate(zip(p["after"], times)):
                row = {k: v for k, v in (out[i + n] if i + n <= j else template).items() if k != "words"}
                row.update(text=text, start=round(s, 2), end=round(e, 2))
                if n and i + n > j:
                    row.pop("segment_id", None)
                rows.append(row)
            out[i:j + 1] = rows
        elif p["type"] == "timing":
            value = _timing_value(out, p, heard)
            if value is not None:
                out[p["line"]] = {**out[p["line"]], p["edge"]: value}
    return out


def interpret(comment: str, segments: list[dict], *, evidence: Any = None, title: str = "", artist: str = "",
              ask: Callable[[str], str] | None = None) -> dict:
    prompt = build_prompt(comment, segments, title=title, artist=artist)
    raw = (ask or ask_model)(prompt)
    changes = parse_response(raw)
    proposals, manual = validate(changes, segments, comment)
    return {"schema": SCHEMA, "model": MODEL, "proposals": proposals, "manual": manual,
            "raw_changes": changes}


# --------------------------------------------------------------------------
# Operaciones del flujo de pedidos (change_request_proposals)
# --------------------------------------------------------------------------

def _source_span(comment: str, quote: str) -> tuple[int | None, int | None]:
    if not quote:
        return None, None
    at = comment.find(quote)
    if at < 0:
        folded = fold(comment)
        at = folded.find(fold(quote))
    return (at, at + len(quote)) if at >= 0 else (None, None)


def to_operations(result: dict, segments: list[dict], comment: str, evidence: Any = None) -> tuple[list[dict], list[dict]]:
    """(operaciones aplicables, pendientes manuales) con la forma de
    change_request_proposals. Nada es automático: el operador aprueba."""
    from change_request_proposals import _operation_id
    from editor import segments_content_hash

    applicable, manual = [], []
    for p in result.get("proposals") or []:
        start_src, end_src = _source_span(comment, p.get("quote", ""))
        if p["type"] == "rewrite":
            i, j = p["from"], p["to"]
            before = [dict(s) for s in segments[i:j + 1]]
            after = apply(segments, [p], evidence)[i:i + len(p["after"])]
            if len(before) == len(after):
                pairs = [(b, a) for b, a in zip(before, after) if b.get("text") != a.get("text")]
                groups = [([b], [{**b, "text": a["text"]}], "replace_text") for b, a in pairs]
            else:
                kind = "merge_phrase" if len(after) == 1 else "relayout"
                if kind == "merge_phrase":
                    after = [{**after[0], "start": float(before[0].get("start") or 0),
                              "end": float(before[-1].get("end") or 0)}]
                groups = [(before, after, kind)]
        else:
            i = p["line"]
            before = [dict(segments[i])]
            moved = apply(segments, [p], evidence)[i]
            if (moved.get("start"), moved.get("end")) == (before[0].get("start"), before[0].get("end")):
                manual.append({**p, "reason": "no_timing_evidence"})
                continue
            groups = [(before, [moved], "timing")]
        for cur, prop, kind in groups:
            cur_rows = [{k: v for k, v in r.items()} for r in cur]
            prop_rows = [{k: v for k, v in r.items() if k not in {"words", "word_timestamps", "tokens"}} for r in prop]
            applicable.append({
                "id": _operation_id("interp", kind, *(r.get("start") for r in cur_rows),
                                    *(r.get("text") for r in prop_rows), prop_rows[0].get("start"), prop_rows[-1].get("end")),
                "group_key": f"interp-{start_src}",
                "kind": kind,
                "origin": "interpreter",
                "status": "pending",
                "applicable": True,
                "automatic_apply_allowed": False,
                "confidence": "medium",
                "scope": "single",
                "why": p.get("why", ""),
                "source_excerpt": p.get("quote", ""),
                "source_start": start_src,
                "source_end": end_src,
                "timecode_seconds": (cited_times(p.get("quote", "")) or [None])[0],
                "start": float(prop_rows[0].get("start") or 0),
                "end": float(prop_rows[-1].get("end") or 0),
                "current_segments": cur_rows,
                "proposed_segments": prop_rows,
                "current_segments_hash": segments_content_hash(cur_rows),
                "proposed_segments_hash": segments_content_hash(prop_rows),
                "warnings": ["structural_change_requires_confirmation"] if kind in {"merge_phrase", "relayout"} else [],
            })
    for m in result.get("manual") or []:
        start_src, end_src = _source_span(comment, m.get("quote", ""))
        manual.append({**m, "source_start": start_src, "source_end": end_src})
    return applicable, manual


def interpretation_for(comment: str, segments: list[dict], *, evidence: Any = None, title: str = "",
                       artist: str = "", ask: Callable[[str], str] | None = None) -> dict:
    """Lo que build_proposal(interpretation=...) espera, sobre los mismos
    segmentos normalizados que usa la propuesta (los hashes deben coincidir)."""
    from editor import normalize_segments

    current = normalize_segments([dict(row) for row in segments])
    result = interpret(comment, current, evidence=evidence, title=title, artist=artist, ask=ask)
    operations, manual = to_operations(result, current, comment, evidence)
    return {"schema": SCHEMA, "model": result["model"], "operations": operations, "manual": manual}
