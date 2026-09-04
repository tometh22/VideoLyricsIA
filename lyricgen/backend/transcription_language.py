"""Language resolution shared by the transcription post-processing chain.

An empty language means "auto-detect".  It must never be silently converted
to Spanish: doing so makes every secondary ASR pass contradict the primary
transcription on English (and other non-Spanish) songs.
"""

from __future__ import annotations

import re
import unicodedata
from collections import Counter
from collections.abc import Iterable


SUPPORTED_LANGUAGES = frozenset({"es", "en", "pt", "fr", "it", "de"})

_MARKERS = {
    "es": frozenset(
        "de la que el en y los se del las por con una para como pero sus ya "
        "porque entre cuando donde desde todo nosotros nunca siempre estoy "
        "tengo tiene eres somos hoy ayer nadie tampoco quizás mientras aquí "
        "allí muy más está estás estaba fueron hice daño lejos temprano tiempo "
        "no nos un uno tu tú mi mí yo me te si sí amor vida corazón corazon".split()
    ),
    "en": frozenset(
        "the and you that was for are with they this have from not but what "
        "can your could them other than now only our because these i it my me "
        "to of in is on he she as do at by we or will so if when all be been".split()
    ),
    "pt": frozenset(
        "de da do que e em para com uma nao nao por os as se no na meu minha "
        "voce voces eu ele ela nos somos estou tem não você vocês nós também "
        "hoje muito estão quero quando onde porque".split()
    ),
    "fr": frozenset(
        "de la le les que et en pour avec une pas des du un je tu il elle nous "
        "vous ils mon ma mes dans sur est sont ne ce qui au aux mais où pourquoi "
        "quand très aussi cette être avoir".split()
    ),
    "it": frozenset(
        "di la il le che e in per con una non del dei un io tu lui lei noi voi "
        "sono nel sul mia mio come ma perché quando dove questo questa anche "
        "molto più già".split()
    ),
    "de": frozenset(
        "der die das und ich du er sie wir ihr nicht mit von zu den ein eine "
        "ist sind auf fuer für mein meine im dem auch aber weil wenn wie was "
        "wer wo warum sehr noch schon nur kein keine".split()
    ),
}

# A language may only win when the text contains at least two markers that are
# genuinely useful to distinguish it.  Common Romance words (no/de/que/un/tu)
# still contribute to the broad score, but can never force a language alone.
_DIAGNOSTIC_MARKERS = {
    "es": frozenset(
        "hoy ayer nadie tampoco quizás aquí allí daño lejos temprano tiempo "
        "estoy tengo eres fueron hice nunca noche amor vida yo corazón corazon".split()
    ),
    "en": frozenset(
        "the and you that was are with they this have from not what can your "
        "could them than our because these been will we me alone tonight leave".split()
    ),
    "pt": frozenset(
        "não você vocês nós eu uma hoje muito minha meu estou também quero "
        "quando onde estão".split()
    ),
    "fr": frozenset(
        "je vous ils elle elles nous est sont avec dans mais très cette être "
        "avoir ne pas veux peur amour abandonne".split()
    ),
    "it": frozenset(
        "io lei noi voi sono nel sul perché questa questo anche molto già vicini".split()
    ),
    "de": frozenset(
        "ich und du wir ihr nicht mit sind für mein meine auch weil sehr schon "
        "schön hier".split()
    ),
}

# Catalan and Galician are not selectable today.  Forcing either into the
# nearest supported Romance language is more damaging than leaving provider
# autodetection on, so two characteristic markers veto a forced result.
_UNSUPPORTED_DIAGNOSTIC_MARKERS = {
    "ca": frozenset(
        "nit llum temps avui aquesta aquest amb però quan soc ets vosaltres "
        "meva teva seva encara".split()
    ),
    "gl": frozenset(
        "non sei noite onte miña túa súa galego cando onde".split()
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
    # Cap each token at two votes: repetition remains useful for short choruses,
    # but a hundred "no"s can never overwhelm the rest of the song. Normalize
    # NFC first so equivalent provider encodings tokenize identically. Split
    # apostrophes so common contractions still expose useful words (we're,
    # j'ai, c'est) instead of becoming unknown opaque tokens.
    token_counts: Counter[str] = Counter()
    for text in _texts(value):
        normalized = unicodedata.normalize("NFC", text).casefold()
        normalized = normalized.replace("’", "'").replace("'", " ")
        token_counts.update(re.findall(r"[^\W\d_]+", normalized, re.UNICODE))

    scores = {
        language: sum(min(token_counts[token], 2) for token in markers)
        for language, markers in _MARKERS.items()
    }

    # Refuse to coerce neighboring unsupported Romance languages.
    for markers in _UNSUPPORTED_DIAGNOSTIC_MARKERS.values():
        if len(set(token_counts).intersection(markers)) >= 2:
            return None

    ranked = sorted(scores.items(), key=lambda item: item[1], reverse=True)
    winner, winner_score = ranked[0]
    runner_score = ranked[1][1]
    if winner_score < 4:
        return None
    diagnostic_hits = len(
        set(token_counts).intersection(_DIAGNOSTIC_MARKERS[winner])
    )
    if diagnostic_hits < 2:
        return None
    # Equality at the confidence boundary is ambiguous, not a win.  This
    # catches compact mixed-language text where each side has real evidence.
    if runner_score and winner_score <= runner_score * 1.5:
        return None
    # Re-check with the multi-label profile: a dominant verse must not hide a
    # genuinely evidenced second language in a bilingual chorus.
    languages = detect_text_languages(value)
    return winner if languages == {winner} else None


def detect_text_languages(value) -> frozenset[str]:
    """Return all supported languages with meaningful evidence.

    A bilingual song is valid input. It must remain multi-label instead of
    being coerced to whichever language happens to have the highest score.
    """
    token_counts: Counter[str] = Counter()
    for text in _texts(value):
        normalized = unicodedata.normalize("NFC", text).casefold()
        normalized = normalized.replace("’", "'").replace("'", " ")
        token_counts.update(re.findall(r"[^\W\d_]+", normalized, re.UNICODE))

    for markers in _UNSUPPORTED_DIAGNOSTIC_MARKERS.values():
        if len(set(token_counts).intersection(markers)) >= 2:
            return frozenset()

    scores = {
        language: sum(min(token_counts[token], 2) for token in markers)
        for language, markers in _MARKERS.items()
    }
    winner_score = max(scores.values(), default=0)
    if winner_score < 4:
        return frozenset()
    return frozenset(
        language for language, score in scores.items()
        if score >= 4
        and score >= winner_score * 0.25
        and len(set(token_counts).intersection(_DIAGNOSTIC_MARKERS[language])) >= 2
    )


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
    reference_languages = detect_text_languages(reference_text)
    if len(reference_languages) == 1:
        return next(iter(reference_languages))
    result_languages = detect_text_languages(result or {})
    if len(result_languages) == 1:
        return next(iter(result_languages))
    return None


def diagnose_language_state(value, persisted_language: str | None = None) -> dict:
    """Classify an unknown LID without guessing a language.

    This deliberately exposes no lyric text.  It distinguishes an intentional
    mixed/insufficient-evidence abstention from the operational bug where a
    single supported language is detectable in the persisted lines but the
    stored song metric still says ``unknown``.
    """
    languages = sorted(detect_text_languages(value))
    persisted = normalize_language(persisted_language)
    if persisted:
        classification = "known"
    elif len(languages) > 1:
        classification = "mixed_language_abstention"
    elif len(languages) == 1:
        classification = "lid_persistence_failure"
    else:
        classification = "insufficient_evidence_abstention"
    return {
        "classification": classification,
        "persisted_language": persisted,
        "detected_languages": languages,
        "mixed_language": len(languages) > 1,
    }


__all__ = [
    "SUPPORTED_LANGUAGES",
    "diagnose_language_state",
    "detect_text_language",
    "detect_text_languages",
    "normalize_language",
    "resolve_transcription_language",
]
