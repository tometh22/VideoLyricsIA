"""Revisión rápida de la letra antes de aprobar.

POR QUÉ EXISTE
--------------
Los 20 pedidos de cambio de UMG del 29-09-2026 (136 ítems, Argentina y
Chile) mostraron que el error dominante no es una palabra borrada por el
revisor sino una palabra MAL OÍDA por la máquina que pasa la revisión sin
que nadie la toque (la mediana de las canciones entregó el 88 % de las
líneas idénticas a la máquina). La evidencia para evitarlo ya estaba
guardada y nadie la miraba:

- el testigo whisper-1 de canción entera (``machine_evidence``);
- Gemini sobre el audio completo SIN referencia (misma evidencia);
- la letra oficial de catálogo, cuando existe (lrclib la tenía en 17 de 20
  canciones y contenía la corrección del cliente en 74 de 97 ítems);
- las correcciones que ya se hicieron en otras canciones del mismo artista;
- las repeticiones de un coro que la persona corrigió sólo en parte.

Medido sobre esos pedidos: la unión de estos oídos y de cinco reglas
deterministas cubre ~110 de los 136 ítems. Lo que queda es criterio
editorial (cortes de línea, timing fino) y se documenta en
``docs/UMG_GUIA_ESTILO_LETRAS.md``.

QUÉ DEVUELVE
------------
Una lista de puntos a revisar, cada uno con un arreglo de un click:

- ``required`` bloquea la aprobación hasta que el revisor aplica o descarta.
  Sólo lo es lo que tiene dos fuentes que coinciden o una regla objetiva.
- lo que oyó una sola fuente es una sugerencia: se muestra, no bloquea.
- los arreglos idénticos se agrupan ("aplicar en las 8 repeticiones"), que
  es exactamente lo que UMG pide cuando dice "corregir en todos los coros".

La decisión "está bien así" se guarda en la línea (``qa_dismissed``), así
viaja con cada versión sin migración. Puro: sin I/O.
"""
from __future__ import annotations

import difflib
import hashlib
import os
import re
import unicodedata
from collections import Counter, defaultdict
from typing import Any, Iterable

from heard_words import (
    _f,
    _is_loop,
    _same,
    aligned_opcodes,
    find_missing_heard_words,
    machine_words,
    norm_token,
    opcode_similarity,
    witness_words,
)

SCHEMA = "lyric-review-v1"
MAX_BLOCK_TOKENS = 8
MAX_ITEMS = 60
# Más palabras que esto no se comparan contra los oídos (se avisa como canción
# difícil): protege al servidor de letras gigantes o pegadas a propósito.
MAX_REVIEW_TOKENS = 2500

# Palabras cortas cuya sola diferencia casi siempre es ruido de un oído.
_FUNCTION = {
    "a", "al", "de", "del", "el", "la", "las", "lo", "los", "en", "y", "e",
    "o", "u", "que", "se", "me", "te", "mi", "tu", "su", "un", "una", "es",
    "ya", "no", "si", "con", "por", "pa", "le", "les", "nos",
}
# Formas con y sin tilde que son OTRA palabra, no un error de escritura.
_DIACRITIC_PAIRS = {
    "tu", "si", "el", "mi", "te", "se", "de", "mas", "solo", "aun", "que",
    "como", "cuando", "donde", "quien", "cual", "cuanto", "este", "esta",
    "ese", "esa", "o", "esto", "aquel", "aquella", "estas", "estos", "esas",
    "hacia", "seria", "sabia", "rio", "continuo", "ultimo", "publico",
    "practico", "critico", "animo", "deposito", "liquido", "habito",
    "calculo", "medico", "termino", "limite", "secretaria", "envio", "confio",
    "varias", "rie", "rien", "sabana", "papa", "mama", "cortes", "ingles",
    "domino", "transito", "numero", "canto", "llego", "paso", "amo", "hablo",
}
_INTERROGATIVES = {
    "qué", "cómo", "cuándo", "dónde", "adónde", "quién", "quiénes", "cuál",
    "cuáles", "cuánto", "cuánta", "cuántos", "cuántas", "acaso",
}
_ACCENTED_INTERROGATIVE = {
    "cuando": "cuándo", "como": "cómo", "donde": "dónde", "adonde": "adónde",
    "que": "qué", "quien": "quién", "quienes": "quiénes", "cual": "cuál",
    "cuales": "cuáles", "cuanto": "cuánto", "cuanta": "cuánta",
    "cuantos": "cuántos", "cuantas": "cuántas",
}
# "¿Cuando vuelvas?" casi nunca es pregunta (UMG #117, #121); "¿Que hora es?"
# casi siempre sí, y le falta la tilde.
_RELATIVE_NOT_QUESTION = {"cuando", "como", "donde", "adonde"}
_EXCLAMATIVE = re.compile(
    r"^\s*¿?\s*(qué|cómo|cuán)\s+"
    r"(lind[oa]s?|bell[oa]s?|hermos[oa]s?|bonit[oa]s?|trist[ea]s?|"
    r"grande|feo|fea|lejos|ric[oa]|dulce)\b",
    re.I,
)
_ORPHANS = {"que", "y", "si", "a", "de", "la", "el", "en", "o", "no", "me",
            "te", "se", "lo", "un", "es", "mi", "tu", "por", "con"}
_WORD_RE = re.compile(r"[^\W_]+(?:'[^\W_]*)?", re.UNICODE)
_NON_LATIN = re.compile(r"[Ͱ-ϿЀ-ӿ֐-ۿ぀-ヿ一-鿿]")
_FILLERS = {"eh", "ehh", "ah", "ahh", "mm", "mmm", "hmm", "aja", "uh", "um", "em"}

SOURCE_LABELS = {
    "machine": "la máquina",
    "witness": "el testigo",
    "gemini": "Gemini",
    "official": "la letra oficial",
    "memory": "una corrección anterior de este artista",
}

# Tipo de punto → etiqueta corta y grupo visual en el editor.
KIND_GROUP = {
    "missing": "text", "heard_different": "text", "correction_memory": "text",
    "chorus_propagate": "chorus", "question_marks": "style",
    "title_spelling": "style", "accent_inconsistent": "style",
    "joined_words": "style", "orphan_word": "layout", "layout_official": "layout",
    "timing": "timing",
}


def mode() -> str:
    """``enforce`` (default): aprobar exige decidir los puntos obligatorios.
    ``observe``: se muestran sin bloquear. ``off``: no se calculan."""
    value = os.environ.get("LYRIC_REVIEW_MODE", "enforce").strip().lower()
    return value if value in {"enforce", "observe", "off"} else "enforce"


def fold(text: str) -> str:
    folded = unicodedata.normalize("NFKD", str(text or "").lower())
    return "".join(ch for ch in folded if not unicodedata.combining(ch))


def sound(word: str) -> str:
    """Clave fonética gruesa del español rioplatense/chileno: seseo, yeísmo,
    b/v, h muda, s aspirada y -ado → -ao."""
    w = fold(word)
    w = re.sub(r"ado\b", "ao", w)
    for a, b in (("ch", "x"), ("sh", "x"), ("qu", "k"), ("ll", "y"), ("v", "b"),
                 ("z", "s"), ("ce", "se"), ("ci", "si"), ("c", "k"), ("h", "")):
        w = w.replace(a, b)
    w = re.sub(r"s(?=[bcdfgjklmnpqrstvxz]|\b)", "", w)
    w = re.sub(r"y\b", "i", w)
    return re.sub(r"(.)\1+", r"\1", w)


def _words(text: str) -> list[str]:
    return _WORD_RE.findall(unicodedata.normalize("NFC", str(text or "")))


def _key_text(text: str) -> str:
    return " ".join(t for t in (norm_token(p) for p in str(text or "").split()) if t)


def dismissed_keys(segments: Iterable[dict]) -> set[str]:
    keys: set[str] = set()
    for seg in segments or []:
        if isinstance(seg, dict):
            for key in seg.get("qa_dismissed") or []:
                if isinstance(key, str):
                    keys.add(key)
    return keys


def _seg_id(segments: list[dict], index: int | None) -> str | None:
    if index is None or not (0 <= index < len(segments)):
        return None
    value = segments[index].get("segment_id") if isinstance(segments[index], dict) else None
    return str(value) if value else None


def _why(sources: Iterable[str]) -> str:
    names = [SOURCE_LABELS[s] for s in ("official", "gemini", "witness", "machine", "memory")
             if s in set(sources)]
    if not names:
        return ""
    if len(names) == 1:
        return f"Lo indica {names[0]}"
    return "Coinciden " + ", ".join(names[:-1]) + " y " + names[-1]


def _match_case(template: str, text: str) -> str:
    """Copia el formato de mayúsculas de la pantalla a la corrección."""
    letters = [c for c in template if c.isalpha()]
    if letters and all(c.isupper() for c in letters) and len(letters) > 1:
        return text.upper()
    if letters and letters[0].isupper():
        return text[:1].upper() + text[1:]
    return text


def _split_punct(raw: str) -> tuple[str, str, str]:
    m = re.match(r"^([^\w]*)(.*?)([^\w']*)$", raw, re.UNICODE)
    return (m.group(1), m.group(2), m.group(3)) if m else ("", raw, "")


def _replacement_for(screen_raw: list[str], heard: list[dict], *, at_line_start: bool) -> str:
    """Palabras oídas con la puntuación de la pantalla. Mayúscula sólo al
    empezar la línea o en nombres propios ("Kapelusz"), nunca copiada de la
    palabra que se reemplaza ("Vos sos Lo más")."""
    words = []
    for h in heard:
        body = _split_punct(h["raw"])[1]
        if not body:
            continue
        proper = body[:1].isupper() and not h.get("bol")
        words.append(body if proper else body.lower())
    text = " ".join(words)
    if screen_raw:
        first = _split_punct(screen_raw[0])[1]
        letters = [c for c in " ".join(screen_raw) if c.isalpha()]
        if letters and len(letters) > 1 and all(c.isupper() for c in letters):
            text = text.upper()
        elif at_line_start and first[:1].isupper():
            text = text[:1].upper() + text[1:]
        lead = _split_punct(screen_raw[0])[0]
        trail = _split_punct(screen_raw[-1])[2]
        return f"{lead}{text}{trail}"
    return text


def replace_words(text: str, find: str, replacement: str, *,
                  at_word: int | None = None, scope: str = "all") -> str | None:
    """Mismo algoritmo que el editor (lyricReview.js ``replaceWords``): por
    palabras, conservando los signos de la pantalla. ``scope="one"`` cambia
    sólo la aparición en ``at_word`` (o la primera): en "Te quiero, te
    quiero" corregir una no toca la otra."""
    tokens = unicodedata.normalize("NFC", str(text or "")).split()
    target = [t for t in (norm_token(w) for w in str(find or "").split()) if t]
    if not target:
        return None
    core = _split_punct(unicodedata.normalize("NFC", str(replacement or "")).strip())[1]
    words = [(i, norm_token(t)) for i, t in enumerate(tokens)]
    words = [(i, t) for i, t in words if t]
    starts = [k for k in range(len(words) - len(target) + 1)
              if [t for _, t in words[k:k + len(target)]] == target]
    if not starts:
        return None
    if scope == "one":
        chosen = [k for k in starts if words[k][0] == at_word] or starts[:1]
    else:
        chosen = starts
    out, skip_until = [], -1
    spans = {words[k][0]: words[k + len(target) - 1][0] for k in chosen}
    for i, tok in enumerate(tokens):
        if i <= skip_until:
            continue
        if i in spans:
            last = spans[i]
            lead = _split_punct(tokens[i])[0]
            trail = _split_punct(tokens[last])[2]
            out.append(f"{lead}{core}{trail}")
            skip_until = last
        else:
            out.append(tok)
    return " ".join(out)


def apply_punctuation(text: str, mode: str, clause: str | None = None) -> str:
    """Misma operación que hace el editor: sólo sobre la frase marcada."""
    text = unicodedata.normalize("NFC", str(text or ""))
    target = clause if clause and clause in text else text
    body = re.sub(r"\s{2,}", " ", target.replace("¿", "").replace("?", "")).strip()
    if mode == "exclaim":
        fixed = "¡" + body.lstrip("¡").rstrip("!") + "!"
    elif mode == "accent_question":
        first, _, rest = body.partition(" ")
        lead, core, trail = _split_punct(first)
        accented = _ACCENTED_INTERROGATIVE.get(fold(core), core)
        fixed = "¿" + lead + _match_case(core, accented) + trail + (" " + rest if rest else "") + "?"
    else:
        fixed = body
    return text.replace(target, fixed, 1) if target is not text else fixed


# --------------------------------------------------------------------------
# Oídos: dónde una fuente independiente escuchó otra cosa que la pantalla.
# --------------------------------------------------------------------------

def _screen_tokens(segments: list[dict]) -> list[dict]:
    out = []
    for i, seg in enumerate(segments):
        if not isinstance(seg, dict):
            continue
        raw_words = str(seg.get("text") or "").split()
        first_word = next((k for k, r in enumerate(raw_words) if norm_token(r)), None)
        for k, raw in enumerate(raw_words):
            tok = norm_token(raw)
            if tok:
                out.append({"tok": tok, "raw": raw, "line": i, "pos": k,
                            "line_start": k == first_word})
    return out


def clean_official(text: str) -> str:
    """Saca etiquetas LRC ("[00:12.3]", "Offset:-2915") y encabezados."""
    lines = []
    for line in str(text or "").splitlines():
        line = re.sub(r"\[[^\]]*\]", " ", line).strip()
        if not line or re.fullmatch(r"[A-Za-z]+\s*:\s*[-+\w.]*", line):
            continue
        lines.append(line)
    return "\n".join(lines)


def _ear_tokens(text: str) -> list[dict]:
    out = []
    for line_no, line in enumerate(str(text or "").splitlines() or [""]):
        first = True
        for raw in line.split():
            tok = norm_token(raw)
            if tok and tok not in _FILLERS and not _NON_LATIN.search(raw):
                out.append({"tok": tok, "raw": raw, "bol": first, "line": line_no})
                first = False
    return out


def _looks_corrupt(ear: list[dict], screen: list[dict]) -> bool:
    """lrclib a veces trae letras con las vocales acentuadas borradas
    ("slo", "ms"): esa fuente no sirve para comparar."""
    vocab = {s["tok"] for s in screen}
    stripped = defaultdict(set)
    for w in vocab:
        for k, ch in enumerate(w):
            if ch in "aeiou":
                stripped[w[:k] + w[k + 1:]].add(w)
    bad = sum(1 for e in ear if e["tok"] not in vocab and e["tok"] in stripped)
    return bad >= 3 and bad * 50 >= len(ear)


def _ear_blocks(segments: list[dict], screen: list[dict], ear: list[dict],
                source: str, witness: list[dict] | None = None):
    """Diferencias pantalla↔oído alineadas en orden. Devuelve los bloques, qué
    tanto se parece el oído a la pantalla y qué palabras de la pantalla
    confirmó este oído."""
    if not screen or len(ear) < 8:
        return [], 0.0, set()
    ops = aligned_opcodes([s["tok"] for s in screen], [e["tok"] for e in ear])
    similarity = opcode_similarity(ops, len(screen), len(ear))
    confirmed: set[int] = set()
    out = []
    for tag, i1, i2, j1, j2 in ops:
        if tag == "equal":
            confirmed.update(range(i1, i2))
            continue
        scr, heard = screen[i1:i2], ear[j1:j2]
        if not heard or len(heard) > MAX_BLOCK_TOKENS or len(scr) > MAX_BLOCK_TOKENS:
            continue
        s_toks, h_toks = [s["tok"] for s in scr], [h["tok"] for h in heard]
        if "".join(s_toks) == "".join(h_toks):
            continue  # separado/pegado: lo cubre la regla de palabras pegadas
        if _is_loop([{"word": h["raw"]} for h in heard]):
            continue
        spelling_only = bool(scr) and _same("".join(s_toks), "".join(h_toks))
        if spelling_only:
            diff_heard = [t for t in h_toks if t not in s_toks]
            diff_screen = [t for t in s_toks if t not in h_toks]
        else:
            diff_heard = [t for t in h_toks if not any(_same(t, s) for s in s_toks)]
            diff_screen = [t for t in s_toks if not any(_same(t, h) for h in h_toks)]
        if not diff_heard:
            continue
        # Apócopes y h muda: sólo cuentan si la letra oficial también lo dice
        # ("urgo"/"hurgo" sí; "Ay"/"Hay" de dos oídos, no).
        style_only = bool(scr) and source != "official" and _only_style_difference(
            [s["raw"] for s in scr], [h["raw"] for h in heard])
        if scr:
            if len({s["line"] for s in scr}) > 1:
                continue  # una corrección nunca cruza carteles
            line = scr[0]["line"]
            raw_line = str(segments[line].get("text") or "").split()
            p0, p1 = scr[0]["pos"], scr[-1]["pos"]
            span = raw_line[p0:p1 + 1]
            fix = {
                "type": "replace", "find": " ".join(span),
                "replace": _replacement_for(span, heard, at_line_start=scr[0]["line_start"]),
                "at_word": p0, "scope": "one",
            }
        else:
            # Lo que la pantalla no tiene va en la línea a la que pertenece en
            # el oído: si el oído empieza ahí una línea, va a la siguiente; si
            # no, al final de la anterior.
            before = screen[i1 - 1] if i1 > 0 else None
            after = screen[i1] if i1 < len(screen) else None
            if before is None and after is None:
                continue
            to_next = after is not None and (
                before is None or (before["line"] != after["line"] and heard[0].get("bol")))
            if to_next:
                line = after["line"]
                fix_anchor = {"anchor_before": "", "anchor_after": after["tok"],
                              "insert_at_word": after["pos"]}
            else:
                line = before["line"]
                fix_anchor = {"anchor_before": before["tok"],
                              "anchor_after": after["tok"] if after and after["line"] == line else "",
                              "insert_at_word": before["pos"] + 1}
            fix = {"type": "insert",
                   "text": " ".join(_split_punct(h["raw"])[1] for h in heard).strip(),
                   **fix_anchor}
            if not fix["text"]:
                continue
        seg = segments[line]
        if witness is not None:
            # El testigo tiene tiempos: lo que dice tiene que haber sonado en
            # ese cartel, no en otra repetición del coro.
            a, b = _f(seg.get("start")) - 2.0, _f(seg.get("end")) + 2.0
            window = [norm_token(w["word"]) for w in witness if a <= w["start"] <= b]
            if not all(any(_same(t, w) for w in window) for t in diff_heard):
                continue
        out.append({
            "line": line, "fix": fix, "source": source, "scr_range": (i1, i2),
            "diff_heard": diff_heard, "diff_screen": diff_screen,
            "spelling_only": spelling_only or style_only,
        })
    return out, similarity, confirmed


def _only_style_difference(screen_raw: list[str], heard_raw: list[str]) -> bool:
    """Diferencias que no son errores: apócopes regionales que UMG quiere
    conservar ("na'", "pa'", "to'") y la h muda ("ay"/"hay", "a"/"ha")."""
    if len(screen_raw) != len(heard_raw):
        return False
    for s_raw, h_raw in zip(screen_raw, heard_raw):
        s_tok, h_tok = norm_token(s_raw), norm_token(h_raw)
        if s_tok == h_tok:
            continue
        if ("'" in s_raw or "´" in s_raw or "’" in s_raw) and h_tok.startswith(s_tok):
            continue
        if s_tok.replace("h", "") == h_tok.replace("h", ""):
            continue
        return False
    return True


def _fix_signature(fix: dict) -> tuple:
    if fix["type"] == "replace":
        return ("replace", _key_text(fix["find"]), _key_text(fix["replace"]))
    return ("insert", _key_text(fix.get("text")), fix.get("anchor_before"), fix.get("anchor_after"))


_SOURCE_RANK = {"official": 3, "gemini": 2, "witness": 1}


def _sounds_closer(candidate: str, target: str, screen: str) -> bool:
    c, t, s = sound(candidate), sound(target), sound(screen)
    to_target = difflib.SequenceMatcher(a=c, b=t).ratio()
    to_screen = difflib.SequenceMatcher(a=c, b=s).ratio()
    return to_target >= 0.75 and to_target - to_screen >= 0.15


def _merge_ear_blocks(blocks: list[dict]) -> list[dict]:
    """Une lo que varios oídos proponen para la misma línea. Si coinciden en
    el mismo cambio, el punto es obligatorio; si proponen cosas distintas
    para el mismo tramo, es UN punto con opciones, no varios."""
    grouped: dict[tuple, dict] = {}
    for b in blocks:
        sig = (b["line"], _fix_signature(b["fix"]))
        match = None
        for key, g in grouped.items():
            if key[0] != b["line"] or key[1][0] != sig[1][0]:
                continue
            idx = -1 if sig[1][0] == "replace" else 1
            same_find = sig[1][0] == "insert" or key[1][1] == sig[1][1]
            if same_find and "".join(key[1][idx].split()) == "".join(sig[1][idx].split()):
                match = g
                break
        if match is None:
            grouped[sig] = {**b, "sources": {b["source"]}}
        else:
            match["sources"].add(b["source"])
            match["spelling_only"] = match["spelling_only"] and b["spelling_only"]
            if b["source"] == "official":
                match["fix"] = b["fix"]  # la letra oficial escribe mejor
    merged = list(grouped.values())
    by_span: dict[tuple, list[dict]] = defaultdict(list)
    out = []
    for m in merged:
        if m["fix"]["type"] == "replace":
            by_span[(m["line"], _key_text(m["fix"]["find"]))].append(m)
        else:
            out.append(m)
    for options in by_span.values():
        official = next((o for o in options if "official" in o["sources"]), None)
        if official is not None:
            # Un oído que SUENA como la letra oficial la corrobora ("la mar" ≈
            # "lo más" no, "largor" ≈ "algura" ≈ "largura" sí).
            for o in options:
                if o is official:
                    continue
                if _sounds_closer(o["fix"]["replace"], official["fix"]["replace"], o["fix"]["find"]):
                    official["sources"] |= o["sources"]
                    o["absorbed"] = True
            options = [o for o in options if not o.get("absorbed")]
        options.sort(key=lambda o: (len(o["sources"]), max(_SOURCE_RANK.get(x, 0) for x in o["sources"])),
                     reverse=True)
        best = options[0]
        best["alternatives"] = [
            {"label": o["fix"]["replace"], "replace": o["fix"]["replace"],
             "why": _why(o["sources"]), "sources": sorted(o["sources"])}
            for o in options[1:]
        ]
        out.append(best)
    return out


# --------------------------------------------------------------------------
# Reglas deterministas (medidas sobre los pedidos #112-#131).
# --------------------------------------------------------------------------

def _rule_question_marks(segments: list[dict]) -> list[dict]:
    """Sólo los casos que UMG devolvió; una pregunta de sí/no ("¿Me
    querés?") es legítima y no se toca. El arreglo toca sólo esa frase."""
    out = []
    for i, seg in enumerate(segments):
        text = str(seg.get("text") or "")
        if "?" not in text and "¿" not in text:
            continue
        for m in re.finditer(r"¿[^?¿]*\??|^[^¿?]*\?", text):
            clause = m.group(0)
            body = clause.replace("¿", "").replace("?", "").strip()
            words = _words(body)
            if not words:
                continue
            first = words[0]
            alternatives = []
            required = True
            if _EXCLAMATIVE.search(body):
                why, mode_ = "Es una exclamación: va con ¡ !", "exclaim"
            elif {w.lower() for w in words} & _INTERROGATIVES:
                continue  # ya pregunta con tilde ("¿que dónde llega…?")
            elif fold(first) in _ACCENTED_INTERROGATIVE and first.lower() not in _INTERROGATIVES:
                accent = _ACCENTED_INTERROGATIVE[fold(first)]
                if fold(first) in _RELATIVE_NOT_QUESTION:
                    why = f"«{first}» sin tilde no pregunta: van sin signos"
                    mode_, alt_mode, alt_label = "remove_question", "accent_question", f"Es pregunta: «{accent}»"
                else:
                    # "¿Que hora es?" pide tilde; "¿Que el mundo gira al revés?"
                    # no: se sugiere, no se exige.
                    why = f"Si es pregunta, «{first}» lleva tilde"
                    mode_, alt_mode, alt_label = "accent_question", "remove_question", "No es pregunta"
                    required = False
                alternatives.append({
                    "label": alt_label, "why": "",
                    "fix": {"type": "punctuation", "mode": alt_mode, "clause": clause},
                })
            elif fold(" ".join(words[:3])).startswith("a ver si"):
                why, mode_ = "«A ver si…» no es una pregunta", "remove_question"
            else:
                continue
            out.append({
                "kind": "question_marks", "line": i, "required": required,
                "title": "Signos de pregunta", "why": why,
                "fix": {"type": "punctuation", "mode": mode_, "clause": clause},
                "alternatives": alternatives, "action": "Corregir",
            })
    return out


def _title_words(title: str) -> list[str]:
    title = unicodedata.normalize("NFC", str(title or ""))
    title = re.sub(r"\(.*?\)|\[.*?\]", " ", title)
    title = re.split(r"\s[-–—]\s|\sft\.?\s|\sfeat\.?\s", title, flags=re.I)[0]
    return _words(title)


def _rule_title(segments: list[dict], title: str) -> list[dict]:
    """Sólo los dos errores de título que UMG devolvió: a la letra le falta la
    "s" final que tiene el título ("vuelva" / "Vuelvas", "lavártelo" /
    "Lavartelos"), o junta lo que el título separa ("Porque" / "Por qué").
    Cualquier otra diferencia con el título puede ser la letra real."""
    words = _title_words(title)
    folded = [fold(w) for w in words]
    if len(words) < 2 or len("".join(folded)) < 8:
        return []
    out = []
    for i, seg in enumerate(segments):
        raw = str(seg.get("text") or "").split()
        core = [fold(_split_punct(w)[1]) for w in raw]
        n = len(words)
        hits = []
        for k in range(0, len(raw) - n + 1):
            span = core[k:k + n]
            diffs = [j for j in range(n) if span[j] != folded[j]]
            if len(diffs) == 1 and folded[diffs[0]] == span[diffs[0]] + "s":
                j = diffs[0]
                fixed = list(raw[k:k + n])
                lead, body, trail = _split_punct(fixed[j])
                fixed[j] = f"{lead}{body}s{trail}"
                hits.append((k, n, " ".join(fixed)))
        if not hits:
            for k in range(0, len(raw) - (n - 1) + 1):
                span = core[k:k + n - 1]
                if "".join(span) == "".join(folded) and span != folded:
                    hits.append((k, n - 1, _replacement_for(
                        raw[k:k + n - 1], [{"raw": w.lower()} for w in words],
                        at_line_start=k == 0)))
        for k, size, replacement in hits[:1]:
            out.append({
                "kind": "title_spelling", "line": i, "required": True,
                "title": "Como en el título",
                "why": f"El título dice «{' '.join(words)}»",
                "fix": {"type": "replace", "find": " ".join(raw[k:k + size]),
                        "replace": replacement, "scope": "all"},
                "action": "Corregir",
            })
    return out


def _rule_accents(segments: list[dict]) -> list[dict]:
    forms: dict[str, Counter] = defaultdict(Counter)
    for seg in segments:
        for w in _words(seg.get("text")):
            forms[fold(w)][w.lower()] += 1
    out = []
    for base, variants in forms.items():
        if len(variants) < 2 or len(base) < 3 or base in _DIACRITIC_PAIRS:
            continue
        accented = [v for v in variants if v != base]
        # bajo/bajó, quedara/quedará, esta/está: la tilde en la última letra
        # cambia la palabra, no es un error de escritura.
        accented = [v for v in accented if fold(v[-1]) == v[-1]]
        if not accented:
            continue
        best = max(accented, key=lambda v: variants[v])
        others = sum(c for v, c in variants.items() if v != best)
        # Obligatorio sólo si la forma con tilde domina la canción ("Mío" x5,
        # "Mio" x1); si está pareja, puede ser otra palabra: sugerencia.
        required = variants[best] >= 2 * others
        for i, seg in enumerate(segments):
            for w in _words(seg.get("text")):
                if fold(w) == base and w.lower() != best:
                    out.append({
                        "kind": "accent_inconsistent", "line": i, "required": required,
                        "title": "Tildes distintas",
                        "why": f"En la canción aparece «{best}» y también «{w.lower()}»",
                        "fix": {"type": "replace", "find": w, "replace": _match_case(w, best),
                                "scope": "all"},
                        "action": "Unificar",
                    })
    return out


_ENCLITICS = {"me", "te", "se", "la", "lo", "le", "nos", "los", "las", "les", "sela", "selo"}
_REAL_COMPOUNDS = {
    "porque", "porqué", "sino", "aunque", "también", "tampoco", "adonde", "quizás",
    "enseguida", "deprisa", "sinvergüenza", "sinfín", "sobretodo", "contracorriente",
    "damajuana", "quesillo", "quesillos", "bienvenido", "bienvenida", "mediodía",
}


def _rule_joined(segments: list[dict], ear_bigrams: set[tuple[str, str]],
                 ear_vocab: set[str]) -> list[dict]:
    """Dos palabras pegadas: sólo si un oído las escuchó SEPARADAS y seguidas
    ("logro entender", "si esto") y ninguno la escuchó junta. Así
    "Desnúdate", "comerme" o "porque" nunca se separan."""
    out = []
    for i, seg in enumerate(segments):
        for raw in str(seg.get("text") or "").split():
            # Coma sin espacio ("vuelvo,vuelvo"): siempre es un error de tipeo.
            if re.search(r"[^\W\d_][,;][^\W\d_]", raw):
                fixed = re.sub(r"([,;])(?=[^\W\d_])", r"\1 ", raw)
                out.append({
                    "kind": "joined_words", "line": i, "required": True,
                    "title": "Falta un espacio", "why": "Después de la coma va un espacio",
                    "fix": {"type": "replace", "find": raw, "replace": fixed, "scope": "all"},
                    "action": "Separar",
                })
                continue
            word = _split_punct(raw)[1]
            tok = norm_token(word)
            if len(tok) < 5 or fold(word) in _REAL_COMPOUNDS or tok in ear_vocab:
                continue
            for k in range(2, len(tok) - 1):
                left, right = tok[:k], tok[k:]
                if right in _ENCLITICS or (left, right) not in ear_bigrams:
                    continue
                cut = len(word) - len(right)
                fixed = word[:cut] + " " + word[cut:].lower()
                if fixed == word:
                    continue
                out.append({
                    "kind": "joined_words", "line": i, "required": True,
                    "title": "Palabras pegadas", "why": f"«{word}» son dos palabras",
                    "fix": {"type": "replace", "find": word, "replace": fixed, "scope": "all"},
                    "action": "Separar",
                })
                break
    return out


def _bigrams(*texts: str) -> set[tuple[str, str]]:
    out: set[tuple[str, str]] = set()
    for text in texts:
        toks = [t["tok"] for t in _ear_tokens(text)]
        out.update(zip(toks, toks[1:]))
    return out


def _rule_orphans(segments: list[dict]) -> list[dict]:
    out = []
    for i, seg in enumerate(segments):
        words = _words(seg.get("text"))
        if len(words) != 1 or fold(words[0]) not in _ORPHANS:
            continue
        nxt = segments[i + 1] if i + 1 < len(segments) else None
        prev = segments[i - 1] if i > 0 else None
        gap_next = _f(nxt.get("start")) - _f(seg.get("end")) if nxt else 99.0
        gap_prev = _f(seg.get("start")) - _f(prev.get("end")) if prev else 99.0
        direction = "next" if gap_next <= gap_prev else "previous"
        if min(gap_next, gap_prev) > 2.5:
            continue
        other = nxt if direction == "next" else prev
        out.append({
            "kind": "orphan_word", "line": i, "required": True,
            "title": "Palabra sola en pantalla",
            "why": f"«{words[0]}» queda sola; va con la línea {'siguiente' if direction == 'next' else 'anterior'}",
            "fix": {"type": "merge", "direction": direction,
                    "other_segment_id": other.get("segment_id"),
                    "expect": _key_text(seg.get("text"))},
            "preview_after": (
                f"{str(seg.get('text') or '').strip()} {str(other.get('text') or '').strip()}"
                if direction == "next" else
                f"{str(other.get('text') or '').strip()} {str(seg.get('text') or '').strip()}"
            ),
            "action": "Unir",
        })
    return out


def _rule_memory(segments: list[dict], pairs: dict[str, str],
                 witness: list[dict]) -> list[dict]:
    out = []
    if not pairs:
        return out
    for i, seg in enumerate(segments):
        text = str(seg.get("text") or "")
        a, b = _f(seg.get("start")) - 2.0, _f(seg.get("end")) + 2.0
        heard_here = {norm_token(w["word"]) for w in witness if a <= w["start"] <= b}
        for raw in text.split():
            word = _split_punct(raw)[1]
            right = pairs.get(fold(word))
            if not right or fold(right) in {fold(w) for w in _words(text)}:
                continue
            corroborated = norm_token(right) in heard_here
            out.append({
                "kind": "correction_memory", "line": i, "required": corroborated,
                "title": "Ya se corrigió en otra canción",
                "why": f"En este artista «{word}» se corrigió a «{right}»",
                "sources": {"memory"} | ({"witness"} if corroborated else set()),
                "fix": {"type": "replace", "find": word, "replace": _match_case(word, right),
                        "scope": "all"},
                "action": "Corregir",
            })
    return out


def _map_to_current(segments: list[dict], orig: dict) -> int | None:
    """Línea actual que corresponde a una línea de la máquina: por identidad
    si se conservó, si no por solapamiento en el tiempo."""
    sid = orig.get("segment_id")
    if sid:
        for i, seg in enumerate(segments):
            if seg.get("segment_id") == sid:
                return i
    a, b = _f(orig.get("start")), _f(orig.get("end"))
    best, best_overlap = None, 0.0
    for i, seg in enumerate(segments):
        overlap = min(b, _f(seg.get("end"))) - max(a, _f(seg.get("start")))
        if overlap > best_overlap:
            best, best_overlap = i, overlap
    if best is None or best_overlap < 0.5 * max(0.1, b - a):
        return None
    return best


def _rule_chorus(segments: list[dict], original_segments: Any) -> list[dict]:
    """Un coro corregido sólo en algunas repeticiones: lo que más veces pide
    UMG ("corregir en todos los coros"). Se detecta cuando la máquina había
    escrito varias líneas iguales y una persona corrigió sólo algunas."""
    originals = [s for s in (original_segments or []) if isinstance(s, dict)]
    groups: dict[str, list[dict]] = defaultdict(list)
    for orig in originals:
        key = _key_text(orig.get("text"))
        if len(key.split()) >= 3:
            groups[key].append(orig)
    out = []
    for key, members in groups.items():
        if len(members) < 2 or len(members) > 40:
            continue
        mapped = [_map_to_current(segments, m) for m in members]
        current = [(i, str(segments[i].get("text") or "")) for i in mapped if i is not None]
        if len({i for i, _ in current}) < 2:
            continue

        # Sólo correcciones del mismo coro, no líneas unidas o reordenadas:
        # misma cantidad de palabras (±2) y mayormente las mismas.
        def is_correction(text: str) -> bool:
            a, b = key.split(), _key_text(text).split()
            return abs(len(a) - len(b)) <= 2 and difflib.SequenceMatcher(a=a, b=b).ratio() >= 0.6
        edited = [(i, t) for i, t in current if _key_text(t) != key and is_correction(t)]
        untouched = [i for i, t in current if _key_text(t) == key]
        if not edited or not untouched:
            continue
        variants = Counter(_key_text(t) for _, t in edited)
        best_key = variants.most_common(1)[0][0]
        best_text = next(t for _, t in edited if _key_text(t) == best_key)
        for i in sorted(set(untouched)):
            out.append({
                # Sugerencia: a veces la corrección de una repetición trae un
                # error de tipeo, y propagarla lo multiplicaría.
                "kind": "chorus_propagate", "line": i, "required": False,
                "title": "Coro corregido en parte",
                "why": f"Esta frase se repite {len(current)} veces y la corregiste en {len(edited)}",
                "fix": {"type": "set_text", "text": best_text, "expect": key},
                "action": "Igualar",
                "dismiss": "Esta repetición es distinta",
            })
    return out


def _rule_layout_official(segments: list[dict], screen: list[dict],
                          official: list[dict], ops: list[tuple]) -> list[dict]:
    """Cortes de línea como en la letra oficial cuando la pantalla deja 1-3
    palabras del borde en la línea equivocada ("¿Cuando vuelvas quiero /
    Verte a solas" → "Cuando vuelvas / Quiero verte a solas", UMG #121)."""
    to_official: dict[int, int] = {}
    for tag, i1, i2, j1, _j2 in ops:
        if tag == "equal":
            for k in range(i2 - i1):
                to_official[i1 + k] = official[j1 + k]["line"]
    by_line: dict[int, list[int]] = defaultdict(list)
    for idx, s in enumerate(screen):
        by_line[s["line"]].append(idx)
    out = []
    lines = sorted(by_line)
    for a, b in zip(lines, lines[1:]):
        if b != a + 1:
            continue
        ta, tb = by_line[a], by_line[b]
        if any(i not in to_official for i in ta + tb):
            continue
        la, lb = [to_official[i] for i in ta], [to_official[i] for i in tb]
        moved_tail = [k for k in range(len(ta)) if la[k] == lb[0] and la[0] != lb[0]]
        moved_head = [k for k in range(len(tb)) if lb[k] == la[-1] and lb[-1] != la[-1]]
        raw_a = str(segments[a].get("text") or "").split()
        raw_b = str(segments[b].get("text") or "").split()
        def lower_first(word: str) -> str:
            return word[:1].lower() + word[1:] if word[:1].isupper() and word[1:] == word[1:].lower() else word
        if moved_tail and 1 <= len(moved_tail) <= 3 and moved_tail[-1] == len(ta) - 1:
            cut = screen[ta[moved_tail[0]]]["pos"]
            # La palabra que abría la línea siguiente deja de abrirla.
            new_a = raw_a[:cut]
            new_b = raw_a[cut:] + ([lower_first(raw_b[0])] + raw_b[1:] if raw_b else [])
        elif moved_head and 1 <= len(moved_head) <= 3 and moved_head[0] == 0:
            cut = screen[tb[moved_head[-1]]]["pos"] + 1
            moved = raw_b[:cut]
            new_a = raw_a + ([lower_first(moved[0])] + moved[1:] if moved else [])
            new_b = raw_b[cut:]
        else:
            continue
        if not new_a or not new_b:
            continue
        text_a, text_b = " ".join(new_a), " ".join(new_b)
        text_b = text_b[:1].upper() + text_b[1:]
        out.append({
            "kind": "layout_official", "line": a, "required": False,
            "title": "Corte de línea", "why": "Así corta la frase la letra oficial",
            "fix": {"type": "relayout", "lines": [
                {"segment_id": segments[a].get("segment_id"), "text": text_a,
                 "expect": _key_text(segments[a].get("text"))},
                {"segment_id": segments[b].get("segment_id"), "text": text_b,
                 "expect": _key_text(segments[b].get("text"))},
            ]},
            "preview_before": f"{segments[a].get('text')} / {segments[b].get('text')}",
            "preview_after": f"{text_a} / {text_b}",
            "action": "Cortar así",
        })
    return out


def _rule_timing(segments: list[dict], witness: list[dict], machine: list[dict]) -> list[dict]:
    """La línea se va antes de que termine la última palabra ("alargar
    brasero", UMG #115) o aparece mucho antes de que se cante. Sólo si la
    máquina y el testigo coinciden en el tiempo de esa palabra."""
    out = []
    for i, seg in enumerate(segments):
        toks = [t for t in (norm_token(w) for w in str(seg.get("text") or "").split()) if t]
        if len(toks) < 2:
            continue
        start, end = _f(seg.get("start")), _f(seg.get("end"))
        nxt = segments[i + 1] if i + 1 < len(segments) else None
        prv = segments[i - 1] if i > 0 else None

        def times(tok: str, lo: float, hi: float, stream: list[dict]) -> list[dict]:
            return [w for w in stream if lo <= w["start"] <= hi and norm_token(w["word"]) == tok]
        last_w = times(toks[-1], end - 1.5, end + 2.5, witness)
        last_m = times(toks[-1], end - 1.5, end + 2.5, machine)
        if last_w and last_m:
            we, me = max(w["end"] for w in last_w), max(w["end"] for w in last_m)
            limit = (_f(nxt.get("start")) - 0.05) if nxt else we + 1.0
            if abs(we - me) <= 0.4 and min(we, me) > end + 0.5 and limit > end + 0.3:
                new_end = round(min(max(we, me) + 0.1, limit), 2)
                out.append({
                    "kind": "timing", "line": i, "required": False,
                    "title": "Timing",
                    "why": f"Se va {min(we, me) - end:.1f} s antes de que termine «{toks[-1]}»",
                    "fix": {"type": "timing", "end": new_end},
                    "preview_after": f"Termina en {new_end:.1f} s", "action": "Alargar",
                })
                continue
        first_w = times(toks[0], start - 1.0, start + 2.5, witness)
        first_m = times(toks[0], start - 1.0, start + 2.5, machine)
        if first_w and first_m:
            ws, ms = min(w["start"] for w in first_w), min(w["start"] for w in first_m)
            floor = (_f(prv.get("end")) + 0.05) if prv else 0.0
            if abs(ws - ms) <= 0.4 and min(ws, ms) > start + 0.7:
                new_start = round(max(min(ws, ms) - 0.1, floor), 2)
                out.append({
                    "kind": "timing", "line": i, "required": False,
                    "title": "Timing",
                    "why": f"Aparece {min(ws, ms) - start:.1f} s antes de que se cante",
                    "fix": {"type": "timing", "start": new_start},
                    "preview_after": f"Aparece en {new_start:.1f} s", "action": "Ajustar",
                })
    return out


# --------------------------------------------------------------------------
# Armado final.
# --------------------------------------------------------------------------

def _insert_preview(segments: list[dict], line: int, fix: dict) -> str:
    before = str(segments[line].get("text") or "")
    tokens = before.split()
    at = max(0, min(len(tokens), int(fix.get("insert_at_word") or 0)))
    words = fix.get("text", "")
    if at == 0 and tokens:
        words = words[:1].upper() + words[1:]
        first = tokens[0]
        if first[:1].isupper() and first[1:] == first[1:].lower():
            tokens = [first[:1].lower() + first[1:]] + tokens[1:]
    return " ".join(tokens[:at] + [words] + tokens[at:])


def _preview(segments: list[dict], line: int, fix: dict) -> tuple[str, str]:
    before = str(segments[line].get("text") or "") if 0 <= line < len(segments) else ""
    if fix["type"] == "punctuation":
        return before, apply_punctuation(before, fix["mode"], fix.get("clause"))
    if fix["type"] == "replace":
        after = replace_words(before, fix["find"], fix["replace"],
                              at_word=fix.get("at_word"), scope=fix.get("scope", "all"))
        return before, after if after is not None else before
    if fix["type"] == "insert":
        return before, _insert_preview(segments, line, fix)
    if fix["type"] == "set_text":
        return before, fix["text"]
    return before, before


def _item_key(kind: str, fix: dict, line_text: str = "") -> str:
    """Identidad estable de un punto: no depende de tiempos ni de posiciones,
    así mover una línea no revive lo que ya se decidió."""
    if fix["type"] == "replace":
        return f"{kind}:{_key_text(fix['find'])}>{_key_text(fix['replace'])}"
    if fix["type"] == "punctuation":
        return f"{kind}:{fix['mode']}:{_key_text(line_text)}"
    if fix["type"] == "merge":
        return f"{kind}:{fix.get('direction')}:{_key_text(line_text)}"
    if fix["type"] == "set_text":
        return f"{kind}:{fix.get('expect')}>{_key_text(fix['text'])}"
    if fix["type"] == "relayout":
        return f"{kind}:" + "/".join(_key_text(x["text"]) for x in fix["lines"])
    if fix["type"] == "timing":
        return f"{kind}:{_key_text(line_text)}:{'end' if 'end' in fix else 'start'}"
    return (f"{kind}:+{_key_text(fix.get('text'))}|{fix.get('anchor_before', '')}"
            f"|{fix.get('anchor_after', '')}")


def _occurrence(segments: list[dict], line: int, fix: dict) -> dict:
    seg = segments[line]
    before, after = _preview(segments, line, fix)
    return {
        "line_index": line,
        "line_segment_id": _seg_id(segments, line),
        "start": round(_f(seg.get("start")), 2),
        "end": round(_f(seg.get("end")), 2),
        "before": before,
        "after": after,
        "fix": fix,
    }


def _missing_items(segments, witness, machine) -> list[dict]:
    items = []
    for alert in find_missing_heard_words(segments, witness=witness, machine=machine,
                                          include_dismissed=True):
        sources = {"both": {"machine", "witness"}, "witness": {"witness"},
                   "machine": {"machine"}}[alert["sources"]]
        if alert["action"] == "new_line":
            fix = {"type": "new_line", "text": alert["text"], **alert["new_line"]}
        else:
            fix = {"type": "insert", "text": alert["text"],
                   "anchor_before": alert["anchor_before"],
                   "anchor_after": alert["anchor_after"],
                   "insert_at_word": alert["insert_at_word"]}
        line = alert["line_index"] if alert["line_index"] is not None else 0
        occ = _occurrence(segments, line, fix)
        if fix["type"] == "new_line":
            occ.update({"start": fix["start"], "end": fix["end"], "line_segment_id": None,
                        "before": "", "after": alert["text"][:1].upper() + alert["text"][1:]})
        else:
            occ.update({"start": alert["start"], "end": alert["end"]})
        items.append({
            "kind": "missing", "required": True, "title": "Falta texto",
            "why": _why(sources), "sources": sources, "action": "Agregar",
            "dismiss": "No se canta", "key": alert["key"], "evidence_time": alert["start"],
            "occurrences": [occ], "text": alert["text"],
        })
    return items


def _empty(reason: str | None = None) -> dict:
    return {
        "schema": SCHEMA, "mode": mode(), "items": [], "required_count": 0,
        "suggested_count": 0, "sources": {}, "risk": {
            "level": "high" if reason else "normal", "reasons": [reason] if reason else []},
    }


def build_review(
    segments: list[dict],
    *,
    original_segments: Any = None,
    machine_evidence: Any = None,
    title: str = "",
    official_text: str | None = None,
    memory_pairs: dict[str, str] | None = None,
) -> dict:
    """Todos los puntos de la letra actual, listos para el editor."""
    segments = [
        {**s, "text": unicodedata.normalize("NFC", str(s.get("text") or ""))}
        for s in (segments or []) if isinstance(s, dict)
    ]
    if not segments:
        return _empty()
    screen = _screen_tokens(segments)
    if len(screen) > MAX_REVIEW_TOKENS:
        return _empty("La letra es demasiado larga para la revisión automática")
    witness = witness_words(machine_evidence)
    machine = machine_words(original_segments)
    gemini = gemini_text(machine_evidence)
    official_clean = clean_official(official_text or "")
    items: list[dict] = []

    items.extend(_missing_items(segments, witness, machine))

    blocks: list[dict] = []
    similarity: dict[str, float] = {}
    confirmed_by_official: set[int] = set()
    official_tokens = _ear_tokens(official_clean)
    official_ops: list[tuple] = []
    if official_tokens and _looks_corrupt(official_tokens, screen):
        official_tokens = []
    witness_text = " ".join(w["word"] for w in witness)
    for source, tokens, timed in (
        ("witness", _ear_tokens(witness_text), witness),
        ("gemini", _ear_tokens(gemini), None),
        ("official", official_tokens, None),
    ):
        if len(tokens) > MAX_REVIEW_TOKENS:
            continue
        found, sim, confirmed = _ear_blocks(segments, screen, tokens, source, timed)
        similarity[source] = round(sim, 3)
        # Un oído que casi no coincide con la pantalla es otra versión o una
        # alucinación: no aporta.
        if sim >= (0.45 if source != "witness" else 0.35):
            blocks.extend(found)
        if source == "official":
            confirmed_by_official = confirmed
            if tokens:
                official_ops = aligned_opcodes([s["tok"] for s in screen], [t["tok"] for t in tokens])
    same_version = similarity.get("official", 0.0) >= 0.85
    human_edited = _human_edited_lines(segments, original_segments)
    for block in _merge_ear_blocks(blocks):
        sources = block["sources"]
        only_official = sources == {"official"}
        i1, i2 = block["scr_range"]
        if same_version and "official" not in sources:
            # Veto de la letra oficial: si confirma lo que dice la pantalla,
            # dos oídos que suenan parecido ("ya"/"ella") no la corrigen.
            covered = range(i1, i2) if i2 > i1 else (i1 - 1, i1)
            if all(k in confirmed_by_official for k in covered):
                continue
        if only_official and block["fix"]["type"] == "insert" and (
            not same_version or len(block["diff_heard"]) > 3
        ):
            # Otra versión (en vivo, con partes habladas): lo que sólo la
            # letra oficial agrega no se propone.
            continue
        if block["spelling_only"] and "official" not in sources:
            continue  # "urgo"/"hurgo" sólo lo decide la letra oficial
        only_function = all(t in _FUNCTION for t in block["diff_heard"] + block["diff_screen"])
        if len(sources) < 2 and only_function and not (only_official and same_version):
            continue
        kind = "heard_different" if block["fix"]["type"] == "replace" else "missing"
        # Si una persona ya cambió ese texto a propósito (por ejemplo, por un
        # pedido del cliente), no se la obliga a volver a decidir: sugerencia.
        edited = block["fix"]["type"] == "replace" and \
            _key_text(block["fix"]["find"]) in human_edited.get(block["line"], set())
        items.append({
            "kind": kind, "line": block["line"], "required": len(sources) >= 2 and not edited,
            "title": "Se escucha distinto" if kind == "heard_different" else "Falta texto",
            "why": _why(sources), "sources": sources, "fix": block["fix"],
            "alternatives": block.get("alternatives") or [],
            "action": "Corregir" if kind == "heard_different" else "Agregar",
            "dismiss": "Está bien así" if kind == "heard_different" else "No se canta",
        })

    vocabulary = {norm_token(w["word"]) for w in witness} | {
        t["tok"] for t in _ear_tokens(gemini)} | {t["tok"] for t in official_tokens}
    items.extend(_rule_question_marks(segments))
    items.extend(_rule_title(segments, title))
    items.extend(_rule_accents(segments))
    items.extend(_rule_joined(segments, _bigrams(witness_text, gemini, official_clean), vocabulary))
    items.extend(_rule_orphans(segments))
    items.extend(_rule_memory(segments, memory_pairs or {}, witness))
    items.extend(_rule_chorus(segments, original_segments))
    items.extend(_rule_timing(segments, witness, machine))
    if same_version and official_ops:
        items.extend(_rule_layout_official(segments, screen, official_tokens, official_ops))

    items = _finalize(segments, items)
    dismissed = dismissed_keys(segments)
    for item in items:
        item["dismissed"] = all(k in dismissed for k in item["keys"])
    pending = [i for i in items if not i["dismissed"]]
    return {
        "schema": SCHEMA,
        "mode": mode(),
        "items": pending[:MAX_ITEMS],
        "required_count": sum(1 for i in pending if i["required"]),
        "suggested_count": sum(1 for i in pending if not i["required"]),
        "sources": {
            "witness": bool(witness), "gemini": bool(gemini),
            "official": bool(official_tokens), "memory": len(memory_pairs or {}),
            "similarity": similarity,
        },
        "risk": _risk(witness, similarity, pending, len(segments)),
    }


def _finalize(segments: list[dict], raw: list[dict]) -> list[dict]:
    """Deduplica (misma línea y mismo arreglo) y agrupa arreglos idénticos en
    distintas líneas en un solo punto con varias repeticiones."""
    # Lo mismo faltante detectado por el testigo con tiempo y por un oído sin
    # tiempo es UN punto: queda el del testigo, que sabe dónde va.
    timed = [(i["text"], i["evidence_time"]) for i in raw
             if i["kind"] == "missing" and "evidence_time" in i]
    kept = []
    for item in raw:
        if item["kind"] == "missing" and "evidence_time" not in item and "occurrences" not in item:
            text = _key_text(item["fix"].get("text"))
            start = _f(segments[item["line"]].get("start"))
            end = _f(segments[item["line"]].get("end"))
            if any(_key_text(t) == text and start - 4 <= at <= end + 4 for t, at in timed):
                continue
        kept.append(item)
    per_line: dict[tuple, dict] = {}
    for item in kept:
        if "occurrences" in item:
            occ = item["occurrences"][0]
            fix = occ["fix"]
            line = occ["line_index"]
            sig = (line, ("new_line", _key_text(item["text"]), occ["start"])
                   if fix["type"] == "new_line" else _fix_signature(fix))
        else:
            line = item["line"]
            fix = item["fix"]
            occ = _occurrence(segments, line, fix)
            if "preview_after" in item:
                occ["after"] = item.pop("preview_after")
            if "preview_before" in item:
                occ["before"] = item.pop("preview_before")
            sig = (line, _fix_signature(fix) if fix["type"] in {"replace", "insert"}
                   else (fix["type"], item["kind"], _item_key(item["kind"], fix,
                                                              segments[line].get("text", ""))))
        item = {**item, "_occ": occ, "_line_text": segments[line].get("text", "")
                if 0 <= line < len(segments) else ""}
        prev = per_line.get(sig)
        if prev is None:
            item["sources"] = set(item.get("sources") or ())
            per_line[sig] = item
        else:
            prev["sources"] |= set(item.get("sources") or ())
            prev["required"] = prev["required"] or item["required"] or (
                prev["kind"] in {"missing", "heard_different"} and len(prev["sources"]) >= 2)
            if prev["kind"] in {"missing", "heard_different"} and prev["sources"]:
                prev["why"] = _why(prev["sources"])
    grouped: dict[tuple, dict] = {}
    for sig, item in per_line.items():
        occ = item["_occ"]
        fix = occ["fix"]
        if fix["type"] == "new_line":
            group_sig = ("new_line", sig[1:])
        elif fix["type"] in {"replace", "insert"}:
            group_sig = (item["kind"], sig[1])
        else:
            # Signos, uniones, cortes y timing: sólo se agrupan líneas con el
            # mismo texto (un coro), nunca líneas distintas.
            group_sig = (item["kind"], fix["type"], _key_text(item["_line_text"]),
                         fix.get("mode"), fix.get("direction"))
        key = item.get("key") or _item_key(item["kind"], fix, item["_line_text"])
        g = grouped.get(group_sig)
        if g is None:
            grouped[group_sig] = {
                "kind": item["kind"], "group": KIND_GROUP.get(item["kind"], "text"),
                "required": bool(item["required"]),
                "title": item["title"], "why": item.get("why", ""),
                "sources": sorted(item["sources"]), "action": item.get("action", "Corregir"),
                "dismiss": item.get("dismiss", "Está bien así"),
                "keys": [key], "occurrences": [occ],
                "alternatives": item.get("alternatives") or [],
            }
        else:
            g["occurrences"].append(occ)
            g["keys"].append(key)
            g["required"] = g["required"] or bool(item["required"])
            g["sources"] = sorted(set(g["sources"]) | item["sources"])
    out = []
    for g in grouped.values():
        g["occurrences"].sort(key=lambda o: o["start"])
        g["keys"] = sorted(set(g["keys"]))
        g["start"] = g["occurrences"][0]["start"]
        g["end"] = g["occurrences"][0]["end"]
        g["id"] = hashlib.sha1("|".join(g["keys"]).encode("utf-8")).hexdigest()[:12]
        out.append(g)
    # Primero lo obligatorio con más fuentes (más seguro), después por tiempo.
    out.sort(key=lambda g: (not g["required"], -len(g["sources"]), g["start"]))
    return out


def _human_edited_lines(segments: list[dict], original_segments: Any) -> dict[int, set[str]]:
    """Por línea, las palabras que NO vienen de la transcripción original:
    las escribió una persona."""
    orig = [t["tok"] for t in _screen_tokens([s for s in (original_segments or []) if isinstance(s, dict)])]
    screen = _screen_tokens(segments)
    if not orig or len(orig) > MAX_REVIEW_TOKENS:
        return {}
    edited: dict[int, set[str]] = defaultdict(set)
    for tag, i1, i2, _j1, _j2 in aligned_opcodes([s["tok"] for s in screen], orig):
        if tag in {"replace", "delete"}:
            for s in screen[i1:i2]:
                edited[s["line"]].add(s["tok"])
    # Una corrección de varias palabras se compara por su texto completo.
    out: dict[int, set[str]] = {}
    for line, toks in edited.items():
        line_toks = [s["tok"] for s in screen if s["line"] == line]
        spans = {" ".join(line_toks[a:b]) for a in range(len(line_toks))
                 for b in range(a + 1, min(len(line_toks), a + 8) + 1)
                 if any(t in toks for t in line_toks[a:b])}
        out[line] = spans
    return out


def _risk(witness, similarity, items, n_lines) -> dict:
    reasons = []
    if witness and similarity.get("witness", 1.0) < 0.35:
        reasons.append("El testigo automático no pudo transcribir bien esta canción")
    suggested = sum(1 for i in items if not i["required"] and i["kind"] in {"heard_different", "missing"})
    if n_lines and suggested >= max(10, n_lines // 3):
        reasons.append("Los oídos automáticos no coinciden en muchas líneas")
    if similarity.get("gemini", 1) and similarity.get("gemini", 1) < 0.6:
        reasons.append("La transcripción de control no se parece a la letra")
    return {"level": "high" if reasons else "normal", "reasons": reasons}


def gemini_text(machine_evidence: Any) -> str:
    """Transcripción de Gemini sobre el audio completo SIN letra de
    referencia: la única versión de Gemini que es un oído independiente."""
    if not isinstance(machine_evidence, dict):
        return ""
    for hyp in machine_evidence.get("hypotheses_by_family") or []:
        if not isinstance(hyp, dict):
            continue
        if not str(hyp.get("family") or "").startswith("google/gemini"):
            continue
        if hyp.get("kind") != "text" or hyp.get("transformation") != "gemini_reference_hypothesis_raw":
            continue
        for ev in hyp.get("events") or []:
            if isinstance(ev, dict) and ev.get("text"):
                return str(ev["text"])
    return ""


def required_for_scope(review: dict, scope: str) -> list[dict]:
    """Puntos que bloquean según el camino de aprobación: ``full`` (editor y
    campañas), ``missing_only`` (re-render por pedido de cambio: sólo lo que
    se canta y se perdió) o ``none``."""
    if scope == "none":
        return []
    items = [i for i in review.get("items") or [] if i.get("required")]
    if scope == "missing_only":
        items = [i for i in items if i.get("kind") == "missing"]
    return items


class LyricReviewPending(Exception):
    """La letra no puede aprobarse con puntos obligatorios sin decidir."""

    def __init__(self, review: dict, pending: list[dict] | None = None):
        super().__init__("lyric_review_pending")
        self.review = review
        self.pending = pending if pending is not None else [
            i for i in review.get("items", []) if i.get("required")]


def conflict_detail(exc: LyricReviewPending) -> dict:
    n = len(exc.pending)
    return {
        "code": "lyric_review_pending",
        "message": (
            f"Quedan {n} {'punto' if n == 1 else 'puntos'} de la revisión rápida por decidir. "
            "Abrí el editor, aplicá cada arreglo o marcá que está bien así, y volvé a aprobar."
        ),
        "lyric_review": exc.review,
    }
