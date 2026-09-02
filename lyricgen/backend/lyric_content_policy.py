"""Product policy for separating sung lyrics from speech and metadata."""

from __future__ import annotations

import re
import unicodedata
from typing import Any, Mapping


EDITORIAL_POLICY_ID = "rotor-umg-display-policy-v2"
MELODIC_INTERJECTION_MIN_SECONDS = 0.75


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


def editorial_display_decision(
    text: str,
    *,
    provider_kind: str = "",
    duration_s: float = 0.0,
    performer_role: str = "",
    compositional_speech: bool | None = None,
    adds_new_text: bool | None = None,
) -> dict[str, Any]:
    """Apply the approved seven-rule display policy without guessing roles.

    Ambiguous speech, crowd and backing-vocal cases are routed to review.  A
    text heuristic may never decide that speech is compositional or that a
    backing voice adds new text; those require provider/audio evidence.
    """

    classification = classify_content(text, provider_kind=provider_kind)
    role = str(performer_role or "").strip().lower()
    display = "normal"
    review_required = False
    reason = "lead_or_guest_lyric"

    if classification in {"METADATA", "CROWD_NOISE"}:
        display, reason = "do_not_show", "incidental_or_metadata"
    elif classification in {"SPEECH", "SPEECH_CANDIDATE"}:
        if compositional_speech is True:
            display, reason = "normal", "compositional_speech"
        elif compositional_speech is False:
            display, reason = "do_not_show", "incidental_speech"
        else:
            display, review_required = "review", True
            reason = "speech_compositionality_unknown"
    elif classification == "NONLEXICAL":
        if float(duration_s or 0.0) >= MELODIC_INTERJECTION_MIN_SECONDS:
            display, reason = "parenthesize", "long_melodic_interjection"
        else:
            display, reason = "do_not_show", "short_melodic_interjection"
    elif classification == "SUNG_CROWD" or role in {"backing", "crowd", "chorus"}:
        if adds_new_text is True:
            display, reason = "normal", "secondary_voice_adds_text"
        elif adds_new_text is False:
            display, reason = "do_not_show", "background_repeats_existing_text"
        else:
            display, review_required = "review", True
            reason = "secondary_voice_text_novelty_unknown"
    elif role == "adlib":
        display, reason = "parenthesize", "artist_adlib"

    return {
        "schema_version": "editorial-display-decision-v1",
        "policy_id": EDITORIAL_POLICY_ID,
        "classification": classification,
        "display": display,
        "reason": reason,
        "review_required": review_required,
        "safe_for_auto_insert": False,
    }


def classify_acoustic_window(structure: Mapping[str, Any]) -> dict[str, Any]:
    """Produce text-free editorial routing from acoustic event taxonomy."""

    events = [
        event
        for event in ((structure.get("best_partition") or {}).get("events") or [])
        if isinstance(event, Mapping)
    ]
    taxonomies = [str(event.get("taxonomy") or "UNKNOWN") for event in events]
    duration = sum(
        max(0.0, float(event.get("end") or 0.0) - float(event.get("start") or 0.0))
        for event in events
    )
    taxonomy_set = set(taxonomies)
    if not events:
        content_type, display, reason = "none", "do_not_show", "no_acoustic_events"
    elif taxonomy_set <= {"NONLEXICAL"}:
        decision = editorial_display_decision(
            "oh", provider_kind="vocalization", duration_s=duration,
        )
        content_type = "melodic_vocalization"
        display, reason = decision["display"], decision["reason"]
    elif taxonomy_set <= {"SPEECH"}:
        content_type, display = "speech", "review"
        reason = "speech_compositionality_unknown"
    elif taxonomy_set & {"SUNG_LEAD", "SUNG_CROWD"}:
        content_type, display = "lexical_candidate", "review"
        reason = "independent_text_consensus_required"
    else:
        content_type, display = "ambiguous", "review"
        reason = "mixed_or_unknown_acoustic_events"
    return {
        "schema_version": "acoustic-editorial-route-v1",
        "policy_id": EDITORIAL_POLICY_ID,
        "content_type": content_type,
        "display": display,
        "reason": reason,
        "event_count": len(events),
        "duration": round(duration, 3),
        # Ranking is still review-only.  The content gate merely decides
        # whether it is sensible to spend lexical ASR/judging work here.
        "allow_lexical_ranking": content_type in {"lexical_candidate", "speech"},
        "safe_for_auto_insert": False,
    }


_OMISSION_REASONS = frozenset({
    "voiced_gap", "uncovered_asr", "independent_uncovered_asr",
})


def route_omission_window(
    window: Mapping[str, Any], content_route: Mapping[str, Any],
) -> dict[str, Any]:
    """Apply the acoustic content gate only to omission-only windows.

    Mixed-risk windows keep their existing route because they may contain a
    timing or text mismatch in addition to an apparent gap.  Pure reverb,
    instrumental tails, melodic interjections and human-ambiguous audio stay
    observable but are not sent to the lexical omission ranker.
    """

    reasons = {
        str(value) for value in (
            window.get("reasons") or [window.get("reason")]
        ) if value
    }
    omission_only = bool(reasons) and reasons <= _OMISSION_REASONS
    content_type = str(content_route.get("content_type") or "ambiguous")
    allowed = bool(
        not omission_only or content_route.get("allow_lexical_ranking") is True
    )
    if allowed:
        reason = (
            "acoustic_content_supports_lexical_ranking"
            if omission_only else "not_an_omission_only_window"
        )
    else:
        reason = "content_gate_abstention"
    return {
        "schema_version": "omission-content-route-v1",
        "omission_only": omission_only,
        "content_type": content_type,
        "allow_lexical_ranking": allowed,
        "reason": reason,
        "safe_for_auto_insert": False,
    }
