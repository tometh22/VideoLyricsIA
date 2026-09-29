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
    find_missing_heard_words,
    machine_words,
    norm_token,
    witness_words,
)

SCHEMA = "lyric-review-v1"
MAX_BLOCK_TOKENS = 8
MAX_ITEMS = 60

# Palabras cortas cuya sola diferencia casi siempre es ruido de un oído.
_FUNCTION = {
    "a", "al", "de", "del", "el", "la", "las", "lo", "los", "en", "y", "e",
    "o", "u", "que", "se", "me", "te", "mi", "tu", "su", "un", "una", "es",
    "ya", "no", "si", "con", "por", "pa", "le", "les", "nos",
}
# Formas con tilde que no son error de escritura sino otra palabra.
_DIACRITIC_PAIRS = {
    "tu", "si", "el", "mi", "te", "se", "de", "mas", "solo", "aun", "que",
    "como", "cuando", "donde", "quien", "cual", "cuanto", "este", "esta",
    "ese", "esa", "o", "esto", "aquel", "aquella",
}
_INTERROGATIVES = {
    "qué", "cómo", "cuándo", "dónde", "adónde", "quién", "quiénes", "cuál",
    "cuáles", "cuánto", "cuánta", "cuántos", "cuántas", "acaso",
}
_EXCLAMATIVE = re.compile(
    r"^\s*¿?\s*(qué|cómo|cuán|cuánto|cuánta)\s+"
    r"(lind[oa]s?|bell[oa]s?|hermos[oa]s?|bonit[oa]s?|trist[ea]s?|buen[oa]s?|"
    r"grande|feo|fea|mal|bien|tant[oa]s?|lejos|rico|rica|dulce|lindo)\b",
    re.I,
)
_ORPHANS = {"que", "y", "si", "a", "de", "la", "el", "en", "o", "no", "me",
            "te", "se", "lo", "un", "es", "mi", "tu", "por", "con"}
_WORD_RE = re.compile(r"[^\W_]+(?:'[^\W_]*)?", re.UNICODE)
_NON_LATIN = re.compile(r"[Ͱ-ϿЀ-ӿ֐-ۿ぀-ヿ一-鿿]")

SOURCE_LABELS = {
    "machine": "la máquina",
    "witness": "el testigo",
    "gemini": "Gemini",
    "official": "la letra oficial",
    "memory": "una corrección anterior de este artista",
}


def mode() -> str:
    """``enforce`` (default): aprobar exige decidir los puntos obligatorios.
    ``observe``: se muestran sin bloquear. ``off``: no se calculan."""
    value = os.environ.get("LYRIC_REVIEW_MODE", "enforce").strip().lower()
    return value if value in {"enforce", "observe", "off"} else "enforce"


def fold(text: str) -> str:
    folded = unicodedata.normalize("NFKD", str(text or "").lower())
    return "".join(ch for ch in folded if not unicodedata.combining(ch))


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


def _replacement_for(screen_raw: list[str], heard_raw: list[str]) -> str:
    """Palabras oídas con la puntuación y las mayúsculas de la pantalla."""
    lead = _split_punct(screen_raw[0])[0] if screen_raw else ""
    trail = _split_punct(screen_raw[-1])[2] if screen_raw else ""
    clean = [_split_punct(w)[1] for w in heard_raw]
    clean = [w for w in clean if w]
    body = " ".join(clean)
    if screen_raw:
        body = _match_case(_split_punct(screen_raw[0])[1], body)
    return f"{lead}{body}{trail}"


# --------------------------------------------------------------------------
# Oídos: dónde una fuente independiente escuchó otra cosa que la pantalla.
# --------------------------------------------------------------------------

def _screen_tokens(segments: list[dict]) -> list[dict]:
    out = []
    for i, seg in enumerate(segments):
        if not isinstance(seg, dict):
            continue
        for k, raw in enumerate(str(seg.get("text") or "").split()):
            tok = norm_token(raw)
            if tok:
                out.append({"tok": tok, "raw": raw, "line": i, "pos": k})
    return out


_FILLERS = {"eh", "ehh", "ah", "ahh", "mm", "mmm", "hmm", "aja", "uh", "um", "em"}


def _ear_tokens(text: str) -> list[dict]:
    out = []
    for raw in str(text or "").split():
        tok = norm_token(raw)
        if tok and tok not in _FILLERS and not _NON_LATIN.search(raw):
            out.append({"tok": tok, "raw": raw})
    return out


def _ear_blocks(segments: list[dict], screen: list[dict], ear_text: str,
                source: str, witness: list[dict] | None = None) -> tuple[list[dict], float]:
    """Diferencias pantalla↔oído, alineadas en orden. Devuelve también qué
    tanto se parece el oído a la pantalla: si casi nada coincide (otra
    versión de la canción, alucinación), el oído no se usa."""
    ear = _ear_tokens(ear_text)
    if not screen or len(ear) < 8:
        return [], 0.0
    sm = difflib.SequenceMatcher(
        a=[s["tok"] for s in screen], b=[e["tok"] for e in ear], autojunk=False,
    )
    similarity = sm.ratio()
    out = []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            continue
        scr, heard = screen[i1:i2], ear[j1:j2]
        if not heard or len(heard) > MAX_BLOCK_TOKENS or len(scr) > MAX_BLOCK_TOKENS:
            continue
        s_toks, h_toks = [s["tok"] for s in scr], [h["tok"] for h in heard]
        if _same("".join(s_toks), "".join(h_toks)):
            continue  # separado/pegado u ortografía: lo cubren las reglas
        if _is_loop([{"word": h["raw"]} for h in heard]):
            continue
        diff_heard = [t for t in h_toks if not any(_same(t, s) for s in s_toks)]
        if not diff_heard:
            continue
        if scr and _only_style_difference([s["raw"] for s in scr], [h["raw"] for h in heard]):
            continue
        if scr:
            lines = {s["line"] for s in scr}
            if len(lines) > 1:
                continue  # una corrección nunca cruza carteles
            line = scr[0]["line"]
            fix = {
                "type": "replace",
                "find": " ".join(s["raw"] for s in scr),
                "replace": _replacement_for([s["raw"] for s in scr], [h["raw"] for h in heard]),
            }
        else:
            # El oído tiene palabras que la pantalla no: se insertan en la
            # línea del vecino que sí coincide.
            before = screen[i1 - 1] if i1 > 0 else None
            after = screen[i1] if i1 < len(screen) else None
            anchor = before or after
            if anchor is None:
                continue
            line = anchor["line"]
            if before is not None and after is not None and before["line"] != after["line"]:
                # Entre dos carteles: va al final del anterior salvo que el
                # siguiente empiece la frase (mayúscula).
                line = after["line"] if after["raw"][:1].isupper() else before["line"]
            same_line = [s for s in screen if s["line"] == line]
            fix = {
                "type": "insert",
                "text": " ".join(_split_punct(h["raw"])[1] for h in heard),
                "anchor_before": before["tok"] if before and before["line"] == line else "",
                "anchor_after": after["tok"] if after and after["line"] == line else "",
                "insert_at_word": (before["pos"] + 1) if before and before["line"] == line else 0,
            }
            if not same_line:
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
            "line": line, "fix": fix, "source": source,
            "diff_heard": diff_heard,
            "diff_screen": [t for t in s_toks if not any(_same(t, h) for h in h_toks)],
        })
    return out, similarity


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
            same_find = sig[1][0] == "insert" or _same(
                "".join(key[1][1].split()), "".join(sig[1][1].split()))
            same_fix = _same("".join(key[1][-1 if sig[1][0] == "replace" else 1].split()),
                             "".join(sig[1][-1 if sig[1][0] == "replace" else 1].split()))
            if same_find and same_fix:
                match = g
                break
        if match is None:
            grouped[sig] = {**b, "sources": {b["source"]}}
        else:
            match["sources"].add(b["source"])
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
        options.sort(key=lambda o: (len(o["sources"]), max(_SOURCE_RANK.get(x, 0) for x in o["sources"])),
                     reverse=True)
        best = options[0]
        best["alternatives"] = [
            {"replace": o["fix"]["replace"], "why": _why(o["sources"]), "sources": sorted(o["sources"])}
            for o in options[1:]
        ]
        out.append(best)
    return out


# --------------------------------------------------------------------------
# Reglas deterministas (medidas sobre los pedidos #112-#131).
# --------------------------------------------------------------------------

_RELATIVES = {"cuando", "como", "donde", "adonde", "que", "quien", "quienes",
              "cual", "cuales", "cuanto", "cuanta", "cuantos", "cuantas"}


def _rule_question_marks(segments: list[dict]) -> list[dict]:
    """Sólo los tres casos que UMG devolvió; una pregunta de sí/no ("¿Me
    querés?") es legítima y no se toca."""
    out = []
    for i, seg in enumerate(segments):
        text = str(seg.get("text") or "")
        if "?" not in text and "¿" not in text:
            continue
        for part in re.findall(r"¿([^?¿]*)\??|^([^¿]*)\?", text):
            body = (part[0] or part[1]).strip()
            words = _words(body)
            if not words:
                continue
            first = words[0]
            if _EXCLAMATIVE.search(body):
                why, mode = "Es una exclamación: va con ¡ !", "exclaim"
            elif fold(first) in _RELATIVES and first.lower() not in _INTERROGATIVES:
                why = f"«{first}» sin tilde no pregunta: sin signos de pregunta"
                mode = "remove_question"
            elif fold(" ".join(words[:3])).startswith("a ver si"):
                why, mode = "«A ver si…» no es una pregunta", "remove_question"
            else:
                continue
            out.append({
                "kind": "question_marks", "line": i, "required": True,
                "title": "Signos de pregunta", "why": why,
                "fix": {"type": "punctuation", "mode": mode},
                "action": "Corregir",
            })
            break
    return out


def _title_words(title: str) -> list[str]:
    title = unicodedata.normalize("NFC", str(title or ""))
    title = re.sub(r"\(.*?\)|\[.*?\]", " ", str(title or ""))
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
        hit = None
        n = len(words)
        for k in range(0, len(raw) - n + 1):
            span = core[k:k + n]
            diffs = [j for j in range(n) if span[j] != folded[j]]
            if len(diffs) == 1 and folded[diffs[0]] == span[diffs[0]] + "s":
                j = diffs[0]
                fixed = list(raw[k:k + n])
                lead, body, trail = _split_punct(fixed[j])
                fixed[j] = f"{lead}{body}s{trail}"
                hit = (k, n, " ".join(fixed))
                break
        if hit is None and n >= 2:
            for k in range(0, len(raw) - (n - 1) + 1):
                span = core[k:k + n - 1]
                if "".join(span) == "".join(folded) and span != folded:
                    title_words = [w.lower() for w in words]
                    hit = (k, n - 1, _replacement_for(raw[k:k + n - 1], title_words))
                    break
        if hit:
            k, size, replacement = hit
            out.append({
                "kind": "title_spelling", "line": i, "required": True,
                "title": "Como en el título",
                "why": f"El título dice «{' '.join(words)}»",
                "fix": {"type": "replace", "find": " ".join(raw[k:k + size]), "replace": replacement},
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
        for i, seg in enumerate(segments):
            text = str(seg.get("text") or "")
            for w in _words(text):
                if fold(w) == base and w.lower() != best:
                    out.append({
                        "kind": "accent_inconsistent", "line": i, "required": True,
                        "title": "Tildes distintas",
                        "why": f"En la canción aparece «{best}» y también «{w.lower()}»",
                        "fix": {"type": "replace", "find": w, "replace": _match_case(w, best)},
                        "action": "Unificar",
                    })
    return out


_ENCLITICS = {"me", "te", "se", "la", "lo", "le", "nos", "los", "las", "les", "sela", "selo"}
_REAL_COMPOUNDS = {"porque", "porqué", "sino", "aunque", "también", "tampoco", "adonde", "quizás"}


def _rule_joined(segments: list[dict], ear_bigrams: set[tuple[str, str]],
                 ear_vocab: set[str]) -> list[dict]:
    """Dos palabras pegadas: sólo si un oído las escuchó SEPARADAS y seguidas
    ("logro entender", "si esto"). Así "Desnúdate", "comerme" o "porque"
    nunca se separan."""
    out = []
    for i, seg in enumerate(segments):
        text = str(seg.get("text") or "")
        for raw in text.split():
            # Coma sin espacio ("vuelvo,vuelvo"): siempre es un error de tipeo.
            if re.search(r"[^\W\d_][,;][^\W\d_]", raw):
                fixed = re.sub(r"([,;])(?=[^\W\d_])", r"\1 ", raw)
                out.append({
                    "kind": "joined_words", "line": i, "required": True,
                    "title": "Falta un espacio", "why": "Después de la coma va un espacio",
                    "fix": {"type": "replace", "find": raw, "replace": fixed},
                    "action": "Separar",
                })
                continue
            word = _split_punct(raw)[1]
            tok = norm_token(word)
            if len(tok) < 5 or fold(word) in _REAL_COMPOUNDS or tok in ear_vocab:
                continue  # algún oído la escuchó junta: es una palabra
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
                    "title": "Palabras pegadas",
                    "why": f"«{word}» son dos palabras",
                    "fix": {"type": "replace", "find": word, "replace": fixed},
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
            "fix": {"type": "merge", "direction": direction},
            "preview_after": (
                f"{seg.get('text', '').strip()} {str(other.get('text') or '').strip()}"
                if direction == "next" else
                f"{str(other.get('text') or '').strip()} {seg.get('text', '').strip()}"
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
                "fix": {"type": "replace", "find": word, "replace": _match_case(word, right)},
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
        if len(members) < 2:
            continue
        mapped = [(m, _map_to_current(segments, m)) for m in members]
        current = [(i, str(segments[i].get("text") or "")) for _, i in mapped if i is not None]
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
        best_key, votes = variants.most_common(1)[0]
        best_text = next(t for _, t in edited if _key_text(t) == best_key)
        for i in sorted(set(untouched)):
            out.append({
                # Sugerencia: a veces la corrección de una repetición trae un
                # error de tipeo, y propagarla lo multiplicaría.
                "kind": "chorus_propagate", "line": i, "required": False,
                "title": "Coro corregido en parte",
                "why": f"Esta frase se repite {len(current)} veces y la corregiste en {len(edited)}",
                "fix": {"type": "replace", "find": str(segments[i].get("text") or ""),
                        "replace": best_text},
                "action": "Igualar",
                "dismiss": "Esta repetición es distinta",
            })
    return out


# --------------------------------------------------------------------------
# Armado final.
# --------------------------------------------------------------------------

def apply_punctuation(text: str, mode: str) -> str:
    """Misma operación que hace el editor al aplicar el arreglo."""
    body = re.sub(r"\s{2,}", " ", text.replace("¿", "").replace("?", "")).strip()
    if mode == "exclaim":
        return "¡" + body.lstrip("¡").rstrip("!") + "!"
    return body


def _preview(segments: list[dict], line: int, fix: dict) -> tuple[str, str]:
    before = str(segments[line].get("text") or "") if 0 <= line < len(segments) else ""
    if fix["type"] == "punctuation":
        return before, apply_punctuation(before, fix["mode"])
    if fix["type"] == "replace":
        # Igual que el editor: todas las apariciones en la línea.
        return before, before.replace(fix["find"], fix["replace"])
    if fix["type"] == "insert":
        tokens = before.split()
        at = max(0, min(len(tokens), int(fix.get("insert_at_word") or 0)))
        words = fix.get("text", "")
        if at == 0 and tokens:
            words = words[:1].upper() + words[1:]
        return before, " ".join(tokens[:at] + [words] + tokens[at:])
    return before, before


def _item_key(kind: str, fix: dict, start: float | None = None) -> str:
    if fix["type"] == "replace":
        base = f"{kind}:{_key_text(fix['find'])}>{_key_text(fix['replace'])}"
    elif fix["type"] == "punctuation":
        base = f"{kind}:{fix['mode']}"
    elif fix["type"] == "merge":
        base = f"{kind}:{fix.get('direction')}"
    else:
        base = f"{kind}:+{_key_text(fix.get('text'))}"
    if start is not None:
        base += f"@{int(round(start))}"
    return base


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
        occ = _occurrence(segments, line, fix) if segments else {}
        if fix["type"] == "new_line":
            occ.update({"start": fix["start"], "end": fix["end"],
                        "before": "", "after": alert["text"][:1].upper() + alert["text"][1:]})
        else:
            occ.update({"start": alert["start"], "end": alert["end"]})
        items.append({
            "kind": "missing", "required": True, "title": "Falta texto",
            "why": _why(sources), "sources": sources, "action": "Agregar",
            "dismiss": "No se canta", "key": alert["key"],
            "occurrences": [occ], "text": alert["text"],
        })
    return items


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
    segments = [s for s in (segments or []) if isinstance(s, dict)]
    witness = witness_words(machine_evidence)
    machine = machine_words(original_segments)
    gemini = gemini_text(machine_evidence)
    screen = _screen_tokens(segments)
    items: list[dict] = []

    items.extend(_missing_items(segments, witness, machine))

    blocks: list[dict] = []
    similarity: dict[str, float] = {}
    witness_text = " ".join(w["word"] for w in witness)
    for source, text, timed in (
        ("witness", witness_text, witness),
        ("gemini", gemini, None),
        ("official", official_text or "", None),
    ):
        found, sim = _ear_blocks(segments, screen, text, source, timed)
        similarity[source] = round(sim, 3)
        # Un oído que casi no coincide con la pantalla es otra versión o una
        # alucinación: no aporta.
        if sim >= (0.45 if source != "witness" else 0.35):
            blocks.extend(found)
    same_version = similarity.get("official", 0.0) >= 0.85
    human_edited = _human_edited_lines(segments, original_segments)
    for block in _merge_ear_blocks(blocks):
        sources = block["sources"]
        only_official = sources == {"official"}
        if only_official and block["fix"]["type"] == "insert" and (
            not same_version or len(block["diff_heard"]) > 3
        ):
            # Otra versión (en vivo, con partes habladas): lo que sólo la
            # letra oficial agrega no se propone.
            continue
        only_function = all(t in _FUNCTION for t in block["diff_heard"] + block["diff_screen"])
        if len(sources) < 2 and only_function and not (only_official and same_version):
            continue
        kind = "heard_different" if block["fix"]["type"] == "replace" else "missing"
        # Si una persona ya cambió ese texto a propósito (por ejemplo, por un
        # pedido del cliente), no se la obliga a volver a decidir: sugerencia.
        edited = block["fix"]["type"] == "replace" and _key_text(block["fix"]["find"]) in human_edited.get(block["line"], set())
        items.append({
            "kind": kind, "line": block["line"], "required": len(sources) >= 2 and not edited,
            "title": "Se escucha distinto" if kind == "heard_different" else "Falta texto",
            "why": _why(sources), "sources": sources, "fix": block["fix"],
            "alternatives": block.get("alternatives") or [],
            "action": "Corregir" if kind == "heard_different" else "Agregar",
            "dismiss": "Está bien así" if kind == "heard_different" else "No se canta",
        })

    vocabulary = {norm_token(w["word"]) for w in witness} | {
        t["tok"] for t in _ear_tokens(gemini)} | {t["tok"] for t in _ear_tokens(official_text or "")}
    items.extend(_rule_question_marks(segments))
    items.extend(_rule_title(segments, title))
    items.extend(_rule_accents(segments))
    items.extend(_rule_joined(segments, _bigrams(witness_text, gemini, official_text or ""), vocabulary))
    items.extend(_rule_orphans(segments))
    items.extend(_rule_memory(segments, memory_pairs or {}, witness))
    items.extend(_rule_chorus(segments, original_segments))

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
            "official": bool(official_text), "memory": len(memory_pairs or {}),
            "similarity": similarity,
        },
        "risk": _risk(witness, similarity, pending, len(segments)),
    }


def _finalize(segments: list[dict], raw: list[dict]) -> list[dict]:
    """Deduplica (misma línea y mismo arreglo) y agrupa arreglos idénticos en
    distintas líneas en un solo punto con varias repeticiones."""
    per_line: dict[tuple, dict] = {}
    for item in raw:
        if item["kind"] == "missing" and "occurrences" in item:
            occ = item["occurrences"][0]
            sig = (occ["line_index"], _fix_signature(occ["fix"]) if occ["fix"]["type"] != "new_line"
                   else ("new_line", _key_text(item["text"]), occ["start"]))
            item = {**item, "_line": occ["line_index"], "_occ": occ}
        else:
            occ = _occurrence(segments, item["line"], item["fix"])
            if "preview_after" in item:
                occ["after"] = item.pop("preview_after")
            ftype = item["fix"]["type"]
            sig = (item["line"], _fix_signature(item["fix"]) if ftype in {"replace", "insert"}
                   else (ftype, item["fix"].get("direction") or item["fix"].get("mode")))
            item = {**item, "_line": item["line"], "_occ": occ}
        prev = per_line.get(sig)
        if prev is None:
            item["sources"] = set(item.get("sources") or ())
            per_line[sig] = item
        else:
            prev["sources"] |= set(item.get("sources") or ())
            prev["required"] = prev["required"] or item["required"] or len(prev["sources"]) >= 2
            if prev["sources"]:
                prev["why"] = _why(prev["sources"]) if prev["kind"] in {
                    "missing", "heard_different"} else prev["why"]
    grouped: dict[tuple, dict] = {}
    for sig, item in per_line.items():
        occ = item["_occ"]
        fix = occ["fix"]
        if fix["type"] == "new_line":
            group_sig = ("new_line", sig[1:])
        else:
            group_sig = (item["kind"], sig[1])
        g = grouped.get(group_sig)
        key = item.get("key") or _item_key(
            item["kind"], fix, occ["start"] if item["kind"] == "missing" else None)
        if g is None:
            grouped[group_sig] = {
                "kind": item["kind"], "required": bool(item["required"]),
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
    out.sort(key=lambda g: (not g["required"], g["start"]))
    return out


def _human_edited_lines(segments: list[dict], original_segments: Any) -> dict[int, set[str]]:
    """Por línea, las palabras que NO vienen de la transcripción original:
    las escribió una persona."""
    orig = [t["tok"] for t in _screen_tokens([s for s in (original_segments or []) if isinstance(s, dict)])]
    screen = _screen_tokens(segments)
    if not orig:
        return {}
    sm = difflib.SequenceMatcher(a=[s["tok"] for s in screen], b=orig, autojunk=False)
    edited: dict[int, set[str]] = defaultdict(set)
    for tag, i1, i2, _j1, _j2 in sm.get_opcodes():
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


class LyricReviewPending(Exception):
    """La letra no puede aprobarse con puntos obligatorios sin decidir."""

    def __init__(self, review: dict):
        super().__init__("lyric_review_pending")
        self.review = review


def conflict_detail(exc: LyricReviewPending) -> dict:
    pending = [i for i in exc.review.get("items", []) if i.get("required")]
    return {
        "code": "lyric_review_pending",
        "message": (
            f"Quedan {len(pending)} puntos de la revisión rápida por decidir. "
            "Abrí el editor, aplicá cada arreglo o marcá que está bien así, y volvé a aprobar."
        ),
        "lyric_review": exc.review,
    }
