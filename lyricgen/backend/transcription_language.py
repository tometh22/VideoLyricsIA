"""Language resolution shared by the transcription post-processing chain.

An empty language means "auto-detect".  It must never be silently converted
to Spanish: doing so makes every secondary ASR pass contradict the primary
transcription on English (and other non-Spanish) songs.
"""

from __future__ import annotations

import re
from collections.abc import Iterable


SUPPORTED_LANGUAGES = frozenset({"es", "en", "pt", "fr", "it", "de"})

_MARKERS = {
    "es": frozenset(
        "de la que el en y los se del las por con una para como pero sus ya "
        "porque entre cuando donde desde todo nosotros nunca siempre estoy "
        "tengo tiene eres somos".split()
    ),
    "en": frozenset(
        "the and you that was for are with they this have from not but what "
        "can your could them other than now only our because these i it my me "
        "to of in is on he she as do at by we or will so if when all be been".split()
    ),
    "pt": frozenset(
        "de da do que e em para com uma nao nao por os as se no na meu minha "
        "voce voces eu ele ela nos somos estou tem".split()
    ),
    "fr": frozenset(
        "de la le les que et en pour avec une pas des du un je tu il elle nous "
        "vous ils mon ma mes dans sur est sont".split()
    ),
    "it": frozenset(
        "di la il le che e in per con una non del dei un io tu lui lei noi voi "
        "sono nel sul mia mio come".split()
    ),
    "de": frozenset(
        "der die das und ich du er sie wir ihr nicht mit von zu den ein eine "
        "ist sind auf fuer für mein meine im dem".split()
    ),
}


def normalize_language(value: str | None) -> str | None:
    """Return a supported ISO-639-1 language code or ``None`` for auto."""
    code = (value or "").strip().lower().replace("_", "-").split("-", 1)[0]
    return code if code in SUPPORTED_LANGUAGES else None


def _texts(value) -> Iterable[str]:
    if isinstance(value, str):
        yield value
        return
    if isinstance(value, dict):
        for segment in value.get("segments") or []:
            if isinstance(segment, dict):
                yield str(segment.get("text") or "")
        return
    if isinstance(value, Iterable):
        for item in value:
            if isinstance(item, dict):
                yield str(item.get("text") or "")
            else:
                yield str(item or "")


def detect_text_language(value) -> str | None:
    """Detect a supported language by conservative function-word voting.

    The detector intentionally returns ``None`` for short or mixed text.  A
    false negative lets the ASR autodetect; a false positive would force every
    recovery pass into the wrong language, which is the damaging failure mode.
    """
    scores = {language: 0 for language in _MARKERS}
    for text in _texts(value):
        for token in re.findall(r"[a-zA-ZÀ-ÿ']+", text.casefold()):
            for language, markers in _MARKERS.items():
                if token in markers:
                    scores[language] += 1

    ranked = sorted(scores.items(), key=lambda item: item[1], reverse=True)
    winner, winner_score = ranked[0]
    runner_score = ranked[1][1]
    if winner_score < 4:
        return None
    if runner_score and winner_score < runner_score * 1.5:
        return None
    return winner


def resolve_transcription_language(
    requested_language: str | None,
    *,
    result: dict | None = None,
    reference_text: str = "",
) -> str | None:
    """Resolve the language used by every transcription post-pass.

    An explicit supported choice always wins.  In auto mode, canonical
    reference lyrics are the strongest signal, followed by the recognized
    segments.  Unknown remains ``None`` so downstream ASR providers can use
    their own language detection.
    """
    explicit = normalize_language(requested_language)
    if explicit:
        return explicit
    return detect_text_language(reference_text) or detect_text_language(result or {})


__all__ = [
    "SUPPORTED_LANGUAGES",
    "detect_text_language",
    "normalize_language",
    "resolve_transcription_language",
]
