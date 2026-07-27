"""Contract tests for the background atmospheric-effects policy.

The important security boundary here is provenance: only the raw prompt typed
by the operator may opt a render into smoke/fog/haze/mist/steam.  Metadata and
creative context generated elsewhere in the pipeline are deliberately absent
from the resolver API.
"""

from __future__ import annotations

import inspect

import pytest

from background_policy import (
    ATMOSPHERIC_NEGATIVE_RAIL,
    POLICY_ENV,
    POLICY_VERSION,
    atmospheric_terms,
    cache_policy_fingerprint,
    finalize_provider_prompt,
    policy_enforces,
    policy_mode,
    policy_observes,
    resolve_atmospherics_policy,
    resolve_creative_mode,
    rebase_stored_atmospherics_policy,
    sanitize_generated_text,
)


@pytest.mark.parametrize(
    ("environment", "expected"),
    [
        ({}, "off"),
        ({POLICY_ENV: "off"}, "off"),
        ({POLICY_ENV: " SHADOW "}, "shadow"),
        ({POLICY_ENV: "enforce"}, "enforce"),
        ({POLICY_ENV: "unexpected"}, "off"),
        ({POLICY_ENV: ""}, "off"),
    ],
)
def test_policy_mode_supports_safe_rollout_states(environment, expected):
    assert policy_mode(environment) == expected


@pytest.mark.parametrize(
    ("mode", "observes", "enforces"),
    [
        ("off", False, False),
        ("shadow", True, False),
        ("enforce", True, True),
    ],
)
def test_off_shadow_enforce_have_distinct_activation_semantics(
    mode, observes, enforces
):
    policy = resolve_atmospherics_policy("", mode=mode)

    assert policy_observes(policy) is observes
    assert policy_enforces(policy) is enforces


@pytest.mark.parametrize("mode", ["off", "shadow"])
def test_off_and_shadow_observe_without_mutating_provider_prompt(mode):
    prompt = "An empty stage with thick smoke and rolling fog."
    policy = resolve_atmospherics_policy("", mode=mode)

    assert finalize_provider_prompt(prompt, policy, generated=True) == prompt


def test_enforce_sanitizes_generated_prompt_and_appends_negative_rail():
    prompt = "An empty stage with thick smoke, mist and warm haze."
    policy = resolve_atmospherics_policy("", mode="enforce")

    finalized = finalize_provider_prompt(prompt, policy, generated=True)

    assert not atmospheric_terms(finalized.removesuffix(ATMOSPHERIC_NEGATIVE_RAIL))
    assert finalized.endswith(ATMOSPHERIC_NEGATIVE_RAIL)
    assert "clear air" in finalized.lower()


@pytest.mark.parametrize(
    ("prompt", "expected_term"),
    [
        ("Dense smoke crossing an empty warehouse", "smoke"),
        ("A foggy forest with no people", "foggy"),
        ("Niebla cinematográfica sobre el lago", "niebla"),
        ("Una habitación llena de humo azul", "humo"),
        ("Névoa suave cobrindo as montanhas", "névoa"),
        ("Fumaça densa sob luzes vermelhas", "fumaça"),
    ],
)
def test_positive_operator_opt_in_is_recognized_in_english_spanish_portuguese(
    prompt, expected_term
):
    policy = resolve_atmospherics_policy(prompt, mode="enforce")

    assert policy["allow_atmospherics"] is True
    assert expected_term in policy["explicit_atmospherics"]
    assert policy["authorization_source"] == "operator_prompt"


@pytest.mark.parametrize(
    "prompt",
    [
        "No smoke or fog in the scene",
        "A smoke-free studio with crisp visibility",
        "Without haze, use clean hard light",
        "Sin humo ni niebla",
        "Aire libre de humo",
        "Evitar bruma en todo momento",
        "Sem fumaça ou nevoeiro",
        "Não use névoa",
        "Evite vapor na sala",
        "zero smoke",
        "cero humo",
        "nada de niebla",
        "nenhuma fumaça",
        "nenhum nevoeiro",
        "No smoke, fog, haze, mist or steam",
        "Sin humo, niebla, bruma ni vapor",
        "Sem fumaça, névoa ou vapor",
        "Smoke: no",
        "Fog: none",
        "Humo: no",
        "Niebla: ninguna",
        "Fumaça: não",
        "Névoa: nenhuma",
        "Smoke - no",
        "Humo — no",
        "Fumaça – não",
        "Smoke: not allowed",
        "Humo: no permitido",
        "Fumaça: não permitida",
        "Smoke: off",
        "Smoke: false",
        "Smoke: disabled",
        "Humo: desactivado",
        "Fumaça: desativada",
        "Smoke = no",
        "Fog = 0",
        "Niebla: falso",
        "Névoa: falso",
        "Vapor: desabilitado",
    ],
)
def test_negated_atmospheric_terms_do_not_opt_in(prompt):
    policy = resolve_atmospherics_policy(prompt, mode="enforce")

    assert policy["allow_atmospherics"] is False
    assert policy["explicit_atmospherics"] == []
    assert policy["authorization_source"] == "default_deny"


def test_mixed_negative_and_positive_clauses_authorize_only_positive_effect():
    policy = resolve_atmospherics_policy(
        "No fog or haze; use dense blue smoke",
        mode="enforce",
    )

    assert policy["allow_atmospherics"] is True
    assert policy["explicit_atmospherics"] == ["smoke"]


@pytest.mark.parametrize(
    ("prompt", "expected"),
    [
        ("No smoke, but use dense fog", ["fog"]),
        ("Sin humo, pero agrega niebla azul", ["niebla"]),
        ("Sem fumaça, mas use névoa leve", ["névoa"]),
    ],
)
def test_contrast_or_action_resets_a_negated_comma_list(prompt, expected):
    assert resolve_atmospherics_policy(
        prompt, mode="enforce"
    )["explicit_atmospherics"] == expected


@pytest.mark.parametrize(
    ("prompt", "expected"),
    [
        ("No smoke and use fog", ["fog"]),
        ("Sin humo y usa niebla", ["niebla"]),
        ("Sem fumaça e use névoa", ["névoa"]),
    ],
)
def test_positive_action_without_comma_resets_prior_negative(prompt, expected):
    assert resolve_atmospherics_policy(
        prompt, mode="enforce"
    )["explicit_atmospherics"] == expected


def test_people_negation_does_not_negate_later_smoke_clause():
    policy = resolve_atmospherics_policy(
        "No people, smoke moving through an empty room",
        mode="enforce",
    )

    assert policy["allow_atmospherics"] is True
    assert policy["explicit_atmospherics"] == ["smoke"]


@pytest.mark.parametrize(
    "prompt",
    [
        "Smoke: no people",
        "Humo - sin personas",
        "Fumaça = sem pessoas",
    ],
)
def test_negative_status_syntax_does_not_consume_a_different_object(prompt):
    policy = resolve_atmospherics_policy(prompt, mode="enforce")

    assert policy["allow_atmospherics"] is True


def test_stored_authorization_rebases_to_current_rollout_mode():
    stored = resolve_atmospherics_policy("use smoke", mode="shadow")

    rebased = rebase_stored_atmospherics_policy(stored, mode="enforce")

    assert rebased["policy_mode"] == "enforce"
    assert rebased["allow_atmospherics"] is True
    assert rebased["authorization_source"] == "operator_prompt"


def test_legacy_or_malformed_stored_policy_fails_closed():
    assert rebase_stored_atmospherics_policy(
        {"allow_atmospherics": True}, mode="enforce"
    )["allow_atmospherics"] is False
    assert rebase_stored_atmospherics_policy(
        {
            "policy_version": POLICY_VERSION,
            "authorization_source": "operator_prompt",
            "allow_atmospherics": True,
            "explicit_atmospherics": [],
        },
        mode="enforce",
    )["allow_atmospherics"] is False
    assert rebase_stored_atmospherics_policy(
        {
            "policy_version": POLICY_VERSION,
            "authorization_source": "visual_bible",
            "allow_atmospherics": True,
        },
        mode="enforce",
    )["allow_atmospherics"] is False


@pytest.mark.parametrize(
    "prompt",
    [
        "An industrial smokestack at sunset",
        "Un fogón encendido en una cabaña vacía",
        "A mistletoe ornament in sharp focus",
    ],
)
def test_substrings_do_not_create_false_positive_opt_in(prompt):
    assert (
        resolve_atmospherics_policy(prompt, mode="enforce")[
            "allow_atmospherics"
        ]
        is False
    )


def test_only_operator_prompt_is_an_authorization_input():
    signature = inspect.signature(resolve_atmospherics_policy)

    assert set(signature.parameters) == {"operator_prompt", "mode"}
    assert not {
        "artist",
        "title",
        "lyrics",
        "genre",
        "concept",
        "background_hint",
        "scene_context",
        "visual_bible",
    }.intersection(signature.parameters)

    # These values may all contain atmospheric words in a real job, but they
    # are not passed to the policy resolver and therefore cannot authorize it.
    artist = "The Smoke"
    title = "Mist and Fog"
    lyrics = "Humo, niebla y bruma"
    assert artist and title and lyrics
    assert not resolve_atmospherics_policy("", mode="enforce")[
        "allow_atmospherics"
    ]


def test_generated_text_is_sanitized_only_when_default_deny_is_enforced():
    generated = "Slow smoke and steam drift through a misty, hazy room."

    denied = resolve_atmospherics_policy("", mode="enforce")
    opted_in = resolve_atmospherics_policy("add smoke", mode="enforce")
    shadow = resolve_atmospherics_policy("", mode="shadow")

    sanitized = sanitize_generated_text(generated, denied)
    assert not atmospheric_terms(sanitized)
    assert sanitized != generated
    assert sanitize_generated_text(generated, opted_in) == generated
    assert sanitize_generated_text(generated, shadow) == generated


def test_sanitizer_does_not_invert_an_existing_negative_instruction():
    denied = resolve_atmospherics_policy("", mode="enforce")

    sanitized = sanitize_generated_text(
        "Do not introduce smoke, fog or haze. Keep the room crisp.", denied
    ).lower()

    assert "do not introduce clear air" not in sanitized
    assert "keep the air clear" in sanitized


def test_literal_prompt_is_preserved_and_only_the_immutable_rail_is_appended():
    literal = "Locked camera on an empty blue room; crisp reflections!"
    policy = resolve_atmospherics_policy("", mode="enforce")

    finalized = finalize_provider_prompt(literal, policy, generated=False)

    assert finalized == f"{literal}. {ATMOSPHERIC_NEGATIVE_RAIL}"


def test_explicit_opt_in_preserves_literal_without_a_contradictory_rail():
    literal = "Locked camera, smoke rolling through an empty blue room."
    policy = resolve_atmospherics_policy(literal, mode="enforce")

    assert finalize_provider_prompt(literal, policy, generated=False) == literal


@pytest.mark.parametrize(
    ("match_lyrics", "operator_prompt", "verbatim", "expected"),
    [
        (False, "", False, "auto"),
        (True, "", False, "lyrics"),
        (False, "add fog", False, "prompt_improved"),
        (True, "add fog", True, "prompt_literal"),
    ],
)
def test_legacy_wizard_fields_resolve_to_a_stable_creative_mode(
    match_lyrics, operator_prompt, verbatim, expected
):
    assert (
        resolve_creative_mode(
            match_lyrics=match_lyrics,
            operator_prompt=operator_prompt,
            verbatim=verbatim,
        )
        == expected
    )


def test_cache_fingerprint_separates_rollout_mode_and_permission():
    fingerprints = {
        cache_policy_fingerprint(
            resolve_atmospherics_policy(prompt, mode=mode)
        )
        for mode in ("off", "shadow", "enforce")
        for prompt in ("", "use fog")
    }

    assert len(fingerprints) == 6
    assert all(value.startswith(f"{POLICY_VERSION}:") for value in fingerprints)
    assert cache_policy_fingerprint(None) == f"{POLICY_VERSION}:off:deny"


def test_same_policy_has_a_stable_cache_fingerprint():
    first = resolve_atmospherics_policy("use cinematic fog", mode="enforce")
    second = resolve_atmospherics_policy("use cinematic fog", mode="enforce")

    assert cache_policy_fingerprint(first) == cache_policy_fingerprint(second)
