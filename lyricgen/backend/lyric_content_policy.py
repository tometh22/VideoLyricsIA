"""Product policy for separating sung lyrics from speech and metadata."""

from __future__ import annotations

import re
import unicodedata


_METADATA = re.compile(
    r"(?:^|\b)(?:cc\s+por|subtitles?\s*(?::|(?:created\s+)?by\b)|"
    r"credits?\s*(?::|by\b)|subtitulad[oa]s?\s+por|"
    r"subtitulos?\s+(?:por|realizados?)|creditos?\s*(?::|por\b)|"
    r"lyrics?\s*(?::|by\b)|transcri(?:pto|bed)\s+by|synced\s+by|"
    r"copyright|todos\s+los\s+derechos|amara(?:\s*\.\s*|\s+)org|"
    r"antarctica\s+films?|publisher|record\s+label)(?=\b|\s|:|$)",
    re.IGNORECASE,
)
_CHATTER = re.compile(
    r"^(?:gracias(?:\s+totales)?|muchas\s+gracias|buenas\s+noches|"
    r"chau|chao|adios|hola\s+[a-z]+|como\s+estan)(?:[!.,\s]+.*)?$",
    re.IGNORECASE,
)
_STAGE = re.compile(
    r"^(?:aplausos?|ovacion|publico|crowd|audiencia|silencio|silence|"
    r"instrumental|musica)(?:\b.*)?$", re.IGNORECASE,
)
_SUNG_KINDS = {
    "sung", "singing", "sung_lead", "lead_singing",
    "crowd_singing", "chorus", "sung_crowd",
}


def _fold(text: str) -> str:
    normalized = unicodedata.normalize("NFKD", str(text or ""))
    return "".join(ch for ch in normalized if not unicodedata.combining(ch)).strip()


def classify_content(text: str, *, provider_kind: str = "") -> str:
    folded = _fold(text)
    lexical = re.sub(r"^[^\w]+|[^\w]+$", "", folded, flags=re.UNICODE)
    kind = str(provider_kind or "").strip().lower()
    if _METADATA.search(folded):
        return "METADATA"
    if kind in {"speech", "spoken", "stage_speech"}:
        return "SPEECH"
    if kind in {"crowd_noise", "applause", "music", "silence"} or _STAGE.match(lexical):
        return "CROWD_NOISE"
    if kind in {"nonlexical", "vocalization", "sustained"}:
        return "NONLEXICAL"
    if kind in {"crowd_singing", "chorus", "sung_crowd"}:
        return "SUNG_CROWD"
    # Acoustic/provider role outranks lexical chatter heuristics.  Words such
    # as "gracias" are valid lyrics when sung; the text alone is never enough
    # to erase them.
    if kind in _SUNG_KINDS:
        return "SUNG_LEAD"
    if _CHATTER.match(lexical):
        return "SPEECH_CANDIDATE"
    return "SUNG_LEAD"


def should_include_as_lyric(
    text: str, *, provider_kind: str = "", isolated_tail: bool = False,
) -> bool:
    classification = classify_content(text, provider_kind=provider_kind)
    if classification in {"METADATA", "SPEECH", "CROWD_NOISE"}:
        return False
    if classification == "SPEECH_CANDIDATE" and isolated_tail:
        return False
    return True
