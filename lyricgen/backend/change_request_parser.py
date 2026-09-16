"""Deterministic parser for label-authored delivery change requests.

The parser deliberately extracts instructions; it never decides what a lyric
*should* say.  Any proposed lyric text must be traceable to the client's own
comment.  Ambiguous, structural and timing instructions remain review-only.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import re
import unicodedata
from typing import Iterable


SCHEMA_VERSION = "change-request-parser-v1"

_TIMECODE_RE = re.compile(
    r"(?<!\d)(?:(?P<hours>\d{1,2}):)?(?P<minutes>\d{1,2}):(?P<seconds>\d{2})(?!\d)"
)
_QUOTED_RE = re.compile(r"[\"“”'‘’]([^\"“”'‘’]{1,500})[\"“”'‘’]")
_REPEAT_RE = re.compile(
    r"\b(todas?\s+las?\s+(?:apariciones|veces)|cada\s+vez|en\s+todo\s+el\s+tema|"
    r"todos?\s+los?\s+(?:coros?|estribillos?))\b",
    re.IGNORECASE,
)
_TIMING_RE = re.compile(
    r"\b(sincroniz|timing|desfasad|antes\s+de\s+que\s+termine|"
    r"mantener.+(?:tiempo|más)|entra\s+(?:tarde|antes)|termina\s+(?:tarde|antes))",
    re.IGNORECASE,
)
_STRUCTURE_RE = re.compile(
    r"\b(unir|separar|dividir|juntar|frase\s+completa|misma\s+pantalla|"
    r"una\s+sola\s+(?:línea|linea)|dos\s+(?:líneas|lineas))\b",
    re.IGNORECASE,
)
_BACKGROUND_RE = re.compile(
    r"\b(fondo|background|prompt|escena|imagen|animaci[oó]n|personas?|logo)\b",
    re.IGNORECASE,
)
_AUDIO_RE = re.compile(r"\baudio\b", re.IGNORECASE)
_TERMINAL_PERIOD_RE = re.compile(
    r"\b(?:sacar|quitar|eliminar|sin)\b.{0,45}\b(?:puntos?|punto\s+final)\b|"
    r"\bpuntos?\s+finales?\b",
    re.IGNORECASE | re.DOTALL,
)
_INSTRUCTION_PREFIX_RE = re.compile(
    r"^(?:revisar|chequear|verificar|corregir|cambiar|quitar|sacar|eliminar|"
    r"unir|separar|dividir|juntar|mantener|dejar|poner|hacer|audio|fondo)\b",
    re.IGNORECASE,
)
_TRAILING_REVIEW_NOTE_RE = re.compile(
    r"\s*\((?:revisar|chequear|verificar|timing|sync|sincroniz|"
    r"que\s+termina|que\s+empieza)[^)]*\)\s*$",
    re.IGNORECASE,
)


def fold_text(value: str) -> str:
    value = unicodedata.normalize("NFKD", (value or "").casefold())
    value = "".join(ch for ch in value if not unicodedata.combining(ch))
    return " ".join(re.findall(r"[\w]+", value, flags=re.UNICODE))


def parse_timecode(match: re.Match[str]) -> float:
    hours = int(match.group("hours") or 0)
    minutes = int(match.group("minutes") or 0)
    seconds = int(match.group("seconds") or 0)
    return float(hours * 3600 + minutes * 60 + seconds)


@dataclass(frozen=True)
class ParsedInstruction:
    id: str
    kind: str
    source_excerpt: str
    timecode_seconds: float | None = None
    current_text: str | None = None
    requested_text: str | None = None
    scope: str = "single"
    confidence: str = "low"
    reason: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


def _clean_candidate(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip(" \t\r\n:;,_-–—.¡!¿?")
    return value[:500] or None


def _bare_timecoded_requested_text(chunk: str) -> str | None:
    """Extract the portal's common ``timestamp + corrected line`` format."""
    payload = _TIMECODE_RE.sub("", chunk, count=1).strip(" \t\r\n:;,_-–—")
    payload = payload.splitlines()[0].strip() if payload else ""
    if not payload or _INSTRUCTION_PREFIX_RE.search(payload):
        return None
    payload = _TRAILING_REVIEW_NOTE_RE.sub("", payload).strip()
    if not payload or len(fold_text(payload).split()) > 40:
        return None
    return _clean_candidate(payload)


def _unquoted_requested_text(chunk: str) -> str | None:
    patterns = (
        r"\b(?:debe|deber[ií]a)\s+decir\s*[:\-]?\s*(.+)",
        r"\bcorregir(?:\s+a)?\s*[:\-]?\s*(.+)",
        r"\bcambiar\s+por\s*[:\-]?\s*(.+)",
    )
    for pattern in patterns:
        match = re.search(pattern, chunk, flags=re.IGNORECASE | re.DOTALL)
        if match:
            value = re.split(r"[\n;]", match.group(1), maxsplit=1)[0]
            return _clean_candidate(value)
    return None


def _explicit_pair(chunk: str, quotes: list[str]) -> tuple[str | None, str | None]:
    if len(quotes) >= 2 and re.search(
        r"\b(cambiar|por|donde\s+dice|en\s+vez\s+de|debe(?:r[ií]a)?\s+decir)\b",
        chunk,
        flags=re.IGNORECASE,
    ):
        return _clean_candidate(quotes[0]), _clean_candidate(quotes[-1])

    match = re.search(
        r"\bdonde\s+dice\s+(.+?)\s+(?:debe|deber[ií]a)\s+decir\s+(.+?)(?:[\n;]|$)",
        chunk,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if match:
        return _clean_candidate(match.group(1)), _clean_candidate(match.group(2))

    match = re.search(
        r"\bcambiar\s+(.+?)\s+por\s+(.+?)(?:[\n;]|$)",
        chunk,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if match:
        return _clean_candidate(match.group(1)), _clean_candidate(match.group(2))
    return None, None


def _timecoded_chunks(comment: str) -> Iterable[tuple[float, str]]:
    matches = list(_TIMECODE_RE.finditer(comment))
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(comment)
        yield parse_timecode(match), comment[match.start():end].strip()


def parse_change_request(comment: str) -> dict:
    """Return conservative structured instructions from one free-text request."""
    comment = (comment or "").strip()
    instructions: list[ParsedInstruction] = []
    seen: set[tuple] = set()

    def add(**kwargs) -> None:
        key = (
            kwargs.get("kind"), kwargs.get("timecode_seconds"),
            fold_text(kwargs.get("current_text") or ""),
            fold_text(kwargs.get("requested_text") or ""),
        )
        if key in seen:
            return
        seen.add(key)
        instructions.append(ParsedInstruction(
            id=f"instruction-{len(instructions) + 1}", **kwargs,
        ))

    scope = "all_matching" if _REPEAT_RE.search(comment) else "single"
    chunks = list(_timecoded_chunks(comment))
    for timecode, chunk in chunks:
        quotes = [_clean_candidate(value) for value in _QUOTED_RE.findall(chunk)]
        quotes = [value for value in quotes if value]
        current, requested = _explicit_pair(chunk, quotes)
        if not requested and quotes:
            requested = quotes[-1]
        if not requested:
            requested = _unquoted_requested_text(chunk)
        if not requested:
            requested = _bare_timecoded_requested_text(chunk)
        if requested:
            add(
                kind="replace_text", source_excerpt=chunk[:600],
                timecode_seconds=timecode, current_text=current,
                requested_text=requested, scope=scope,
                confidence="high" if quotes or current else "medium",
            )
        if _TIMING_RE.search(chunk):
            add(
                kind="timing_review", source_excerpt=chunk[:600],
                timecode_seconds=timecode, scope="single", confidence="medium",
                reason="timing_requires_audio_review",
            )
        if _STRUCTURE_RE.search(chunk):
            add(
                kind="structure_review", source_excerpt=chunk[:600],
                timecode_seconds=timecode, scope="single", confidence="medium",
                reason="structure_requires_editor_review",
            )
        if (
            not requested
            and not _TIMING_RE.search(chunk)
            and not _STRUCTURE_RE.search(chunk)
        ):
            add(
                kind="manual_review", source_excerpt=chunk[:600],
                timecode_seconds=timecode, confidence="low",
                reason="timecoded_instruction_not_understood",
            )

    # Non-timecoded explicit replacements are still useful.
    if not chunks:
        quotes = [_clean_candidate(value) for value in _QUOTED_RE.findall(comment)]
        quotes = [value for value in quotes if value]
        current, requested = _explicit_pair(comment, quotes)
        if requested:
            add(
                kind="replace_text", source_excerpt=comment[:600],
                current_text=current, requested_text=requested, scope=scope,
                confidence="medium" if current else "low",
            )

    if _TERMINAL_PERIOD_RE.search(comment):
        add(
            kind="remove_terminal_period", source_excerpt=comment[:600],
            scope="all_matching", confidence="high",
        )
    if _BACKGROUND_RE.search(comment):
        add(
            kind="background_review", source_excerpt=comment[:600],
            confidence="medium", reason="background_change_not_a_lyric_patch",
        )
    if _AUDIO_RE.search(comment) and re.search(
        r"\b(incorrect|equivoc|no\s+es|no\s+est[aá]\s+bien|mal)\b",
        comment,
        flags=re.IGNORECASE,
    ):
        add(
            kind="audio_review", source_excerpt=comment[:600],
            confidence="medium", reason="audio_identity_requires_manual_review",
        )
    if _TIMING_RE.search(comment) and not any(
        row.kind == "timing_review" for row in instructions
    ):
        add(
            kind="timing_review", source_excerpt=comment[:600],
            confidence="medium", reason="timing_requires_audio_review",
        )
    if _STRUCTURE_RE.search(comment) and not any(
        row.kind == "structure_review" for row in instructions
    ):
        add(
            kind="structure_review", source_excerpt=comment[:600],
            confidence="medium", reason="structure_requires_editor_review",
        )
    if not instructions and comment:
        add(
            kind="manual_review", source_excerpt=comment[:600], confidence="low",
            reason="request_not_understood",
        )

    return {
        "schema_version": SCHEMA_VERSION,
        "instructions": [row.to_dict() for row in instructions],
        "instruction_count": len(instructions),
        "has_actionable_text": any(
            row.kind in {"replace_text", "remove_terminal_period"}
            for row in instructions
        ),
    }
