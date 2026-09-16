"""Deterministic, tenant-aware video-provider selection.

The selected provider is part of a render's compliance contract.  Callers
must persist :data:`VIDEO_PROVIDER_RENDER_KEY` in ``Job.render_params`` when a
job is created.  Once present, that value always wins over later environment
changes so a retry or edit cannot silently move a customer between vendors.

Firefly tenants are explicit opt-ins.  There is no provider fallback in this
module: an invalid persisted value or configuration raises before a billable
request can be attempted.
"""

from __future__ import annotations

import os
from collections.abc import Mapping


VIDEO_PROVIDER_RENDER_KEY = "video_provider"
VIDEO_PROVIDER_VEO = "veo"
VIDEO_PROVIDER_FIREFLY = "firefly"
SUPPORTED_VIDEO_PROVIDERS = frozenset(
    {VIDEO_PROVIDER_VEO, VIDEO_PROVIDER_FIREFLY}
)


class VideoProviderPolicyError(RuntimeError):
    """Provider policy is missing, invalid, or contradictory."""


def _normalize(value: object) -> str:
    return str(value or "").strip().lower()


def _configured_firefly_tenants(environ: Mapping[str, str]) -> frozenset[str]:
    return frozenset(
        normalized
        for item in environ.get("VIDEO_PROVIDER_FIREFLY_TENANTS", "").split(",")
        if (normalized := _normalize(item))
    )


def _validate_provider(provider: object, *, source: str) -> str:
    normalized = _normalize(provider)
    if normalized not in SUPPORTED_VIDEO_PROVIDERS:
        allowed = ", ".join(sorted(SUPPORTED_VIDEO_PROVIDERS))
        raise VideoProviderPolicyError(
            f"Unsupported {source} video provider {normalized!r}; allowed: {allowed}"
        )
    return normalized


def select_video_provider(
    tenant_id: str | None,
    *,
    render_params: Mapping[str, object] | None = None,
    environ: Mapping[str, str] | None = None,
) -> str:
    """Return the authoritative provider for one job.

    Selection order:

    1. persisted ``render_params.video_provider``;
    2. explicit tenant membership in ``VIDEO_PROVIDER_FIREFLY_TENANTS``;
    3. ``VIDEO_PROVIDER_DEFAULT`` (or legacy ``VIDEO_PROVIDER``);
    4. Veo.

    This keeps existing jobs stable while letting a new tenant be pinned to
    Firefly without changing the default provider for the rest of GenLy.
    """

    params = render_params if isinstance(render_params, Mapping) else {}
    persisted = params.get(VIDEO_PROVIDER_RENDER_KEY)
    if persisted is not None and _normalize(persisted):
        return _validate_provider(persisted, source="persisted")

    env = environ if environ is not None else os.environ
    tenant = _normalize(tenant_id)
    if tenant and tenant in _configured_firefly_tenants(env):
        return VIDEO_PROVIDER_FIREFLY

    configured_default = (
        env.get("VIDEO_PROVIDER_DEFAULT")
        or env.get("VIDEO_PROVIDER")
        or VIDEO_PROVIDER_VEO
    )
    return _validate_provider(configured_default, source="default")


def provider_render_params_patch(provider: str) -> dict[str, str]:
    """Return the small patch callers persist when a job is created."""

    return {
        VIDEO_PROVIDER_RENDER_KEY: _validate_provider(
            provider, source="selected"
        )
    }


def is_firefly_only(
    tenant_id: str | None,
    *,
    render_params: Mapping[str, object] | None = None,
    environ: Mapping[str, str] | None = None,
) -> bool:
    """Whether this job must use Firefly and fail if Firefly is unavailable."""

    return select_video_provider(
        tenant_id,
        render_params=render_params,
        environ=environ,
    ) == VIDEO_PROVIDER_FIREFLY
