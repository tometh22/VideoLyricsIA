import pytest

from video_provider_policy import (
    VIDEO_PROVIDER_FIREFLY,
    VIDEO_PROVIDER_RENDER_KEY,
    VIDEO_PROVIDER_VEO,
    VideoProviderPolicyError,
    is_firefly_only,
    provider_render_params_patch,
    select_video_provider,
)


def test_firefly_tenant_is_explicitly_selected():
    env = {
        "VIDEO_PROVIDER_DEFAULT": "veo",
        "VIDEO_PROVIDER_FIREFLY_TENANTS": "other, Universal_Spain ",
    }

    assert select_video_provider("universal_spain", environ=env) == "firefly"
    assert is_firefly_only("UNIVERSAL_SPAIN", environ=env)


def test_persisted_provider_wins_after_environment_changes():
    params = {VIDEO_PROVIDER_RENDER_KEY: VIDEO_PROVIDER_FIREFLY}
    env = {
        "VIDEO_PROVIDER_DEFAULT": "veo",
        "VIDEO_PROVIDER_FIREFLY_TENANTS": "",
    }

    assert select_video_provider(
        "universal_spain", render_params=params, environ=env
    ) == VIDEO_PROVIDER_FIREFLY


def test_non_firefly_tenant_uses_default_without_implicit_fallback():
    env = {
        "VIDEO_PROVIDER_DEFAULT": "firefly",
        "VIDEO_PROVIDER_FIREFLY_TENANTS": "universal_spain",
    }

    assert select_video_provider("another_tenant", environ=env) == VIDEO_PROVIDER_FIREFLY


def test_legacy_video_provider_setting_remains_compatible():
    assert select_video_provider(
        "tenant", environ={"VIDEO_PROVIDER": "veo"}
    ) == VIDEO_PROVIDER_VEO


@pytest.mark.parametrize(
    ("provider", "source"),
    [("unknown", "default"), ("imagen", "persisted")],
)
def test_invalid_provider_fails_closed(provider, source):
    kwargs = (
        {"render_params": {VIDEO_PROVIDER_RENDER_KEY: provider}, "environ": {}}
        if source == "persisted"
        else {"environ": {"VIDEO_PROVIDER_DEFAULT": provider}}
    )

    with pytest.raises(VideoProviderPolicyError, match=source):
        select_video_provider("tenant", **kwargs)


def test_provider_patch_validates_before_persistence():
    assert provider_render_params_patch(" FIREFLY ") == {
        VIDEO_PROVIDER_RENDER_KEY: VIDEO_PROVIDER_FIREFLY
    }
    with pytest.raises(VideoProviderPolicyError):
        provider_render_params_patch("automatic")
