"""Contract-backed Firefly cost configuration.

Adobe prices Firefly Services in Operations and exposes the consumption rate
through the customer's private rate card.  Public Firefly app credits are not
an API price.  GenLy therefore refuses to invent a list price: both contract
values must be configured before a billable Firefly generation is enabled.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP


class FireflyPricingConfigurationError(RuntimeError):
    """The Adobe Operations rate card has not been configured safely."""


@dataclass(frozen=True)
class FireflyVideoRate:
    operations_per_call: Decimal
    usd_per_1000_operations: Decimal
    rate_card_version: str

    @property
    def estimated_cost_usd(self) -> Decimal:
        return (
            self.operations_per_call
            * self.usd_per_1000_operations
            / Decimal("1000")
        ).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)

    def provenance_fields(self) -> dict[str, str]:
        return {
            "operations_reserved": str(self.operations_per_call),
            "usd_per_1000_operations": str(self.usd_per_1000_operations),
            "estimated_cost_usd": str(self.estimated_cost_usd),
            "rate_card_version": self.rate_card_version,
        }


def _positive_decimal(env: Mapping[str, str], key: str) -> Decimal:
    raw = str(env.get(key, "")).strip()
    try:
        value = Decimal(raw)
    except (InvalidOperation, ValueError):
        value = Decimal("0")
    if not value.is_finite() or value <= 0:
        raise FireflyPricingConfigurationError(
            f"{key} must be set from the signed Adobe rate card before "
            "Firefly generation is enabled"
        )
    return value


def load_firefly_video_rate(
    environ: Mapping[str, str] | None = None,
) -> FireflyVideoRate:
    """Load the private contract values required to price one video call."""

    env = environ if environ is not None else os.environ
    version = str(env.get("FIREFLY_RATE_CARD_VERSION", "")).strip()
    if not version:
        raise FireflyPricingConfigurationError(
            "FIREFLY_RATE_CARD_VERSION must identify the signed Adobe rate card"
        )
    return FireflyVideoRate(
        operations_per_call=_positive_decimal(
            env, "FIREFLY_VIDEO_OPERATIONS_PER_CALL"
        ),
        usd_per_1000_operations=_positive_decimal(
            env, "FIREFLY_USD_PER_1000_OPERATIONS"
        ),
        rate_card_version=version,
    )
