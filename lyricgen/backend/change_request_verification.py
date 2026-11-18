"""Verificación punto por punto de un pedido de cambio antes de cerrarlo.

Por qué existe
--------------
De 310 pedidos de UMG (sep-nov 2026), 96 fueron segunda ronda o más sobre el
mismo video y 58 repetían un tiempo ya pedido: se corrigió a medias y el
pedido se cerró igual. Las protecciones anteriores no lo evitaban:

* el checklist de #1479 es manual: el operador tilda "revisado" sin que nada
  compare el punto contra la letra;
* el control de texto de #1511 (``change_request_publish_guard.text_gate``)
  acepta la frase pedida en CUALQUIER parte de la canción. En un estribillo
  ("2:39 Vení sumate…" y "2:49 Vení sumate…") basta con arreglar una
  aparición para que las dos "pasen";
* el cierre manual (Cambios y Campañas) y la revisión humana de lo publicado
  (#1520/#1571) cierran con un motivo libre, sin ninguna comprobación;
* los pedidos de timing, metadatos y puntuación nunca se comprobaban.

Qué hace
--------
``extract_points`` parte el comentario en puntos (uno por tiempo citado o por
instrucción) y ``verify`` compara CADA punto contra la letra que se publica:

* texto: la frase pedida tiene que estar en la letra en el tiempo citado
  (±``NEAR_SECONDS``). Las tildes que escribe el cliente cuentan; las que
  omite, no. Si la frase está sólo en otra parte de la canción es ✘;
* "cambiar X por Y" / "dice X": además, X no puede seguir en ese lugar;
* timing: "Desde X hasta Y" exige el inicio/fin pedidos; "entra tarde",
  "se va antes", "sincronizar"… exigen que la línea se haya movido hacia lo
  pedido respecto de la versión que vio el cliente;
* metadatos: artista/título iguales a lo pedido (con tildes);
* puntuación: sin ¿? / sin punto final donde se pidió.

Lo que no se puede comprobar con la letra (fondo, imagen, prosa ambigua)
queda ``manual``: el operador lo revisa en el video como hasta ahora. Un
punto ✘ bloquea el cierre salvo una justificación del operador para ESE
punto, que se guarda en la auditoría y nunca se le muestra al cliente.

La comparación es determinística y pura (sin I/O); al final del módulo están
los helpers que leen la letra aprobada de la base. Lo usan los endpoints de
cierre, el listado de Cambios, la bandeja de Campañas y el backtest
``scripts/backtest_change_request_verification.py``.

Backtest sobre el banco sep-nov 2026 (cerrando cada pedido contra la última
versión aprobada al momento del cierre): de 50 pedidos cuyo punto volvió a
reclamarse después de cerrado, 35 habrían tenido algún ✘ (29 en el mismo
punto reclamado); de 233 cerrados que no volvieron a reclamarse, 36 (15 %)
habrían pedido alguna justificación.
"""
from __future__ import annotations

import hashlib
import os
import re
import unicodedata
from typing import Any, Iterable

SCHEMA_VERSION = "change-request-verification-v1"
# Los clientes citan el segundo en que ESCUCHAN la frase (truncado) y a veces
# el inicio del verso anterior. En el banco sep-nov 2026, el 72 % de las citas
# de una frase que aparece una sola vez cayó a ±4 s de esa línea (73 % a ±5 s,
# 76 % a ±6 s): ensanchar casi no suma y arriesga tomar la repetición
# siguiente de un estribillo corto.
NEAR_SECONDS = 4.0
# Timing explícito "Desde X hasta Y": X e Y vienen al segundo.
START_TOLERANCE = 1.5
END_TOLERANCE = 2.0
# Por debajo de esto un borde "no se movió".
MOVED_EPSILON = 0.08
MAX_POINTS = 200
MAX_OVERRIDE_LENGTH = 1000

OK, FAIL, MANUAL = "ok", "fail", "manual"


def enabled() -> bool:
    """Prendido por defecto: es un control de cierre, no cambia la letra.

    ``CHANGE_REQUEST_CLOSE_VERIFICATION=0`` lo apaga (vuelve al cierre de
    antes) sin redeploy de código.
    """
    return os.environ.get("CHANGE_REQUEST_CLOSE_VERIFICATION", "1").strip().lower() not in {"0", "false", "no", "off"}


# --------------------------------------------------------------------------
# Normalización
# --------------------------------------------------------------------------

_APOSTROPHES = re.compile(r"['’‘´`ʼ]")


def strict_mask(text: str) -> list[bool]:
    """Qué palabras pidió el cliente EN MAYÚSCULAS: ahí la tilde cuenta en los
    dos sentidos ("2:41 TU casa vacía" no se cumple con "Tú"). Si todo el
    pedido está en mayúsculas no marca nada."""
    words = str(text or "").split()
    letters = [ch for ch in str(text or "") if ch.isalpha()]
    if letters and all(ch.isupper() for ch in letters):
        return [False] * len(tokens(text))
    mask: list[bool] = []
    for word in words:
        flag = len([ch for ch in word if ch.isalpha()]) >= 2 and all(ch.isupper() for ch in word if ch.isalpha())
        mask.extend([flag] * len(tokens(word)))
    return mask


def _strip_accents(text: str) -> str:
    text = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in text if not unicodedata.combining(ch))


def tokens(text: str) -> list[str]:
    """Palabras en minúscula CON tildes (NFC), sin puntuación."""
    text = _APOSTROPHES.sub("", str(text or ""))
    text = unicodedata.normalize("NFC", text.casefold())
    return re.findall(r"[^\W_]+", text)


def fold(text: str) -> str:
    return " ".join(_strip_accents(tok) for tok in tokens(text))


def _has_accent(token: str) -> bool:
    return _strip_accents(token) != token


def _token_match(wanted: str, have: str, strict: bool = False) -> bool:
    """La tilde cuenta sólo si el cliente la escribió.

    "cantás" pedido contra "cantas" en la letra es ✘ (pidió la tilde);
    "Nico Perez" pedido contra "García" en la letra es ✔ (el cliente
    omitió una tilde que la letra ya tiene)."""
    if wanted == have:
        return True
    if _strip_accents(wanted) != _strip_accents(have):
        return False
    return not strict and not _has_accent(wanted)


def _seconds(raw: str) -> float:
    parts = [int(v) for v in raw.split(":")]
    return float(parts[-1] + 60 * parts[-2] + (3600 * parts[0] if len(parts) == 3 else 0))


def clock(value: float | None) -> str:
    if value is None:
        return "?"
    value = max(0.0, float(value))
    return f"{int(value // 60)}:{value % 60:04.1f}"


# --------------------------------------------------------------------------
# Puntos del pedido
# --------------------------------------------------------------------------

_TIME = r"(?<![\d:])(?:\d{1,2}:)?\d{1,2}:[0-5]\d(?!\d)"
_TIME_RE = re.compile(_TIME)
_QUOTED_RE = re.compile(r'"([^"\n]+)"|“([^”\n]+)”|«([^»\n]+)»')
# Notas del cliente entre paréntesis ("(sacar el en mí)", "(revisar)"). Un
# paréntesis que no arranca con una instrucción es letra: "(eso, chico, eso)".
_PAREN_RE = re.compile(
    r"\((?:\s*(?:revisar|chequear|verificar|ver|ojo|nota|timing|sync|sincroniz\w*|que\s+termina|que\s+empieza|como|igual"
    r"|tal\s+como|sacar|quitar|eliminar|borrar|agregar|sin|no|falta|es|era|dice|se\s+repite)\b)[^)]*\)", re.I)
_INTERVAL = re.compile(
    r"^(?:(?:sincronizar|adelantar|atrasar|corregir|mejorar)(?:\s+(?:la|el)\s+(?:sincronizaci[oó]n|timing|frase|letra))*"
    r"(?:\s+(?:de\s+la\s+frase|con\s+la\s+voz|del\s+audio))*[\s,:]+)?(?:la\s+frase\s+)?"
    r"(?:desde\s+(?:el\s+)?(?P<a>" + _TIME + r")\s*(?:hasta|a)|(?P<a2>" + _TIME + r")\s*hasta)\s+(?:el\s+)?(?P<b>"
    + _TIME + r")\s*[:,.\-–—]?\s*(?P<text>.*)$", re.I | re.S)
_METADATA_LOOSE = re.compile(r'\b(?P<field>artista|t[ií]tulo)\b[^:"“]{0,60}[:]\s*(?P<value>[^:]+)$', re.I | re.S)
_METADATA = re.compile(
    r"^(?:solo\s+|s[oó]lo\s+)?(?:(?:corregir|cambiar)\s+(?:el\s+)?)?(?P<field>artista|t[ií]tulo|nombre\s+del\s+(?:tema|track))"
    r"\s*(?:del\s+tema|de\s+la\s+canci[oó]n)?\s*(?::|es|por|a|debe\s+ser|deber[ií]a\s+ser|deber[ií]a\s+decir)?\s*:?\s*"
    r"(?P<value>[\"“«].+|(?<=[:as])\s*.+)$",
    re.I | re.S)
_VISUAL = re.compile(
    r"\b(?:fondo|background|escena|imagen|im[aá]genes|animaci[oó]n|logo|armas?|banderas?|perrit[oa]s?|colores?|paleta"
    r"|iluminaci[oó]n|cuerpos?|portada|personas?|borde|bordes|marca|marcas|video|v[ií]deo|tipograf[ií]a|fuente|plano)\b",
    re.I)
_TIMING = re.compile(
    r"sincroniz|desincroniz|sincron[ií]a|timing|desfas|\bentr[ae]n?\b.{0,20}\b(?:tarde|antes|despu[eé]s)|\blleg[ae]n?\s+tarde"
    r"|\btermina|\bempieza|\bcomienza\s+(?:antes|tarde)|\barranc|m[aá]s\s+(?:de\s+)?tiempo|se\s+lea|alargar|extender|adelantar|atrasar"
    r"|se\s+van?\s+antes|desaparece|aparece\s+(?:antes|tarde|despu[eé]s)|que\s+aparezca|mantener|dure|queda\s+trabad|retras|demora"
    r"|dura\s+(?:muy\s+)?poco|m[aá]s\s+(?:de\s+)?duraci[oó]n|\bentra\s+en\b|empieza\s+a\s+cantar|coincid"
    r"|(?:comenzar|empezar|entrar|aparecer)\s+(?:un\s+poco\s+)?(?:antes|despu[eé]s|m[aá]s\s+tarde)"
    r"|que\s+(?:comience|empiece|entre)\s+en|\d+[,.]?\d*\s*seg",
    re.I)
_START_EARLIER = re.compile(
    r"\bentr[ae]n?\b.{0,20}\btarde|\blleg[ae]n?\s+tarde|aparece\s+tarde|aparece\s+despu[eé]s|que\s+aparezca\s+antes"
    r"|entre\s+(?:un\s+poco\s+)?antes|adelantar|empiece\s+antes|comience\s+antes|retras|demora|atrasad"
    r"|(?:comenzar|empezar|entrar|aparecer)\s+(?:un\s+poco\s+)?antes",
    re.I)
_START_LATER = re.compile(
    r"\bentr[ae]n?\b.{0,20}\bantes(?!\s+de)|aparece\s+antes(?!\s+de)|se\s+adelanta|atrasar|empieza\s+antes|comienza\s+antes"
    r"|m[aá]s\s+tarde|(?:comenzar|empezar|entrar|aparecer)\s+(?:un\s+poco\s+)?(?:despu[eé]s|m[aá]s\s+tarde)", re.I)
_EXPLICIT_START = re.compile(
    r"(?:que\s+(?:aparezca|entre|empiece|comience)\s+(?:en|a\s+partir\s+de|desde)|(?:adelantar|atrasar|mover)\w*\s+(?:\w+\s+){0,2}a)"
    r"\s+(?:el\s+)?(" + _TIME + r")", re.I)
_END_LATER = re.compile(
    r"se\s+van?\s+antes|termina(?:n)?\s+antes|desaparece|alargar|extender|m[aá]s\s+(?:de\s+)?tiempo|mantener|antes\s+de\s+que\s+(?:se\s+)?termin"
    r"|se\s+lea|dure\s+m[aá]s|se\s+corta|dura\s+(?:muy\s+)?poco|m[aá]s\s+(?:de\s+)?duraci[oó]n", re.I)
_END_EARLIER = re.compile(
    r"queda\s+trabad|se\s+queda|termina(?:n)?\s+tarde|dura\s+de\s+m[aá]s|se\s+mantiene\s+de\s+m[aá]s|dure\s+(?:un\s+poco\s+)?menos"
    r"|termine\s+antes", re.I)
_EXPLICIT_END = re.compile(r"(?:deber[ií]a\s+ser|hasta|que\s+dure\s+hasta|termin\w*\s+en)\s+(?:el\s+)?(" + _TIME + r")", re.I)
_QUESTION = re.compile(
    r"(?:sacar|sacarle|sacarles|quitar|quitarle|quitarles|eliminar|borrar|sin)\b.{0,25}\bsignos?\s+de\s+(?:pregunta|interrogaci[oó]n)",
    re.I)
_PERIOD = re.compile(
    r"(?:sacar|sacarle|sacarles|quitar|quitarle|quitarles|eliminar|borrar|sin)\b.{0,25}\bpuntos?(?:\s+final(?:es)?|\s+al\s+final|\s+de\s+(?:cada|las|todas))",
    re.I)
_ALL = re.compile(
    r"\b(?:todas?\s+las?\s+(?:frases|l[ií]neas|veces|apariciones|partes|repeticiones)|cada\s+(?:frase|l[ií]nea|vez)"
    r"|todo\s+el\s+(?:tema|video)|(?:en\s+)?el\s+resto\s+de(?:\s+la)?\s+canci[oó]n|todos\s+los\s+(?:coros|estribillos)"
    r"|siguiente\s+l[ií]nea|donde\s+(?:sea\s+que\s+)?aparezca)\b", re.I)
_REMOVE_VERB = r"(?:sacar|sacarle|quitar|quitarle|borrar|eliminar)"
_ARTICLES = r"(?:\s+(?:el|la|los|las|un|una|primer|primera|segundo|segunda|[uú]ltim[oa]|ese|esa|esos|esas|este|esta|palabra))*"
# Lo que dice HOY la letra, no lo pedido: 'dice "X"', 'cambiar "X" por …'.
_CURRENT_BEFORE = re.compile(
    r"(?:\bdice|\ben\s+(?:vez|lugar)\s+de|\bcambiar|\bno\s+es|\bno\s+dice|(?:^|[,;.\s])no"
    r"|\b" + _REMOVE_VERB + _ARTICLES + r"|\b" + _REMOVE_VERB + r"\b[^\"“«]{0,40}\b(?:a|en|de|del)" + _ARTICLES + r")\s*:?\s*$",
    re.I)
_CURRENT_AFTER = re.compile(r"^\s*(?:no\s+(?:lleva|va|es)|sobra|est[aá]\s+de\s+m[aá]s)\b", re.I)
_REMOVE_AFTER = re.compile(r"^\W{0,4}(?:" + _REMOVE_VERB + r")" + _ARTICLES + r"\s+[\"“«]?(?P<word>[^\W\d_]+)", re.I)
_PAIR = re.compile(
    r"(?:cambiar|reemplazar)\s+(?:(?:la\s+)?palabra\s+)?(?P<a>\"[^\"]+\"|“[^”]+”|'[^']+')\s+por\s+(?P<b>\"[^\"]+\"|“[^”]+”|'[^']+')"
    r"|donde\s+dice\s+(?P<c>\"[^\"]+\"|“[^”]+”|'[^']+')\s*,?\s*(?:debe|deber[ií]a)\s+(?:decir|ser)\s+(?P<d>\"[^\"]+\"|“[^”]+”|'[^']+')",
    re.I)
_WORD_ADD = re.compile(
    r"(?:agregar|poner|falta|corregir|con)\s+(?:la\s+|una\s+|el\s+)?(?:tilde|acento|letra\s+\w|\w)\s+(?:a|en)\s+(?:la\s+)?(?:palabra\s+)?"
    r"[\"“«]?(?P<word>(?!\d)[^\s\"”»,.;:]+)", re.I)
_ACCENT_REMOVE = re.compile(r"(?:quitar|sacar|sacarle|quitarle|eliminar|sin)\s+(?:la\s+|el\s+)?(?:tilde|acento)", re.I)
_PREFIXES = [re.compile(p, re.I) for p in (
    r"^faltaron\s+las\s+siguientes\s+correcciones",
    r"^(?:sincronizar|corregir)\s+(?:bien\s+|mejor\s+)?(?:la\s+)?(?:letras?|frases?|l[ií]neas?)(?:\s+en)?",
    r"^correcci[oó]n(?:es)?(?:\s+de\s+letra)?",
    r"^falta(?:n)?\s+(?:la\s+|una\s+|el\s+)?(?:frase|palabra|l[ií]nea|letra)s?",
    r"^no\s+aparece\s+(?:la\s+)?letra(?:\s+en\s+pantalla)?(?:\s+en)?",
    r"^a[uú]n\s+dice\s+\S+\s*,?",
    r"^(?:debe|deber[ií]a)\s+(?:decir|ser)",
    r"^(?:la\s+)?(?:l[ií]nea|frase|palabra)\s+(?:correcta\s+)?(?:es|dice)",
    r"^(?:añadir|agregar|sumar)\b",
    r"^es\s+",
    r"^(?:en|desde|hasta|a|y|de)\s*(?=§)",
    r"^§",
    r"^[,:;_\-–—.\s]+",
)]
_COURTESY = re.compile(r"[\s.!]*(?:gracias+|graciaas|porfa(?:vor)?|por\s+favor)[\s.!]*$", re.I)
_INSTRUCTION = re.compile(
    r"^(?:sacar|sacarle|quitar|cambiar|revisar|chequear|verificar|sincroniz\w*|acomodar|alargar|mover|unir|separar"
    r"|dividir|juntar|poner|dejar|mantener|corregir\s+(?:la\s+)?sincron\w*|el\s+estribillo|frase\s+de|que\s+(?:ac[aá]|no\s+aparezcan)"
    r"|se\s+sigue|ojo|nota|agregar|agregarle|eliminar|borrar|lo\s+mismo|igual\s+(?:que|en)|la\s+misma|el\s+mismo|punto\s+anterior)\b",
    re.I)
_STRUCTURE = re.compile(
    r"\b(?:unir|separar|dividir|juntar|misma\s+pantalla|una\s+sola\s+l[ií]nea|dos\s+l[ií]neas|cortar\s+la\s+l[ií]nea"
    r"|armar\s+mejor|ordenar\s+mejor|sugiero|por\s+separado|sola\s+en\s+(?:la\s+)?pantalla|tan\s+cortas|todos\s+juntos"
    r"|segunda\s+voz|en\s+adelante|no\s+se\s+ha\s+agregado)\b", re.I)
_TAIL_INSTRUCTION = re.compile(
    r"(?:\.\s+|\s+)(?=(?:y\s+)?(?:repetir|corregir|igual|lo\s+mismo|aparece|se\s+repite|ojo|nota)\b)", re.I)
_CAPS = re.compile(r"\b[A-ZÁÉÍÓÚÑÜ]{2,}(?:[\s,'-]+[A-ZÁÉÍÓÚÑÜ]{2,})*\b")


def _open_quote(text: str) -> bool:
    return text.count('"') % 2 == 1 or text.count("“") > text.count("”")


def _split_inline(line: str) -> list[str]:
    """Chile escribe varios puntos en un renglón: '0:35 es "X" 1:00 es "Y"'.

    Se corta antes de un tiempo sólo si lo precede un cierre de comillas, un
    punto, o ' - ': 'desde 2:50 hasta 2:53' y '1:34 y 3:03' no se tocan."""
    parts = re.split(r"(?<=[.\"”»])\s+(?=-?\s*" + _TIME + r")|\s+-\s+(?=" + _TIME + r")"
                     r"|(?<=\S)\s+(?=t[ií]tulo\s*:)", line)
    return [p for p in parts if p and p.strip(" -*•")]


def _chunks(comment: str) -> list[str]:
    out: list[str] = []
    for part in re.split(r"//", comment or ""):
        current: list[str] = []
        blank = False
        for raw in part.split("\n"):
            line = raw.strip()
            if not line:
                blank = True
                continue
            for number, piece in enumerate(_split_inline(line)):
                piece = piece.strip()
                timed = bool(re.match(r"(?:[-*•]\s*)?" + _TIME, piece))
                starts = (timed or piece.startswith(("-", "•", "*"))
                          or blank or bool(_INSTRUCTION.match(piece)) or bool(_METADATA.match(piece))
                          or bool(re.match(r"desde\s+" + _TIME, piece, re.I)))
                # Una cita sin cerrar no se come al punto siguiente del mismo
                # renglón ('es "TIENE… PIEL. - 1:24 dejar …').
                if current and starts and (not _open_quote(" ".join(current)) or (timed and number > 0)):
                    out.append(" ".join(current))
                    current = []
                current.append(piece)
                blank = False
        if current:
            out.append(" ".join(current))
    return [c for c in (c.strip(" -*•\t") for c in out) if c]


def _strip_prefixes(text: str) -> str:
    changed = True
    while changed and text:
        changed = False
        for pattern in _PREFIXES:
            new = pattern.sub("", text, count=1).lstrip()
            if new != text:
                text, changed = new, True
    return text


def _unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] in "\"“'«" and value[-1] in "\"”'»":
        value = value[1:-1]
    elif value[:1] in "\"“«":
        value = value[1:]
    return value.strip()


def _point_id(index: int, kind: str, excerpt: str, times: list) -> str:
    raw = f"{index}|{kind}|{excerpt}|{','.join('-' if t is None else f'{t:.0f}' for t in times)}"
    return "p-" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]


def _point(index: int, kind: str, excerpt: str, times: list, **extra) -> dict:
    return {"id": _point_id(index, kind, excerpt, times), "kind": kind, "excerpt": excerpt[:400],
            "times": times, "chunk": index, **extra}


def _text_after_time(chunk: str) -> str:
    body = _PAREN_RE.sub(" ", chunk)
    body = re.sub(r"^[-*•]\s*", "", body).strip()
    marked = _TIME_RE.sub(" § ", body)
    text = _strip_prefixes(marked.strip())
    if ":" in text:
        head, _, tail = text.rpartition(":")
        if re.search(r"(?:corregir|decir|ser|frase|letra|l[ií]neas?|correcciones|es|§)\s*$", head, re.I) or not tail.strip():
            text = _strip_prefixes(tail.strip()) if tail.strip() else ""
    return _COURTESY.sub("", text).strip(" .,;:")


def _without_tail(text: str) -> tuple[str, bool]:
    """'X. Repetir lo mismo en el resto' -> ('X', True)."""
    repeat = bool(_ALL.search(text))
    head = _TAIL_INSTRUCTION.split(text, maxsplit=1)[0]
    if repeat:
        head = _ALL.split(head)[0]
    return head.strip(" .,;:-"), repeat


def _metadata_value(field: str, raw: str) -> str:
    value = _COURTESY.sub("", raw).strip()
    quoted = _QUOTED_RE.search(value)
    if quoted:
        value = next(g for g in quoted.groups() if g is not None)
    return _unquote(value).strip(" .,;")


def extract_points(comment: str) -> list[dict]:
    """Un punto por instrucción y por tiempo citado.

    ``kind``: text, timing, metadata, punctuation, visual, manual. Un punto
    ``text`` trae ``expected`` (lo que debe decir) y opcional ``current`` (lo
    que no debe decir más); uno ``timing`` trae ``direction`` y opcional
    ``requested_start``/``requested_end``.
    """
    points: list[dict] = []
    for index, chunk in enumerate(_chunks(comment)):
        if len(points) >= MAX_POINTS:
            break
        times = [_seconds(t) for t in _TIME_RE.findall(chunk)]
        body = re.sub(r"^[-*•]\s*", "", _PAREN_RE.sub(" ", chunk)).strip()
        # "comienza en 0:46 y termina en 0:48": el segundo tiempo no es otra
        # aparición de la frase.
        ends = {_seconds(m.group(1)) for m in re.finditer(r"termin\w*\s+en\s+(?:el\s+)?(" + _TIME + r")", body, re.I)}
        cited_times = [t for t in times if t not in ends] or times

        metadata = _METADATA.match(body) or (None if times else _METADATA_LOOSE.search(body))
        if metadata:
            field = "artist" if metadata.group("field").lower().startswith("artista") else "song_title"
            value = _metadata_value(field, metadata.group("value"))
            if value and not _TIME_RE.search(value):
                points.append(_point(index, "metadata", chunk, [], field=field, expected=value))
                continue

        if body.count('"') % 2 == 1:
            # Comillas desparejas: no hay forma segura de separar la letra de
            # la explicación ('agregar tilde a "MÍO uno tiene… dice "MÍO MIO"').
            points.append(_point(index, "manual", chunk, times, reason="unbalanced_quotes"))
            continue

        interval = _INTERVAL.match(body)
        if interval:
            start = _seconds(interval.group("a") or interval.group("a2"))
            end = _seconds(interval.group("b"))
            raw_text = interval.group("text") or ""
            quoted_text = [next(g for g in m.groups() if g is not None) for m in _QUOTED_RE.finditer(raw_text)]
            text = _unquote(_strip_prefixes(_COURTESY.sub("", raw_text).strip(" .,;:")))
            if end > start and quoted_text and not _TIMING.search(_QUOTED_RE.sub(" ", raw_text)):
                for value in quoted_text:
                    points.append(_point(index, "text", chunk, [start, end], expected=value, current=None, scope="near"))
                continue
            if end > start and text and not _TIMING.search(text) and not _INSTRUCTION.match(text) and not _STRUCTURE.search(text):
                points.append(_point(index, "timing", chunk, [start], direction="interval",
                                     requested_start=start, requested_end=end, expected=text))
                continue
            if end > start:
                # "Mejorar la sincronización desde 0:00 hasta 0:20": un
                # tramo, no una línea; alcanza con que el tramo haya cambiado.
                points.append(_point(index, "timing", chunk, [start], direction="any", span=[start, end]))
                continue

        if _QUESTION.search(body):
            locator = next((next(g for g in m.groups() if g is not None) for m in _QUOTED_RE.finditer(body)), None)
            scope = "all" if _ALL.search(body) or not times else "near"
            for at in (times if scope == "near" else [None]):
                points.append(_point(index, "punctuation", chunk, [at] if at is not None else [], mark="question",
                                     scope=scope, locator=locator))
            continue
        if _PERIOD.search(body):
            scope = "all" if (not times or _ALL.search(body)) else "near"
            points.append(_point(index, "punctuation", chunk, times if scope == "near" else [], mark="period", scope=scope))
            continue
        if _STRUCTURE.search(body):
            points.append(_point(index, "manual", chunk, times, reason="structure_review"))
            continue

        marked = _TIME_RE.sub(" § ", body)
        literal_marker = re.search(
            r"(?:debe|deber[ií]a)\s+(?:decir|ser)(?!\s+(?:hasta|en|a)\b)|falta|la\s+(?:l[ií]nea|frase)\s+(?:correcta\s+)?es|corregir\s+letra|\bes\s+[\"“«]",
            marked, re.I)
        if _TIMING.search(_QUOTED_RE.sub(" ", marked)) and not literal_marker:
            direction = ("end_earlier" if _END_EARLIER.search(marked) else
                         "start_earlier" if _START_EARLIER.search(marked) else
                         "start_later" if _START_LATER.search(marked) else
                         "end_later" if _END_LATER.search(marked) else "any")
            explicit_end = _EXPLICIT_END.search(body)
            requested_end = _seconds(explicit_end.group(1)) if explicit_end and direction == "end_later" else None
            explicit_start = _EXPLICIT_START.search(body)
            requested_start = _seconds(explicit_start.group(1)) if explicit_start else None
            cited = [t for t in times if t not in (requested_end, requested_start)] or times
            if requested_start is not None and cited:
                direction = "start_later" if requested_start > cited[0] else "start_earlier"
            span = (min(times), max(times)) if len(times) >= 2 and re.search(r"desde.+hasta", body, re.I) else None
            if not cited:
                points.append(_point(index, "manual", chunk, [], reason="timing_without_time"))
                continue
            if span:
                points.append(_point(index, "timing", chunk, [span[0]], direction="any", span=list(span)))
                continue
            locator = next((next(g for g in m.groups() if g is not None) for m in _QUOTED_RE.finditer(body)), None)
            if locator is None:
                after_colon = re.search(_TIME + r"\s*:\s*([^:]+)$", body)
                tail = after_colon.group(1).strip(" .,") if after_colon else ""
                caps = _CAPS.search(_TIME_RE.sub(" ", body))
                locator = (tail if tail and not _TIMING.search(tail) and not _INSTRUCTION.match(tail) else None) or (
                    caps.group(0) if caps else None)
            for at in cited:
                points.append(_point(index, "timing", chunk, [at], direction=direction, expected=locator or None,
                                     **({"requested_end": requested_end} if requested_end is not None else {}),
                                     **({"requested_start": requested_start} if requested_start is not None else {})))
            continue

        pair = _PAIR.search(body)
        if pair:
            current = _unquote(pair.group("a") or pair.group("c"))
            expected = _unquote(pair.group("b") or pair.group("d"))
            repeat = bool(_ALL.search(body))
            scope = "all" if repeat or not times else "near"
            for at in times or [None]:
                points.append(_point(index, "text", chunk, [at] if at is not None else [], expected=expected,
                                     current=current, scope=scope, all_repetitions=repeat or not times))
            continue

        quoted, current = [], None
        for match in _QUOTED_RE.finditer(body):
            value = next(g for g in match.groups() if g is not None).strip()
            if len(fold(value).replace(" ", "")) <= 1:
                continue  # una letra ("agregar "N"") no es letra de la canción
            if _CURRENT_BEFORE.search(body[:match.start()]) or _CURRENT_AFTER.match(body[match.end():]):
                current = current or value
                continue
            removal = _REMOVE_AFTER.match(body[match.end():])
            if removal:
                # '"Remar ES..." borrar ese ES': la cita ubica, la palabra se va.
                drop = removal.group("word").casefold()
                kept = [w for w in value.split() if w.strip(".,…").casefold() != drop]
                value = " ".join(kept)
                current = current or removal.group("word")
            if fold(value):
                quoted.append(value)
        accent_strict = False
        if not quoted and current and _ACCENT_REMOVE.search(body):
            quoted, accent_strict = [_strip_accents(current)], True
            current = None
        if not quoted:
            word_add = _WORD_ADD.search(body)
            if word_add and times:
                quoted = [word_add.group("word")]
        if quoted:
            repeat = bool(_ALL.search(body))
            for order, value in enumerate(quoted):
                for at in cited_times or [None]:
                    points.append(_point(index, "text", chunk, [at] if at is not None else [], expected=value,
                                         current=current, scope="all" if at is None else "near",
                                         all_repetitions=repeat, accent_strict=accent_strict,
                                         window_extra=6.0 * order))
            continue
        if current and re.search(_REMOVE_VERB, body, re.I) and times:
            for at in cited_times:
                points.append(_point(index, "text", chunk, [at], expected=None, current=current, scope="near"))
            continue

        text = _text_after_time(chunk)
        if not text or "§" in text:
            points.append(_point(index, "manual", chunk, times, reason="unparsed_instruction"))
            continue
        if _VISUAL.search(text) and (not times or _INSTRUCTION.match(text)):
            points.append(_point(index, "visual", chunk, times))
            continue
        if _INSTRUCTION.match(text) or not times:
            points.append(_point(index, "manual", chunk, times, reason="free_prose"))
            continue
        text, repeat = _without_tail(text)
        if not fold(text):
            points.append(_point(index, "manual", chunk, times, reason="free_prose"))
            continue
        for at in cited_times:
            points.append(_point(index, "text", chunk, [at], expected=text, current=None, scope="near",
                                 all_repetitions=repeat))
    return points[:MAX_POINTS]


# --------------------------------------------------------------------------
# Comparación contra la letra
# --------------------------------------------------------------------------

def _clean_segments(segments: Iterable[dict] | None) -> list[dict]:
    rows = []
    for row in segments or []:
        if not isinstance(row, dict):
            continue
        try:
            start = float(row.get("start"))
            end = float(row.get("end"))
        except (TypeError, ValueError):
            continue
        rows.append({"start": start, "end": max(end, start), "text": str(row.get("text") or "")})
    rows.sort(key=lambda r: (r["start"], r["end"]))
    return rows


def _stream(segments: list[dict]) -> list[tuple[str, int]]:
    out = []
    for index, row in enumerate(segments):
        for token in tokens(row["text"]):
            out.append((token, index))
    return out


def _near(row: dict, times: list[float], window: float = NEAR_SECONDS) -> bool:
    return any(row["start"] - window <= t <= row["end"] + window for t in times)


def _find(segments: list[dict], wanted: list[str], times: list[float] | None, *, strict: bool | list[bool] = False,
          extra: float = 0.0) -> tuple[list[list[int]], list[list[int]]]:
    """Apariciones (índices de línea) de la frase: (cerca del tiempo, en otra parte).

    ``extra`` agranda la ventana hacia adelante: con varias citas seguidas
    bajo un solo tiempo ('3:50 "Yeah…" "Ia ia ia oh" "Tra la la"'), la
    tercera se canta segundos después del tiempo citado."""
    stream = _stream(segments)
    near, far = [], []
    if not wanted:
        return near, far
    n = len(wanted)
    flags = strict if isinstance(strict, list) and len(strict) == n else [bool(strict)] * n
    for at in range(len(stream) - n + 1):
        if all(_token_match(wanted[k], stream[at + k][0], flags[k]) for k in range(n)):
            lines = sorted({stream[at + k][1] for k in range(n)})
            if not times or any(_near(segments[i], times) or (
                    extra and any(t <= segments[i]["start"] <= t + NEAR_SECONDS + extra for t in times)) for i in lines):
                near.append(lines)
            else:
                far.append(lines)
    return near, far


def _lines_at(segments: list[dict], at: float, window: float = NEAR_SECONDS) -> list[int]:
    hits = [i for i, row in enumerate(segments) if row["start"] - 0.25 <= at <= row["end"] + 0.25]
    if hits:
        return hits
    near = sorted(((min(abs(row["start"] - at), abs(row["end"] - at)), i) for i, row in enumerate(segments)))
    return [i for d, i in near[:1] if d <= window]


def _brief(segments: list[dict], indexes: Iterable[int]) -> dict | None:
    indexes = sorted(set(indexes))
    if not indexes:
        return None
    rows = [segments[i] for i in indexes]
    return {"start": rows[0]["start"], "end": rows[-1]["end"], "text": " / ".join(r["text"] for r in rows)}


def _best_line_text(segments: list[dict], times: list[float]) -> dict | None:
    if not times:
        return None
    for at in times:
        lines = _lines_at(segments, at)
        if lines:
            return _brief(segments, lines[:1])
    return None


def pending_repetitions(before: list[dict] | None, after: list[dict], times: list[float]) -> list[dict]:
    """Otras apariciones de la MISMA línea original que siguen sin tocar.

    La línea citada se busca en la versión que vio el cliente; sus
    "hermanas" son las líneas con el mismo texto (sin tildes ni signos). Si
    la citada cambió y una hermana sigue igual que antes, quedó pendiente.
    """
    if not before or not times:
        return []
    out = []
    for at in times:
        targets = _lines_at(before, at, 1.0)
        if len(targets) != 1:
            continue
        original = fold(before[targets[0]]["text"])
        if len(original.split()) < 2:
            continue
        target_after = _lines_at(after, (before[targets[0]]["start"] + before[targets[0]]["end"]) / 2, 0.5)
        if not target_after or any(fold(after[i]["text"]) == original for i in target_after):
            continue
        for index, row in enumerate(before):
            if index == targets[0] or fold(row["text"]) != original:
                continue
            same = _lines_at(after, (row["start"] + row["end"]) / 2, 0.5)
            if same and any(fold(after[i]["text"]) == original for i in same):
                out.append(_brief(after, same[:1]))
    return out


def _distance(segments: list[dict], lines: list[int], times: list[float]) -> float:
    if not times:
        return 0.0
    return min(abs((segments[i]["start"] + segments[i]["end"]) / 2 - t) for i in lines for t in times)


def _check_text(point: dict, after: list[dict], before: list[dict] | None = None,
                taken: dict | None = None) -> dict:
    wanted = tokens(point.get("expected") or "")
    times = [t for t in point.get("times") or [] if t is not None]
    current = tokens(point.get("current") or "")
    if not wanted and current and times:
        # Sólo sacar algo ('eliminar esos "AMEN"'): no tiene que quedar ahí.
        still, _ = _find(after, current, times, strict=True)
        if still:
            return {"status": FAIL, "reason": "current_text_still_present", "line": _brief(after, still[0])}
        return {"status": OK, "reason": "removed", "line": _best_line_text(after, times)}
    if not wanted:
        return {"status": MANUAL, "reason": "empty_expected"}
    strict = [True] * len(wanted) if point.get("accent_strict") else strict_mask(point.get("expected") or "")
    near, far = _find(after, wanted, times if point.get("scope") != "all" else None,
                      strict=strict, extra=float(point.get("window_extra") or 0.0))
    if near and times and taken is not None:
        # "0:16 y 0:20 A esperarte en la esquina": dos tiempos, dos apariciones.
        # La misma línea corregida no puede responder por las dos.
        key = (point.get("chunk"), fold(point.get("expected") or ""))
        used = taken.setdefault(key, set())
        near.sort(key=lambda lines: _distance(after, lines, times))
        free = [lines for lines in near if lines[0] not in used]
        if not free:
            return {"status": FAIL, "reason": "same_occurrence_as_other_time", "line": _best_line_text(after, times)}
        near = free
        used.add(near[0][0])
    if near:
        line = _brief(after, near[0])
        if current and not _contains(current, wanted) and (point.get("all_repetitions") or point.get("scope") == "all"):
            # 'cambiar "X" por "Y" en todas las apariciones' (o sin tiempo):
            # ninguna aparición de X puede quedar, no sólo la primera.
            remaining, _ = _find(after, current, None, strict=True)
            if remaining:
                return {"status": FAIL, "reason": "repetitions_not_fixed", "line": line,
                        "repetitions_pending": [_brief(after, lines) for lines in remaining[:5]]}
        if current and not _contains(current, wanted):
            # Lo que había que sacar no puede seguir en la línea corregida.
            matched = tokens(" ".join(after[i]["text"] for i in near[0]))
            if _contains(matched, current):
                return {"status": FAIL, "reason": "current_text_still_present", "line": line}
        pending = pending_repetitions(before, after, times)
        if pending and point.get("all_repetitions"):
            return {"status": FAIL, "reason": "repetitions_not_fixed", "line": line, "repetitions_pending": pending}
        return {"status": OK, "reason": "found_near" if times else "found", "line": line,
                **({"repetitions_pending": pending} if pending else {})}
    line = _best_line_text(after, times)
    if far:
        return {"status": FAIL, "reason": "found_only_elsewhere", "line": line,
                "elsewhere": _brief(after, far[0])}
    return {"status": FAIL, "reason": "expected_text_missing", "line": line}


def _contains(haystack: list[str], needle: list[str]) -> bool:
    n = len(needle)
    return any(haystack[i:i + n] == needle for i in range(len(haystack) - n + 1)) if n else False


def _check_punctuation(point: dict, after: list[dict]) -> dict:
    times = [t for t in point.get("times") or [] if t is not None]
    rows = [i for i, row in enumerate(after) if not times or point.get("scope") == "all" or _near(row, times, 2.0)]
    locator = fold(point.get("locator") or "")
    if locator:
        # '… "tan bien". Y repetir en cada vez': sólo las líneas citadas.
        rows = [i for i in rows if locator in fold(after[i]["text"])] or (rows if times else [])
    if point.get("mark") == "question":
        def asks(text: str) -> bool:
            if not locator:
                return bool(re.search(r"[¿?]", text))
            return any(locator in fold(piece) and re.search(r"[¿?]", piece) for piece in re.split(r"[\n()]", text))
        bad = [i for i in rows if asks(after[i]["text"])]
        reason = "question_marks_remain"
    else:
        bad = [i for i in rows if re.search(r"(?<!\.)\.[\"'”’)]*\s*$", after[i]["text"])]
        reason = "terminal_period_remains"
    if bad:
        return {"status": FAIL, "reason": reason, "line": _brief(after, bad[:1]), "count": len(bad)}
    return {"status": OK, "reason": "punctuation_removed", "line": _brief(after, rows[:1]) if rows else None}


def _locate_for_timing(segments: list[dict], point: dict) -> list[int]:
    times = [t for t in point.get("times") or [] if t is not None]
    expected = tokens(point.get("expected") or "")
    if expected:
        window = list(times) + ([point["requested_end"]] if point.get("requested_end") is not None else [])
        near, _ = _find(segments, expected, window or None)
        if near:
            anchor = point.get("requested_start") if point.get("direction") == "interval" else None
            anchor = times[0] if anchor is None and times else anchor
            if anchor is not None:
                near.sort(key=lambda lines: abs(segments[lines[0]]["start"] - anchor))
            return near[0]
    if point.get("span"):
        a, b = point["span"]
        return [i for i, row in enumerate(segments) if row["end"] > a - 0.5 and row["start"] < b + 0.5]
    return _lines_at(segments, times[0]) if times else []


def _check_timing(point: dict, after: list[dict], before: list[dict] | None) -> dict:
    if point.get("direction") == "interval" and tokens(point.get("expected") or ""):
        # "Desde 0:10 hasta 0:13: Quiero soñar contigo": la letra citada es
        # parte del pedido. Si en ese tramo sigue la letra vieja, que los
        # tiempos coincidan no alcanza (antes caía en la línea de ese momento
        # y daba OK con el texto sin corregir).
        times = [t for t in point.get("times") or [] if t is not None]
        window = times + ([point["requested_end"]] if point.get("requested_end") is not None else [])
        near, far = _find(after, tokens(point["expected"]), window or None)
        if not near:
            at = _lines_at(after, times[0]) if times else []
            return {"status": FAIL, "reason": "found_only_elsewhere" if far else "expected_text_missing",
                    "line": _brief(after, at) if at else None,
                    **({"elsewhere": _brief(after, far[0])} if far else {})}
    lines = _locate_for_timing(after, point)
    if not lines:
        return {"status": FAIL, "reason": "timing_line_not_found"}
    line = _brief(after, lines)
    old_lines = _locate_for_timing(before, point) if before else []
    old = _brief(before, old_lines) if old_lines else None
    if point.get("span"):
        if old is None:
            return {"status": MANUAL, "reason": "timing_without_previous_version", "line": line}
        a, b = point["span"]
        new_rows = [(round(r["start"], 2), round(r["end"], 2)) for r in after if r["end"] > a - 0.5 and r["start"] < b + 0.5]
        old_rows = [(round(r["start"], 2), round(r["end"], 2)) for r in before if r["end"] > a - 0.5 and r["start"] < b + 0.5]
        if new_rows == old_rows:
            return {"status": FAIL, "reason": "timing_unchanged", "line": line, "previous": old}
        return {"status": OK, "reason": "timing_changed", "line": line, "previous": old}
    unchanged = (old is not None and abs(old["start"] - line["start"]) < MOVED_EPSILON
                 and abs(old["end"] - line["end"]) < MOVED_EPSILON)
    if point.get("direction") == "interval":
        start_ok = abs(line["start"] - point["requested_start"]) <= START_TOLERANCE
        end_ok = point.get("requested_end") is None or abs(line["end"] - point["requested_end"]) <= END_TOLERANCE
        if start_ok and end_ok:
            # Argentina escribe TODOS los puntos como "Desde X hasta Y: letra",
            # también los de texto: si la línea ya está donde dice, se cumple.
            return {"status": OK, "reason": "interval_matches", "line": line, "previous": old}
        return {"status": FAIL, "reason": "interval_mismatch", "line": line, "previous": old,
                "requested_start": point["requested_start"], "requested_end": point.get("requested_end")}
    if point.get("requested_start") is not None:
        ok = abs(line["start"] - point["requested_start"]) <= START_TOLERANCE
        return {"status": OK if ok else FAIL, "reason": "start_matches_request" if ok else "start_differs_from_request",
                "line": line, "previous": old}
    if old is None:
        return {"status": MANUAL, "reason": "timing_without_previous_version", "line": line}
    if unchanged:
        return {"status": FAIL, "reason": "timing_unchanged", "line": line, "previous": old}
    direction = point.get("direction")
    moved = {
        "start_earlier": line["start"] < old["start"] - MOVED_EPSILON,
        "start_later": line["start"] > old["start"] + MOVED_EPSILON,
        "end_later": line["end"] > old["end"] + MOVED_EPSILON,
        "end_earlier": line["end"] < old["end"] - MOVED_EPSILON,
    }.get(direction, True)
    if point.get("requested_end") is not None and line["end"] < point["requested_end"] - END_TOLERANCE:
        return {"status": FAIL, "reason": "end_short_of_request", "line": line, "previous": old}
    if not moved:
        return {"status": FAIL, "reason": "timing_moved_wrong_way", "line": line, "previous": old}
    return {"status": OK, "reason": "timing_moved", "line": line, "previous": old}


def _metadata_key(value: str) -> list[str]:
    return tokens(value)


def _check_metadata(point: dict, metadata: dict | None) -> dict:
    if not metadata or point.get("field") not in metadata:
        return {"status": MANUAL, "reason": "metadata_unavailable"}
    have = _metadata_key(metadata.get(point["field"]) or "")
    candidates = [point.get("expected") or ""]
    if " - " in candidates[0]:
        # 'cambiar el título "Saiko - Cuando Llegue El Tren"': artista - título.
        candidates.append(candidates[0].split(" - ", 1)[1])
    ok = False
    for candidate in candidates:
        wanted = _metadata_key(candidate)
        ok = ok or (len(have) == len(wanted) and all(_token_match(w, h) for w, h in zip(wanted, have)))
    return {"status": OK if ok else FAIL, "reason": "metadata_matches" if ok else "metadata_differs",
            "current_value": metadata.get(point["field"])}


def verify(comment: str, after_segments: Iterable[dict] | None, *, before_segments: Iterable[dict] | None = None,
           metadata: dict | None = None, source: str | None = None, revision: int | None = None) -> dict[str, Any]:
    """Compara cada punto del pedido contra ``after_segments`` (la letra que
    se publica o publicó). ``before_segments`` es la letra que vio el cliente
    al pedir; sin ella el timing en prosa queda manual."""
    after = _clean_segments(after_segments)
    before = _clean_segments(before_segments) if before_segments is not None else None
    results = []
    taken: dict = {}
    for point in extract_points(comment):
        kind = point["kind"]
        if kind == "text":
            check = _check_text(point, after, before, taken) if after else {"status": MANUAL, "reason": "lyrics_unavailable"}
        elif kind == "timing":
            check = _check_timing(point, after, before) if after else {"status": MANUAL, "reason": "lyrics_unavailable"}
        elif kind == "punctuation":
            check = _check_punctuation(point, after) if after else {"status": MANUAL, "reason": "lyrics_unavailable"}
        elif kind == "metadata":
            check = _check_metadata(point, metadata)
        else:
            check = {"status": MANUAL, "reason": point.get("reason") or kind}
        results.append({**point, **check})
    counts = {OK: 0, FAIL: 0, MANUAL: 0}
    for row in results:
        counts[row["status"]] += 1
    return {"schema_version": SCHEMA_VERSION, "points": results, "counts": counts,
            "blocking": counts[FAIL] > 0, "source": source, "revision": revision}


# --------------------------------------------------------------------------
# Justificaciones y reincidencias
# --------------------------------------------------------------------------

def failing_ids(result: dict | None) -> list[str]:
    return [p["id"] for p in (result or {}).get("points") or [] if p.get("status") == FAIL]


def missing_overrides(result: dict | None, overrides: dict[str, str] | None) -> list[str]:
    """Puntos ✘ sin justificación. Una justificación vacía no cuenta."""
    overrides = overrides or {}
    return [pid for pid in failing_ids(result) if not str(overrides.get(pid) or "").strip()]


def clean_overrides(result: dict | None, overrides: dict[str, str] | None) -> dict[str, str]:
    """Sólo las justificaciones de puntos que hoy fallan, recortadas."""
    wanted = set(failing_ids(result))
    return {pid: str(text).strip()[:MAX_OVERRIDE_LENGTH] for pid, text in (overrides or {}).items()
            if pid in wanted and str(text or "").strip()}


def audit_summary(result: dict | None, overrides: dict[str, str] | None = None) -> dict:
    """Lo que se guarda en la auditoría local (nunca en el portal)."""
    result = result or {}
    return {
        "schema_version": result.get("schema_version"), "source": result.get("source"),
        "revision": result.get("revision"), "counts": result.get("counts"),
        "points": [{"id": p["id"], "kind": p["kind"], "status": p["status"], "reason": p.get("reason"),
                    "times": p.get("times"), "excerpt": (p.get("excerpt") or "")[:160],
                    **({"override": overrides[p["id"]]} if overrides and p["id"] in overrides else {})}
                   for p in result.get("points") or []][:MAX_POINTS],
    }


def blocked_detail(result: dict, *, message: str | None = None) -> dict:
    failing = [p for p in result.get("points") or [] if p.get("status") == FAIL]
    return {
        "code": "change_request_points_unverified",
        "message": message or (
            f"{len(failing)} punto(s) del pedido no están en la letra que se publica. Corregilos, "
            "o justificá cada uno (la justificación queda interna, el cliente no la ve)."),
        "verification": result,
        "failing": [p["id"] for p in failing],
    }


def recurrences(points: list[dict], previous: list[dict], *, window: float = 3.0) -> list[dict]:
    """Puntos de un pedido nuevo que repiten un punto de un pedido anterior
    YA CERRADO del mismo video: mismo tiempo (±``window``) o mismo texto.

    ``previous``: [{"id", "resolved_at", "points": [...]}]. Sirve para medir
    cuántas veces se cerró algo incompleto; no bloquea nada.
    """
    out = []
    for point in points:
        if point.get("kind") in {"visual", "manual"} and not point.get("times"):
            continue
        times = [t for t in point.get("times") or [] if t is not None]
        wanted = fold(point.get("expected") or "")
        for request in previous:
            match = None
            for old in request.get("points") or []:
                old_times = [t for t in old.get("times") or [] if t is not None]
                if times and old_times and any(abs(a - b) <= window for a in times for b in old_times):
                    match = old
                    break
                if wanted and len(wanted) >= 6 and wanted == fold(old.get("expected") or ""):
                    match = old
                    break
            if match is not None:
                out.append({"point_id": point["id"], "request_id": request.get("id"),
                            "resolved_at": request.get("resolved_at"), "excerpt": (match.get("excerpt") or "")[:160]})
                break
    return out


# --------------------------------------------------------------------------
# Contra la base: qué letra se publica y qué letra vio el cliente
# --------------------------------------------------------------------------

POINTS_UNVERIFIED = "points_unverified"
UNVERIFIED_ACTION = "delivery.change_request.points_unverified"


def lyrics_for(db, job, *, at=None, fallback: bool = True) -> tuple[list | None, str | None, int | None]:
    """La letra aprobada vigente en ``at`` (la que se publica o publicó).

    Sin versión aprobada del editor se usa la del job (lo que se renderizó)
    sólo si ``fallback``; para "lo que vio el cliente" no hay fallback: sin
    versión anterior el timing en prosa queda manual en vez de "sin cambios".
    """
    from change_request_publish_guard import published_version
    version = published_version(db, job.job_id, at)
    if version is not None:
        return list(version.segments or []), "editor_version", int(version.revision or 0)
    if fallback and job.segments_json:
        return list(job.segments_json), "job_segments", None
    return None, None, None


def verify_request(db, job, request, *, at=None) -> dict | None:
    """Verificación de UN pedido contra la letra aprobada del ``job``."""
    if job is None or request is None:
        return None
    after, source, revision = lyrics_for(db, job, at=at)
    before = None
    if request.submitted_at is not None:
        before, _, _ = lyrics_for(db, job, at=request.submitted_at, fallback=False)
    return verify(request.comment or "", after, before_segments=before,
                  metadata={"artist": job.artist, "song_title": job.song_title},
                  source=source, revision=revision)


def parse_overrides(raw) -> dict[str, str]:
    """``{point_id: justificación}`` desde un body JSON; ignora basura."""
    if not isinstance(raw, dict):
        return {}
    return {key[:40]: value[:MAX_OVERRIDE_LENGTH] for key, value in list(raw.items())[:MAX_POINTS]
            if isinstance(key, str) and isinstance(value, str) and value.strip()}


def kept_entry(request, result: dict) -> dict:
    """Un pedido que la publicación deja abierto: tiene puntos ✘ sin justificar."""
    failing = [p for p in result.get("points") or [] if p.get("status") == FAIL]
    return {"id": request.id, "reason": POINTS_UNVERIFIED,
            "missing": [p.get("excerpt") or "" for p in failing][:20],
            "failing": [p["id"] for p in failing], "editor_revision": result.get("revision")}


def kept_audit(user_id, job, kept: dict, source: str, **extra):
    """Auditoría local de un pedido que quedó abierto al publicar."""
    if kept.get("reason") != POINTS_UNVERIFIED:
        import change_request_publish_guard as guard
        return guard.text_not_found_audit(user_id, job, kept, source, **extra)
    from database import AuditLog
    return AuditLog(user_id=user_id, action=UNVERIFIED_ACTION, detail={
        "job_id": job.job_id, "change_request_id": kept["id"], "reason": POINTS_UNVERIFIED,
        "missing": kept.get("missing", [])[:20], "failing": kept.get("failing", [])[:50],
        "editor_revision": kept.get("editor_revision"), "source": source, **extra,
    })


def recurrences_by_request(rows: list) -> dict[int, list[dict]]:
    """Reincidencias de cada pedido contra los pedidos YA CERRADOS de la misma
    entrega antes de que llegara. ``rows``: todos los pedidos de las entregas
    en juego (``id``, ``delivery_id``, ``comment``, ``submitted_at``,
    ``resolved_at``). Sin I/O."""
    from change_request_workflow import timestamp
    by_delivery: dict = {}
    for request in rows:
        by_delivery.setdefault(request.delivery_id, []).append(request)
    cache: dict = {}

    def points_of(request):
        if request.id not in cache:
            try:
                cache[request.id] = extract_points(request.comment or "")
            except Exception:
                cache[request.id] = []
        return cache[request.id]

    out: dict[int, list[dict]] = {}
    for group in by_delivery.values():
        for request in group:
            submitted = timestamp(request.submitted_at)
            if submitted is None:
                continue
            previous = []
            for other in group:
                closed = timestamp(other.resolved_at)
                if other.id != request.id and closed is not None and closed <= submitted:
                    previous.append({"id": other.id, "resolved_at": closed.isoformat(), "points": points_of(other)})
            if previous:
                previous.sort(key=lambda row: row["resolved_at"], reverse=True)
                hits = recurrences(points_of(request), previous)
                if hits:
                    out[request.id] = hits
    return out
