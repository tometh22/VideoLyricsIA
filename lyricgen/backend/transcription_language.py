"""Language resolution shared by the transcription post-processing chain.

An empty language means "auto-detect".  It must never be silently converted
to Spanish: doing so makes every secondary ASR pass contradict the primary
transcription on English (and other non-Spanish) songs.
"""

from __future__ import annotations

import os
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
        "estoy tengo eres fueron hice corazón corazon".split()
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


def forced_language_for_tenant(
    tenant_id: str | None, requested_language: str | None
) -> str:
    """Pin the transcription language for single-language tenants.

    A tenant that only ever uploads one language (e.g. UMG Chile = always
    Spanish) can be configured to force it, so WhisperX cannot misdetect a
    Spanish song as English and poison the transcription cache under `en`
    (incident 2026-08-12: Sebastián/UMG Chile got English lyrics from a
    Spanish audio because auto-detect chose `en`, and every re-upload hit
    the cached English result).

    Env: ``TRANSCRIPTION_LANG_BY_TENANT="universal_chile:es,other:pt"``.
    Read per-call (not import-time) so an ops change applies on redeploy
    without import-order surprises — same pattern as the editor_v2 gate.

    A configured tenant's language OVERRIDES auto-detect. If the tenant is
    not configured, the requested language passes through unchanged.
    """
    tid = (tenant_id or "").strip().lower()
    if not tid:
        return requested_language or ""
    raw = os.environ.get("TRANSCRIPTION_LANG_BY_TENANT", "")
    for pair in raw.split(","):
        if ":" not in pair:
            continue
        mapped_tenant, _, mapped_lang = pair.partition(":")
        if mapped_tenant.strip().lower() == tid:
            forced = normalize_language(mapped_lang)
            if forced:
                return forced
    return requested_language or ""


__all__ = [
    "SUPPORTED_LANGUAGES",
    "detect_text_language",
    "normalize_language",
    "resolve_transcription_language",
    "forced_language_for_tenant",
]
