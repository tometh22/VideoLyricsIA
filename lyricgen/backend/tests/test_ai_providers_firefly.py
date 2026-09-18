from dataclasses import dataclass
from decimal import Decimal

import pytest

from ai_providers import FireflyVideoProvider, get_video_provider
from firefly_pricing import FireflyVideoRate
from firefly_pricing import FireflyPricingConfigurationError


@dataclass
class _Result:
    provider_job_id: str = "urn:ff:jobs:test:1"
    model_version: str = "video1_standard"
    width: int = 1920
    height: int = 1080
    seed: int = 123
    output_sha256: str = "a" * 64
    output_bytes: int = 42


class _Client:
    def __init__(self):
        self.calls = []

    def generate_video(self, prompt, output_path, **kwargs):
        self.calls.append((prompt, output_path, kwargs))
        return _Result(width=kwargs["width"], height=kwargs["height"])


def _rate():
    return FireflyVideoRate(
        operations_per_call=Decimal("120"),
        usd_per_1000_operations=Decimal("7.50"),
        rate_card_version="test-rate-card",
    )


def test_registry_exposes_firefly_without_initializing_credentials():
    provider = get_video_provider("firefly")

    assert isinstance(provider, FireflyVideoProvider)
    assert provider.get_model_version() == "video1_standard"


@pytest.mark.parametrize(
    ("aspect_ratio", "dimensions"),
    [("16:9", (1920, 1080)), ("9:16", (1080, 1920)), ("1:1", (1080, 1080))],
)
def test_firefly_provider_maps_supported_aspect_ratios(aspect_ratio, dimensions):
    client = _Client()
    provider = FireflyVideoProvider(client=client, rate=_rate())

    path = provider.generate_video(
        "A slow cinematic landscape",
        "/tmp/firefly.mp4",
        aspect_ratio=aspect_ratio,
    )

    assert path == "/tmp/firefly.mp4"
    assert client.calls == [
        (
            "A slow cinematic landscape",
            "/tmp/firefly.mp4",
            {"width": dimensions[0], "height": dimensions[1]},
        )
    ]


def test_firefly_provider_rejects_unknown_aspect_ratio_before_call():
    client = _Client()

    with pytest.raises(ValueError, match="Unsupported Firefly aspect ratio"):
        FireflyVideoProvider(client=client, rate=_rate()).generate_video(
            "Landscape", "/tmp/firefly.mp4", aspect_ratio="4:3"
        )

    assert client.calls == []


def test_firefly_provider_requires_contract_rate_before_billable_call(monkeypatch):
    client = _Client()
    for key in (
        "FIREFLY_VIDEO_OPERATIONS_PER_CALL",
        "FIREFLY_USD_PER_1000_OPERATIONS",
        "FIREFLY_RATE_CARD_VERSION",
    ):
        monkeypatch.delenv(key, raising=False)

    with pytest.raises(FireflyPricingConfigurationError):
        FireflyVideoProvider(client=client).generate_video(
            "Landscape", "/tmp/firefly.mp4"
        )

    assert client.calls == []
