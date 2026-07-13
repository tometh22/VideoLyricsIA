"""Internal policy for AI-generated backgrounds.

This module intentionally has no database, provider or HTTP dependencies.  It
turns the legacy wizard fields into a canonical creative mode and resolves the
atmospheric-effects opt-in *once* from the operator's raw prompt.  Derived
prompts (lyrics, genre, visual bible, scene beats, Gemini output) must never be
used as an authorization source.
"""

from __future__ import annotations

import os
import re
from typing import Any


POLICY_VERSION = "background-v6"
POLICY_ENV = "BACKGROUND_SMOKE_POLICY_MODE"
VALID_POLICY_MODES = frozenset({"off", "shadow", "enforce"})

# These are aesthetic defaults, not legal classifications.  They are kept in
# one place so prompt preflight and Vision output validation use the same
# vocabulary.  Exact word boundaries avoid false opt-ins such as ``fogón`` and
# ``smokestack``.
_ATMOSPHERIC_TERMS: tuple[tuple[str, str], ...] = (
    ("smoke", r"smoke|smoky|smokey|smoke[-\s]?filled"),
    ("fog", r"fog|foggy"),
    ("haze", r"haze|hazy"),
    ("mist", r"mist|misty"),
    ("steam", r"steam|steamy"),
    ("humo", r"humo|humareda"),
    ("niebla", r"niebla|neblina"),
    ("bruma", r"bruma"),
    ("vapor", r"vapor"),
    ("fumaca", r"fuma[cç]a|fumacento|fumacenta"),
    ("nevoa", r"n[eé]voa|nevoeiro"),
)

_TERM_RE = re.compile(
    r"\b(?:" + "|".join(pattern for _, pattern in _ATMOSPHERIC_TERMS) + r")\b",
    re.IGNORECASE,
)
_NEGATION_WORDS = (
    r"no|not|without|avoid|exclude|never|free|free\s+of|none|zero|"
    r"sin|evitar|excluir|nunca|libre\s+de|cero|nada\s+de|"
    r"ning[uú]n(?:a|o|as|os)?|"
    r"sem|n[aã]o|evite|exclua|nenhum(?:a|as|os)?|nada\s+de"
)
_NEGATION_BEFORE_CURRENT_RE = re.compile(
    rf"\b(?:{_NEGATION_WORDS})\b[^\n.!?;,]{{0,48}}$",
    re.IGNORECASE,
)
_NEGATION_AFTER_CURRENT_RE = re.compile(
    r"(?:^\s*(?:[-–—]\s*)?(?:free|libre|none|zero|cero)\b)|"
    r"(?:^\s*[:=\-–—]\s*(?:(?:is|es|[eé])\s+)?(?:"
    r"(?:(?:no|not|n[aã]o)\b(?:\s+(?:allowed|permitted|permitid[oa]s?))?)|"
    r"(?:none|zero|cero|ning[uú]n(?:a|o|as|os)?|nenhum(?:a|as|os)?|"
    r"forbidden|prohibited|prohibid[oa]s?|proibid[oa]s?|off|false|fals[oa]s?|"
    r"disabled|desactivad[oa]s?|deshabilitad[oa]s?|desabilitad[oa]s?|"
    r"desativad[oa]s?|desligad[oa]s?|apagad[oa]s?|0)\b)"
    r"\s*(?=$|[\n.!?;,]))",
    re.IGNORECASE,
)
_NEGATION_CONTRAST_RE = re.compile(
    r"\b(?:but|however|instead|with|pero|sino|con|por[eé]m|mas|com)\b",
    re.IGNORECASE,
)
_POSITIVE_SCOPE_RESET_RE = re.compile(
    r"\b(?:but|however|instead|with|use|add|include|create|generate|show|"
    r"pero|sino|con|usar|usa|agregar|agrega|incluir|incluye|crear|genera|mostrar|"
    r"por[eé]m|mas|com|use|adicione|inclua|crie|gere|mostre)\b",
    re.IGNORECASE,
)
_NEGATION_DIRECT_COMMAND_RE = re.compile(
    r"\b(?:do\s+not|don't|no|not|without|avoid|exclude|never|sin|evitar|"
    r"excluir|nunca|sem|n[aã]o|evite|exclua)\s*$",
    re.IGNORECASE,
)

# Appended at the provider boundary when the v6 policy is enforced. Repeating
# it at the last boundary protects literal prompts without rewriting them.
ATMOSPHERIC_NEGATIVE_RAIL = (
    "Do not introduce smoke, fog, haze, mist, steam, vapor, stage smoke, "
    "smoke machines, or smoke-like atmospheric clouds. Keep the air clear "
    "and visibility crisp."
)


def policy_mode(env: dict[str, str] | None = None) -> str:
    """Return ``off``, ``shadow`` or ``enforce``; invalid values fail safe.

    ``off`` is the deployment default so shipping code cannot silently change
    production output before API/Worker/ShortWorker are on the same commit.
    """
    source = os.environ if env is None else env
    value = str(source.get(POLICY_ENV, "off") or "off").strip().lower()
    return value if value in VALID_POLICY_MODES else "off"


def resolve_creative_mode(
    *,
    match_lyrics: bool = True,
    operator_prompt: str | None = None,
    verbatim: bool = False,
) -> str:
    """Resolve legacy fields into one deterministic internal mode."""
    if (operator_prompt or "").strip():
        return "prompt_literal" if verbatim else "prompt_improved"
    return "lyrics" if match_lyrics else "auto"


def _positive_atmospheric_matches(text: str) -> list[str]:
    """Return positive effect mentions, excluding nearby negations."""
    positives: list[str] = []
    value = text or ""
    matches = list(_TERM_RE.finditer(value))
    for index, match in enumerate(matches):
        start, end = match.span()
        # Commas do not necessarily end a negative list: "no smoke, fog,
        # haze" denies all three.  Hard punctuation ends the list; an explicit
        # contrast/action verb ("but use smoke", "pero agrega humo") starts a
        # new positive scope.  A negation only propagates across a comma when
        # an earlier atmospheric item exists, so "no people, smoke" still opts
        # into smoke.
        hard_clause = re.split(r"[\n.!?;]", value[:start])[-1]
        current_segment = hard_clause.rsplit(",", 1)[-1]
        after = value[end:min(len(value), end + 40)]
        direct_before = current_segment
        reset_matches = list(_POSITIVE_SCOPE_RESET_RE.finditer(current_segment))
        if reset_matches:
            last_reset = reset_matches[-1]
            if not _NEGATION_DIRECT_COMMAND_RE.search(
                current_segment[:last_reset.start()]
            ):
                direct_before = current_segment[last_reset.end():]
        inherited_negation = False
        for previous in reversed(matches[:index]):
            between = value[previous.end():start]
            if re.search(r"[\n.!?;]", between):
                break
            previous_clause = re.split(r"[\n.!?;]", value[:previous.start()])[-1]
            if _POSITIVE_SCOPE_RESET_RE.search(between):
                break
            if _NEGATION_BEFORE_CURRENT_RE.search(previous_clause):
                inherited_negation = True
                break
        if (
            _NEGATION_BEFORE_CURRENT_RE.search(direct_before)
            or _NEGATION_AFTER_CURRENT_RE.search(after)
            or inherited_negation
        ):
            continue
        positives.append(match.group(0).lower())
    return positives


def resolve_atmospherics_policy(
    operator_prompt: str | None,
    *,
    mode: str | None = None,
) -> dict[str, Any]:
    """Resolve atmospheric permission only from raw operator-authored text."""
    raw = (operator_prompt or "").strip()
    requested = _positive_atmospheric_matches(raw)
    requested_mode = (mode or policy_mode()).strip().lower()
    active_mode = requested_mode if requested_mode in VALID_POLICY_MODES else "off"
    return {
        "policy_version": POLICY_VERSION,
        "policy_mode": active_mode,
        "allow_atmospherics": bool(requested),
        "explicit_atmospherics": requested,
        "authorization_source": "operator_prompt" if requested else "default_deny",
    }


def rebase_stored_atmospherics_policy(
    stored_policy: dict[str, Any] | None,
    *,
    mode: str | None = None,
) -> dict[str, Any]:
    """Move trusted persisted authorization onto the active rollout mode.

    A v6 scene plan stores the *result* of resolving the raw operator prompt so
    later edits do not need to reinterpret generated scene text. Only that
    allow/deny decision survives; off/shadow/enforce is always read again from
    the current runtime. Legacy or malformed metadata fails closed to deny.
    """
    requested_mode = (mode or policy_mode()).strip().lower()
    active_mode = requested_mode if requested_mode in VALID_POLICY_MODES else "off"
    trusted = bool(
        isinstance(stored_policy, dict)
        and stored_policy.get("policy_version") == POLICY_VERSION
        and stored_policy.get("authorization_source") in {
            "operator_prompt", "default_deny"
        }
    )
    allowed = bool(
        trusted
        and stored_policy.get("allow_atmospherics")
        and stored_policy.get("authorization_source") == "operator_prompt"
    )
    explicit = stored_policy.get("explicit_atmospherics", []) if trusted else []
    if (
        not isinstance(explicit, list)
        or not all(isinstance(item, str) for item in explicit)
        or (allowed and not explicit)
        or (allowed and not all(atmospheric_terms(item) for item in explicit))
    ):
        explicit = []
        allowed = False
    return {
        "policy_version": POLICY_VERSION,
        "policy_mode": active_mode,
        "allow_atmospherics": allowed,
        "explicit_atmospherics": explicit if allowed else [],
        "authorization_source": "operator_prompt" if allowed else "default_deny",
    }


def policy_enforces(policy: dict[str, Any] | None) -> bool:
    return bool(policy and policy.get("policy_mode") == "enforce")


def policy_observes(policy: dict[str, Any] | None) -> bool:
    return bool(policy and policy.get("policy_mode") in {"shadow", "enforce"})


def atmospheric_terms(text: str | None) -> list[str]:
    """Find atmospheric vocabulary in generated/internal text.

    Unlike the authorization detector, this intentionally also returns
    negated mentions; callers use it for observability and preflight checks,
    not for permission.
    """
    return [match.group(0).lower() for match in _TERM_RE.finditer(text or "")]


def sanitize_generated_text(
    text: str | None,
    policy: dict[str, Any] | None,
) -> str:
    """Remove automatic atmospheric motifs under ``enforce``.

    Operator-authored literal text is never passed here.  Generated creative
    text is normalized to clear-air language before reaching Imagen/Veo so the
    final negative rail does not contradict a positive prompt.
    """
    value = text or ""
    if not policy_enforces(policy) or policy.get("allow_atmospherics"):
        return value
    value = _TERM_RE.sub("clear air", value)
    # Generated prompts sometimes already contain a negative such as
    # "do not introduce smoke". A naive token replacement would turn that
    # into the contradictory "do not introduce clear air". Normalize those
    # phrases before the immutable provider rail is appended.
    value = re.sub(
        rf"\b(?:do\s+not|don't|no|not|without|avoid|exclude|never|sin|evitar|"
        rf"excluir|nunca|sem|n[aã]o|evite)\b(?:\s+\w+){{0,4}}\s+clear\s+air\b",
        "keep the air clear",
        value,
        flags=re.IGNORECASE,
    )
    value = re.sub(r"\b(?:stage|theatrical)\s+clear\s+air\s+machines?\b", "clear air", value, flags=re.I)
    value = re.sub(
        r"\bclear air(?:\s*[,/]\s*|\s+(?:and|or)\s+)clear air\b",
        "clear air",
        value,
        flags=re.IGNORECASE,
    )
    value = re.sub(r"\s{2,}", " ", value)
    value = re.sub(r"\s+([,.;:])", r"\1", value)
    return value.strip()


def finalize_provider_prompt(
    prompt: str | None,
    policy: dict[str, Any] | None,
    *,
    generated: bool,
) -> str:
    """Apply the atmospheric policy at the last provider boundary."""
    value = prompt or ""
    if generated:
        value = sanitize_generated_text(value, policy)
    if policy_enforces(policy) and not policy.get("allow_atmospherics"):
        value = f"{value.rstrip().rstrip('.')}. {ATMOSPHERIC_NEGATIVE_RAIL}".strip()
    return value


def cache_policy_fingerprint(policy: dict[str, Any] | None) -> str:
    """Stable cache component; prevents off/shadow/enforce cross-hits."""
    if not policy:
        return f"{POLICY_VERSION}:off:deny"
    allowed = "allow" if policy.get("allow_atmospherics") else "deny"
    return f"{policy.get('policy_version', POLICY_VERSION)}:{policy.get('policy_mode', 'off')}:{allowed}"


def runtime_rollout_fingerprint(*, mode: str | None = None) -> str:
    """Release-independent token used to keep API and workers in flag lockstep."""
    active = (mode or policy_mode()).strip().lower()
    if active not in VALID_POLICY_MODES:
        active = "off"
    return f"{POLICY_VERSION}:{active}"
