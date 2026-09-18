from decimal import Decimal

import pytest

from firefly_pricing import (
    FireflyPricingConfigurationError,
    load_firefly_video_rate,
)


def test_contract_rate_calculates_one_call_cost():
    rate = load_firefly_video_rate(
        {
            "FIREFLY_VIDEO_OPERATIONS_PER_CALL": "120",
            "FIREFLY_USD_PER_1000_OPERATIONS": "7.50",
            "FIREFLY_RATE_CARD_VERSION": "adobe-order-2026-09",
        }
    )

    assert rate.estimated_cost_usd == Decimal("0.9000")
    assert rate.provenance_fields() == {
        "operations_reserved": "120",
        "usd_per_1000_operations": "7.50",
        "estimated_cost_usd": "0.9000",
        "rate_card_version": "adobe-order-2026-09",
    }


@pytest.mark.parametrize(
    "missing_key",
    [
        "FIREFLY_VIDEO_OPERATIONS_PER_CALL",
        "FIREFLY_USD_PER_1000_OPERATIONS",
        "FIREFLY_RATE_CARD_VERSION",
    ],
)
def test_missing_contract_value_blocks_generation_pricing(missing_key):
    env = {
        "FIREFLY_VIDEO_OPERATIONS_PER_CALL": "120",
        "FIREFLY_USD_PER_1000_OPERATIONS": "7.50",
        "FIREFLY_RATE_CARD_VERSION": "adobe-order-2026-09",
    }
    env.pop(missing_key)

    with pytest.raises(FireflyPricingConfigurationError):
        load_firefly_video_rate(env)


@pytest.mark.parametrize("value", ["0", "-1", "NaN", "unknown"])
def test_invalid_operation_price_is_rejected(value):
    with pytest.raises(FireflyPricingConfigurationError):
        load_firefly_video_rate(
            {
                "FIREFLY_VIDEO_OPERATIONS_PER_CALL": value,
                "FIREFLY_USD_PER_1000_OPERATIONS": "7.50",
                "FIREFLY_RATE_CARD_VERSION": "v1",
            }
        )
