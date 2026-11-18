"""Revisión rápida: timing y fronteras de frase determinísticos.

POR QUÉ EXISTE
--------------
La evaluación de las 15 demos (16-11-2026) encontró dos clases de error que
ninguna señal marcaba:

- **Timing.** La línea se va de pantalla mientras se sigue cantando (Facção
  Central: la última línea dura 0,86 s y sus palabras siguen hasta 28,5 s).
  Las "partes a revisar" sólo miran la confianza CTC, que en los jobs
  WhisperX ni se calcula. En UMG son el 8 % de los pedidos.
- **Frontera de frase.** Líneas que terminan en un artículo, una preposición
  o un pronombre átono ("…hoje me", "…feliz quem") y líneas de una sola
  palabra ("Sabe"). La regla de estructura existente sólo mira el largo.

Todo lo que sale de acá es SUGERENCIA (``required=False``): nunca bloquea la
aprobación, con cualquier ``LYRIC_REVIEW_MODE``. Los puntos de timing con
evidencia por palabra traen un arreglo de un click ("Alargar"); el resto se
ofrece para escuchar, igual que la regla de estructura existente.

``LYRIC_REVIEW_BOUNDARY_RULES=0`` apaga ambas reglas sin tocar el resto de la
revisión. Puro: sin I/O.
"""
from __future__ import annotations

import difflib
import hashlib
import json
import os
import re
import unicodedata
from typing import Any, Iterable

from heard_words import _f, norm_token

# ---------------------------------------------------------------------------
# Umbrales (calibrados sobre las 15 demos del 16-11-2026, ver PR).
# ---------------------------------------------------------------------------
# La línea se va antes de que termine su última palabra.
WORD_END_SLACK_S = 0.3
# La línea se va mucho antes de que termine lo que oyó el proveedor (ASR).
PROVIDER_END_SLACK_S = 1.0
# Muchas palabras en muy poco tiempo: no se alcanza a leer.
COMPRESSED_MIN_WORDS = 4
COMPRESSED_MAX_S = 1.0
# Canto sin línea después de la última línea (testigo whisper-1).
TAIL_MIN_WORDS = 3
TAIL_MIN_SPAN_S = 1.0
TAIL_AFTER_S = 0.3
# Frontera de frase: una palabra sola sostenida esto o más es intencional.
HELD_WORD_S = 1.5
# Una primera línea que arranca en 0 s puede ser el final de una frase cortada.
EDGE_CUT_S = 0.1

_WORD_RE = re.compile(r"[^\W_]+(?:'[^\W_]*)?", re.UNICODE)

# Palabras átonas que no pueden cerrar una frase. Se comparan CON tildes:
# "qué", "él", "tú", "sé", "é" son tónicas y sí cierran frase.
_ENDING_COMMON = {
    "a", "de", "en", "con", "por", "que", "porque",
    "me", "te", "se", "nos", "e", "o",
}
_ENDING_ES = _ENDING_COMMON | {
    "el", "la", "los", "las", "un", "una", "unos", "unas",
    "al", "del", "sin", "desde",
    "le", "les", "lo",
    "y", "u", "ni", "pero",
}
_ENDING_PT = _ENDING_COMMON | {
    "os", "as", "um", "uma", "uns", "umas",
    "à", "às", "ao", "aos", "do", "da", "dos", "das", "no", "na", "nos",
    "nas", "num", "numa", "dum", "duma", "pelo", "pela", "pelos", "pelas",
    "pra", "pro", "pras", "pros", "em", "com", "sem",
    "lhe", "lhes", "vos",
    "ou", "nem", "mas", "quem",
}
# Sin idioma conocido sólo valen las que son átonas en los dos ("no" es
# negación en español y contracción en portugués; "da", "dos", "pelo"...
# son verbos o sustantivos en español).
_ENDING_ANY = _ENDING_ES & _ENDING_PT

# "no se" / "lo se" casi siempre es "sé" sin tilde.
_SE_AS_KNOW = {"no", "lo", "yo", "eu", "não"}

# Una palabra sola que es un grito o un ad-lib está bien en pantalla.
_INTERJECTIONS = {
    "oh", "ohh", "ooh", "uh", "uhh", "uuh", "ah", "ahh", "aah", "eh", "ehh",
    "ey", "hey", "ay", "ayy", "yeah", "yeh", "yah", "ye", "wow", "uoh", "uou",
    "woo", "wuh", "ou", "uo", "eo", "mm", "mmm", "hmm", "um", "em", "aja",
    "ajá", "aham", "uhum", "la", "lala", "na", "nana", "olé", "ole", "epa",
    "uepa", "wepa", "dale", "vamo", "vamos", "vai", "bora", "alô", "aló",
    "ya", "ra", "pum", "prr", "brr", "skrt", "sí", "não", "no", "yes",
}
_VOWEL_CRY = re.compile(r"^[aeiouyh]+$")

_ES_MARKERS = {"el", "los", "las", "y", "yo", "está", "tú", "del", "una",
               "pero", "qué", "también", "muy", "porque", "cuando", "ella",
               "mí", "sí", "hay", "estoy", "eres", "es", "mis", "sus"}
_PT_MARKERS = {"não", "você", "eu", "é", "um", "uma", "com", "do", "da",
               "pra", "os", "as", "mais", "também", "meu", "minha", "isso",
               "tá", "estou", "quem", "nós", "ao", "na", "no", "em"}


def enabled() -> bool:
    value = os.environ.get("LYRIC_REVIEW_BOUNDARY_RULES", "1").strip().lower()
    return value not in {"0", "false", "off", "no"}


def _tokens(text: str) -> list[str]:
    return _WORD_RE.findall(unicodedata.normalize("NFC", str(text or "")))


def _key_text(text: str) -> str:
    return " ".join(t for t in (norm_token(p) for p in str(text or "").split()) if t)


def occurrence_key(prefix: str, segments: list[dict], index: int) -> str:
    """Identidad de UNA aparición: línea, tiempos y texto, como
    ``_rule_phrase_structure``. Cada repetición de un coro se revisa y se
    descarta por separado, y mover sus tiempos vuelve a abrir el punto."""
    seg = segments[index]
    segment_id = seg.get("segment_id")
    if segment_id is None:
        segment_id = seg.get("_id")
    if segment_id is None:
        segment_id = f"index:{index}"
    identity = json.dumps([
        str(segment_id), _f(seg.get("start")), _f(seg.get("end")),
        unicodedata.normalize("NFC", str(seg.get("text") or "")),
    ], ensure_ascii=False)
    return f"{prefix}:" + hashlib.sha256(identity.encode()).hexdigest()


def _fmt(seconds: float) -> str:
    return f"{seconds:.1f}"


def language(machine_evidence: Any, segments: Iterable[dict]) -> str | None:
    """``es``, ``pt`` o ``None``. Primero el idioma que decidió el pipeline;
    si no está, una votación de palabras marcadoras sobre la letra."""
    if isinstance(machine_evidence, dict):
        quality = (machine_evidence.get("decisions") or {}).get("quality") or {}
        lang = str(((quality.get("metrics") or {}).get("language")) or "").lower()[:2]
        if lang in {"es", "pt"}:
            return lang
        if lang:
            return None
    es = pt = 0
    for seg in segments:
        for tok in _tokens(seg.get("text")):
            low = tok.casefold()
            es += low in _ES_MARKERS
            pt += low in _PT_MARKERS
    if es + pt < 4 or max(es, pt) < 2 * min(es, pt):
        return None
    return "es" if es > pt else "pt"


def _locked(seg: dict) -> bool:
    return any(seg.get(k) is True for k in ("locked", "operator_locked", "timing_locked"))


def _similar(a: str, b: str) -> float:
    ka, kb = _key_text(a), _key_text(b)
    if not ka or not kb:
        return 0.0
    return difflib.SequenceMatcher(None, ka.split(), kb.split(), autojunk=False).ratio()


# ---------------------------------------------------------------------------
# Timing.
# ---------------------------------------------------------------------------

def _tail_words(segments: list[dict], witness: list[dict]) -> list[dict]:
    """Palabras del testigo cantadas después de que se fue la última línea."""
    if not segments or not witness:
        return []
    last_end = max(_f(s.get("end")) for s in segments)
    tail = [w for w in witness
            if _f(w.get("start")) >= last_end + TAIL_AFTER_S
            # Un testigo en bucle repite el final con duración cero.
            and _f(w.get("end")) - _f(w.get("start")) >= 0.05]
    if len(tail) < TAIL_MIN_WORDS:
        return []
    if _f(tail[-1].get("end")) - _f(tail[0].get("start")) < TAIL_MIN_SPAN_S:
        return []
    return tail


def timing_items(segments: list[dict], witness: list[dict] | None = None) -> list[dict]:
    order = sorted(range(len(segments)), key=lambda k: _f(segments[k].get("start")))
    last_index = max(range(len(segments)), key=lambda k: _f(segments[k].get("end"))) if segments else None
    tail = _tail_words(segments, witness or [])
    out = []
    for pos, i in enumerate(order):
        seg = segments[i]
        if _locked(seg):
            continue  # una persona fijó estos tiempos
        text = str(seg.get("text") or "")
        toks = [t for t in (norm_token(w) for w in text.split()) if t]
        if not toks:
            continue
        start, end = _f(seg.get("start")), _f(seg.get("end"))
        nxt = segments[order[pos + 1]] if pos + 1 < len(order) else None
        limit = _f(nxt.get("start")) - 0.05 if nxt else float("inf")
        fix_end = None
        reasons: list[str] = []

        # 1. La línea se va antes de que termine su última palabra.
        words = [w for w in seg.get("words") or [] if isinstance(w, dict) and w.get("end") is not None]
        last = [w for w in words if norm_token(w.get("word")) == toks[-1]
                and _f(w.get("start")) >= start - 0.5]
        if last:
            word_end = max(_f(w.get("end")) for w in last)
            if word_end > end + WORD_END_SLACK_S and limit > end + WORD_END_SLACK_S:
                fix_end = round(min(word_end + 0.1, limit), 2)
                reasons.append(f"Se va {_fmt(word_end - end)} s antes de que termine «{toks[-1]}»")

        # 2. …o mucho antes de lo que oyó el proveedor en esa misma línea.
        pe = seg.get("provider_evidence")
        if not reasons and isinstance(pe, dict) and pe.get("end") is not None:
            pe_start, pe_end = _f(pe.get("start"), start), _f(pe.get("end"))
            if (pe_start < end and pe_end > end + PROVIDER_END_SLACK_S
                    and limit > end + 0.5 and _similar(text, pe.get("text") or "") >= 0.6):
                reasons.append(f"Se sigue cantando hasta {_fmt(pe_end)} s y la línea se va a los "
                               f"{_fmt(end)} s")

        # 3. Muchas palabras en muy poco tiempo.
        duration = end - start
        if len(toks) >= COMPRESSED_MIN_WORDS and 0 < duration < COMPRESSED_MAX_S:
            reasons.append(f"{len(toks)} palabras en {_fmt(duration)} s: pasa demasiado rápido para leerla")

        # 4. Canto sin línea al final de la canción.
        if tail and i == last_index:
            heard = " ".join(str(w.get("word") or "").strip() for w in tail[:6])
            span = _f(tail[-1].get("end")) - end
            reasons.append(f"Después de la última línea se siguen escuchando {_fmt(span)} s de canto "
                           f"(«{heard}{'…' if len(tail) > 6 else ''}»)")

        if not reasons:
            continue
        if fix_end is not None:
            out.append({
                "kind": "timing", "line": i, "required": False,
                "key": occurrence_key("timing", segments, i),
                "title": "Timing", "why": ". ".join(reasons) + f". Alargar hasta {_fmt(fix_end)} s.",
                "sources": set(), "fix": {"type": "timing", "end": fix_end},
                "action": "Alargar", "dismiss": "Está bien así",
            })
        else:
            out.append({
                "kind": "timing", "line": i, "required": False, "listen": True,
                "key": occurrence_key("timing:inspect", segments, i),
                "title": "Revisar timing",
                "why": ". ".join(reasons) + ". Escuchá si hay que ajustar cuándo entra o sale.",
                "sources": {"witness"} if tail and i == last_index else set(),
                "fix": {"type": "inspect_structure"},
                "action": "Escuchar", "dismiss": "Está bien así",
            })
    return out


# ---------------------------------------------------------------------------
# Frontera de frase.
# ---------------------------------------------------------------------------

def _is_interjection(word: str) -> bool:
    low = word.casefold()
    folded = norm_token(low)
    # "ooh", "aaah", "uuuh": sólo vocales (y h) con una letra estirada.
    return low in _INTERJECTIONS or bool(_VOWEL_CRY.match(folded) and re.search(r"(.)\1", folded))


def phrase_items(segments: list[dict], *, lang: str | None,
                 orphan_words: Iterable[str] = ()) -> list[dict]:
    endings = {"es": _ENDING_ES, "pt": _ENDING_PT}.get(lang or "", _ENDING_ANY)
    orphans = {norm_token(w) for w in orphan_words}
    order = sorted(range(len(segments)), key=lambda k: _f(segments[k].get("start")))
    single = [i for i in order if len(_tokens(segments[i].get("text"))) == 1]
    single_count: dict[str, int] = {}
    for i in single:
        key = norm_token(_tokens(segments[i].get("text"))[0])
        single_count[key] = single_count.get(key, 0) + 1
    out = []
    for pos, i in enumerate(order):
        text = unicodedata.normalize("NFC", str(segments[i].get("text") or "")).strip()
        words = _tokens(text)
        if not words or text.startswith("("):
            continue  # ad-lib entre paréntesis
        # Un fragmento (demo, Short, Canvas) puede cortar una frase al
        # principio o al final del audio: esa frontera no la eligió nadie.
        if pos == len(order) - 1 or (pos == 0 and _f(segments[i].get("start")) <= EDGE_CUT_S):
            continue
        why = None
        if len(words) == 1:
            word = words[0]
            key = norm_token(word)
            duration = _f(segments[i].get("end")) - _f(segments[i].get("start"))
            if (key in orphans  # la regla de palabra huérfana ya lo cubre
                    or _is_interjection(word)
                    or single_count.get(key, 0) >= 2  # estribillo de una palabra
                    or duration >= HELD_WORD_S  # nota sostenida: va sola a propósito
                    or text.rstrip().endswith("!")):
                continue
            why = (f"«{word}» queda sola en pantalla. Escuchá si va con la línea "
                   "anterior o la siguiente.")
        else:
            if text.rstrip().endswith(("?", "!", "…", "...")):
                continue
            last = words[-1].casefold()
            if last not in endings:
                continue
            if last == words[-2].casefold():
                continue  # "la la la", "na na"
            if last == "se" and words[-2].casefold() in _SE_AS_KNOW:
                continue  # "no se" = "no sé" sin tilde
            why = (f"La línea termina en «{words[-1]}» y la frase sigue en la siguiente. "
                   "Escuchá dónde cortarla.")
        out.append({
            "kind": "phrase_structure", "line": i, "required": False, "listen": True,
            "key": occurrence_key("phrase_structure:boundary", segments, i),
            "title": "Palabra sola en pantalla" if len(words) == 1 else "Revisar división de frases",
            "why": why, "sources": set(), "fix": {"type": "inspect_structure"},
            "action": "Escuchar", "dismiss": "Está bien así",
        })
    return out


def boundary_items(segments: list[dict], *, witness: list[dict] | None = None,
                   machine_evidence: Any = None,
                   orphan_words: Iterable[str] = ()) -> list[dict]:
    """Puntos de timing y de frontera de frase, todos no obligatorios."""
    if not enabled() or not segments:
        return []
    return [
        *timing_items(segments, witness),
        *phrase_items(segments, lang=language(machine_evidence, segments),
                      orphan_words=orphan_words),
    ]
