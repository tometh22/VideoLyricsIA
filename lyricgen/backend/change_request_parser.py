"""Conservative source-preserving grammar; unsupported instructions abstain."""
from __future__ import annotations

import re
import unicodedata

SCHEMA_VERSION = "change-request-parser-v6"
MAX_COMMENT_LENGTH = 24000
MAX_INSTRUCTIONS = 400
MAX_LITERAL_LENGTH = 500
_TIME = r"(?:\d{1,2}:)?\d{1,2}:\d{2}"
_TIME_RE = re.compile(_TIME)
_TIME_PREFIX = re.compile(r"^(?P<times>" + _TIME + r"(?:\s*(?:y|/|,)\s*" + _TIME + r")*)\s*[:_\-–—]?\s*(?P<payload>.*)$", re.S)
_REPEAT = re.compile(r"\b(?:todas?\s+las?\s+(?:apariciones|veces|frases)|cada\s+vez|en\s+todo\s+el\s+tema|todos?\s+los?\s+(?:coros?|estribillos?))\b", re.I)
_LAYOUT = re.compile(r"frases?\s+completas?.{0,60}(?:una|1|misma)\s+(?:sola\s+)?pantalla", re.I | re.S)
_TIMING = re.compile(r"sincroniz|timing|desfas|entra\s+(?:tarde|antes)|termina\s+(?:tarde|antes)|mantener.+(?:tiempo|más)|empieza\s+a\s+cantar|mover\s+un", re.I)
_STRUCTURE = re.compile(r"\b(?:unir|separar|dividir|juntar|frase\s+completa|misma\s+pantalla|una\s+sola\s+l[ií]nea|dos\s+l[ií]neas)\b", re.I)
_VISUAL = re.compile(r"\b(?:fondo|background|prompt|escena|imagen|animaci[oó]n|personas?|logo|armas?|banderas?|perrit[oa]|perr[oa]|colores?|paleta|iluminaci[oó]n|identidad|dos\s+cuerpos|pie\s+que)\b", re.I)
_VERBS = r"(?:cambiar|quitar|sacar|eliminar|corregir|reemplazar|modificar|borrar|mover|alterar|tocar|traducir|publicar|regenerar|cortar|mostrar)"
_ACTION = re.compile(r"^(?:revisar|chequear|verificar|" + _VERBS + r"|unir|separar|dividir|juntar|mantener|dejar|poner|hacer|audio|fondo|repetir|insertar|agregar|no\s+" + _VERBS + r"|en\s+todas?)\b", re.I)
_UNSAFE = re.compile(r"\b(?:no\s+" + _VERBS + r"|quiz[aá]s|tal\s+vez|podr[ií]a|podr[ií]amos|la\s+misma\s+frase|punto\s+anterior|repetir|insertar|agregar)\b", re.I)
_PERIOD = re.compile(r"^(?:sacar|quitar|eliminar)\s+(?:el\s+|los\s+)?puntos?(?:\s+final(?:es)?)?(?:\s+de\s+todas\s+las\s+frases)?$", re.I)
_NOTE = re.compile(r"\s*(\((?:revisar|chequear|verificar|timing|sync|sincroniz|que\s+termina|que\s+empieza)[^)]*\))\s*$", re.I)
_LITERAL = r'''(?:"[^"\n]+"|“[^”\n]+”|'[^'\n]+'|‘[^’\n]+’)'''
_PAIR = re.compile(r"^(?:(?:en\s+)?(?:todas?\s+las?\s+(?:apariciones|veces)),?\s*)?(?:cambiar\s+(?P<a>" + _LITERAL + r")\s+por\s+(?P<b>" + _LITERAL + r")|donde\s+dice\s+(?P<c>" + _LITERAL + r")\s+(?:debe|deber[ií]a)\s+decir\s+(?P<d>" + _LITERAL + r"))(?P<scope>\s+(?:en\s+)?todas?\s+las?\s+(?:apariciones|veces))?\s*$", re.I)
_SAY = re.compile(r"^(?:debe|deber[ií]a)\s+decir\s*[:\-]?\s*(.+)$", re.I | re.S)
_SAY_QUOTED = re.compile(r"^(?:deber[ií]a\s+ser|la\s+frase\s+(?:es|debe\s+ser)|es)\s+(" + _LITERAL + r")\s*$", re.I)
_DICE_PAIR = re.compile(r"^dice\s+(?P<a>" + _LITERAL + r")\s*,?\s*deber[ií]a\s+ser\s+(?P<b>" + _LITERAL + r")\s*$", re.I)


def fold_text(value: str) -> str:
    """Search/ranking only; not identity for mutation."""
    value = unicodedata.normalize("NFKD", (value or "").casefold())
    return " ".join(re.findall(r"[\w]+", "".join(ch for ch in value if not unicodedata.combining(ch))))


def _literal(value: str) -> str | None:
    value = value.strip()
    pairs = {'"': '"', '“': '”', "'": "'", '‘': '’'}
    if value and value[0] in pairs:
        if len(value) < 3 or value[-1] != pairs[value[0]]:
            return None
        value = value[1:-1]
    if '"' in value or '“' in value or '”' in value:
        return None
    if any(unicodedata.category(ch) in {"Cf", "Cc"} and ch not in "\n\t" for ch in value):
        return None
    return value if value and len(value) <= MAX_LITERAL_LENGTH else None


def _quoted_literal(value: str) -> str | None:
    """Only an explicit whole quoted span authorizes literal lyric content.

    A timestamp or 'debe decir' does not delimit the end of an unquoted lyric
    versus an editorial qualifier. Unknown prose must remain review-only;
    extending verb/negation blacklists cannot prove its intended meaning.
    """
    value = value.strip()
    return _literal(value) if re.fullmatch(_LITERAL, value) else None


def _positive_layout(value: str) -> bool:
    value = re.sub(r"^[-*•]\s+", "", value.strip()).rstrip(".")
    return bool(re.fullmatch(
        r"(?:revisar(?:\s+que)?\s+)?(?:las\s+)?frases?\s+completas?"
        r"(?:\s+est[eé]n)?\s+en\s+(?:(?:1|una)\s+sola|(?:la\s+)?misma)\s+pantalla",
        value, re.I))


def _blocks(comment: str):
    """Preserve physical continuations and every non-whitespace source block."""
    start = None
    end = 0
    for match in re.finditer(r"[^\n]*(?:\n|$)", comment):
        raw = match.group()
        if not raw:
            continue
        line = raw.strip()
        boundary = not line or bool(re.match(r"(?:[-−*•]\s*)?\d{1,2}:", line)) or bool(_ACTION.match(line)) or bool(re.match(r"(?:no|si|conservar|preservar)\b", line, re.I)) or _positive_layout(line) or line.startswith(("-", "•", "* "))
        if start is not None and boundary:
            yield start, end, comment[start:end]
            start = None
        if line:
            if start is None:
                start = match.start() + len(raw) - len(raw.lstrip())
            end = match.start() + len(raw.rstrip())
    if start is not None:
        yield start, end, comment[start:end]


def parse_change_request(comment: str) -> dict:
    comment = comment or ""
    if len(comment) > MAX_COMMENT_LENGTH:
        raise ValueError("change_request_comment_too_long")
    rows: list[dict] = []
    coverage: list[dict] = []
    blocks = list(_blocks(comment))
    if len(blocks) > MAX_INSTRUCTIONS:
        raise ValueError("change_request_too_many_instructions")
    layout = any(_positive_layout(text) for _, _, text in blocks)

    def add(kind, start, end, *, reason=None, requested=None, current=None, times=None, scope="single", confidence="low"):
        if len(rows) >= MAX_INSTRUCTIONS:
            raise ValueError("change_request_too_many_instructions")
        row = {"id": f"instruction-{len(rows)+1}", "kind": kind,
               "source_excerpt": comment[start:end], "source_start": start, "source_end": end,
               "requested_text": requested, "current_text": current,
               "timecode_seconds": times, "scope": scope, "confidence": confidence, "reason": reason}
        rows.append(row)
        coverage.append({"start": start, "end": end, "instruction_id": row["id"],
                         "status": "manual" if kind.endswith("review") else "recognized"})

    for start, end, text in blocks:
        payload = text.strip()
        # A spaced list bullet is syntax, not a negative timestamp. Ambiguous
        # '-0:10' remains review-only instead of silently changing its sign.
        payload = re.sub(r"^[-*•]\s+", "", payload)
        time_source = payload
        times: list[float | None] = [None]
        timed = _TIME_PREFIX.match(payload)
        if timed:
            times = []
            for raw_time in _TIME_RE.findall(timed.group("times")):
                components = [int(v) for v in raw_time.split(":")]
                if components[-1] >= 60 or (len(components) == 3 and components[-2] >= 60):
                    times = []
                    break
                times.append(float(components[-1] + 60*components[-2] + (3600*components[0] if len(components) == 3 else 0)))
            payload = timed.group("payload").strip()
            if (not times or payload.startswith(('.', ',', ':')) or _TIME_RE.search(payload)
                    or time_source[timed.end("times"):timed.end("times") + 1].isdigit()):
                add("manual_review", start, end, reason="ambiguous_or_invalid_timecode")
                continue
        elif _TIME_RE.search(payload):
            add("manual_review", start, end, reason="ambiguous_or_invalid_timecode")
            continue

        command = re.sub(_LITERAL, "<literal>", payload)
        if re.match(r"(?:mantener|conservar|preservar)\b", command, re.I) and _VISUAL.search(command):
            add("manual_review", start, end, reason="unresolved_visual_constraint", times=times[0])
            continue
        # A leading prohibition/condition is not a bare lyric instruction.
        # Explicitly quoted lyrics beginning "No"/"Si" remain supported.
        if _UNSAFE.search(command) or re.match(r"(?:si|no)\s+", command, re.I):
            reason = "unresolved_visual_constraint" if _VISUAL.search(command) else "ambiguous_negated_or_contextual_instruction"
            add("manual_review", start, end, reason=reason, times=times[0])
            continue
        explicit_lyric = bool((timed and _quoted_literal(payload))
                              or _PAIR.fullmatch(payload) or _DICE_PAIR.fullmatch(payload)
                              or _SAY.fullmatch(payload) or _SAY_QUOTED.fullmatch(payload))
        if not explicit_lyric and _VISUAL.search(payload) and (_ACTION.match(payload) or re.search(r"\b(?:sin|no\s+(?:incluir|aparezcan))\b", payload, re.I)):
            if re.search(r"\b(?:cambiar|corregir|reemplazar).{0,15}\b(?:letra|frase|palabra)\b|\bdebe(?:r[ií]a)?\s+decir\b", payload, re.I):
                add("manual_review", start, end, reason="mixed_visual_and_lyric_instruction")
                continue
            add("background_review", start, end, reason="background_change_not_a_lyric_patch", confidence="medium")
            continue
        if _positive_layout(payload) and not timed:
            add("layout_context", start, end, reason="complete_phrase_on_one_screen")
            continue
        if _PERIOD.fullmatch(payload):
            scope = "all_matching" if _REPEAT.search(command) else "single"
            if not timed and scope != "all_matching":
                add("manual_review", start, end, reason="punctuation_scope_required")
            else:
                for at in times:
                    add("remove_terminal_period", start, end, times=at, scope=scope, confidence="high")
            continue
        note = _NOTE.search(payload)
        if note:
            add("timing_review", start, end, times=times[0], reason="timing_requires_audio_review", confidence="medium")
            payload = payload[:note.start()].strip()
        elif _TIMING.search(command):
            add("timing_review", start, end, times=times[0], reason="timing_requires_audio_review")
            if _STRUCTURE.search(payload):
                add("structure_review", start, end, times=times[0], reason="structure_requires_editor_review")
            continue

        pair = _PAIR.fullmatch(payload)
        dice_pair = _DICE_PAIR.fullmatch(payload)
        say = _SAY.fullmatch(payload) or _SAY_QUOTED.fullmatch(payload)
        current = None
        requested = None
        kind = "replace_text"
        scope = "single"
        if pair:
            current = _literal(pair.group("a") or pair.group("c"))
            requested = _literal(pair.group("b") or pair.group("d"))
            # Scope belongs to the matched pair, not a separately extracted
            # parenthetical review note or either quoted lyric literal.
            pair_command = re.sub(_LITERAL, "<literal>", payload)
            scope = "all_matching" if _REPEAT.search(pair_command) else "single"
        elif dice_pair:
            current = _literal(dice_pair.group("a"))
            requested = _literal(dice_pair.group("b"))
        elif say:
            requested = _quoted_literal(say.group(1))
        elif timed and payload:
            requested = _quoted_literal(payload)
            kind = "merge_phrase" if layout else "replace_text"
        if (pair or dice_pair) and (current is None or not current.strip()):
            # Invalid anchors must not degrade into an unanchored full-segment
            # replacement merely because this instruction has a timestamp.
            requested = None
        if requested and (timed or current):
            for at in times:
                add(kind, start, end, requested=requested, current=current, times=at, scope=scope, confidence="high" if current or say else "medium")
        else:
            kind = "structure_review" if _STRUCTURE.search(payload) else "audio_review" if re.search(r"\baudio\b", payload, re.I) else "manual_review"
            add(kind, start, end, times=times[0], reason="instruction_requires_manual_interpretation")

    return {"schema_version": SCHEMA_VERSION, "instructions": rows, "instruction_count": len(rows),
            "coverage": coverage, "coverage_complete": True,
            "has_actionable_text": any(r["kind"] in {"replace_text", "remove_terminal_period", "merge_phrase"} for r in rows)}
