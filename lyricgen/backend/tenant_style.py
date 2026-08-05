"""Per-account visual style defaults (tenant / billing group).

Why this exists
---------------
UMG left 31 change requests on the deliveries portal between May and August
2026.  Nine of them (29%) were not defects at all — they were the *same two
preferences*, restated video after video, because nothing on our side
remembered them:

  - "sacarle los puntos al final de cada frase porfa"        × 6
  - "poner la tipografía un toque más grande"                 × 3

By 2026-08-04 the tone had moved from a request to a reminder ("Porfa, que
las letras no tengan puntos").  Meanwhile the operator was compensating by
hand: 38 of the 56 live deliveries had ``font_scale`` set manually, and
inconsistently (24 at 1.3, 14 at 1.15, 18 at 1.0).

This module is the missing memory.  A profile is a **sparse** set of
overrides — absent keys mean "no opinion, use the platform default" — so a
row never has to restate the whole render contract just to express one
preference.

Relationship to ``batch_profiles``
----------------------------------
``batch_profiles.normalize_render_profile`` validates a *complete* visual
contract for controlled batches: it requires ``font``, and rejects any
``font_scale`` outside ``{1.0, 1.3}``.  That is deliberately stricter than
what an account default needs to express, so this module keeps its own
(narrow) validator rather than bending the batch contract.  The two are
complementary: a batch profile still wins, because it is an explicit
per-request choice.

Resolution order
----------------
``explicit request → tenant → billing_group → platform default``

The explicit-request level is handled by the callers (the upload/generate
endpoints), which only fall back here when the operator did not choose.
The tenant/billing-group levels live in ``tenant_style_profiles``; the
billing-group scope exists because Universal spans five tenants
(``pipeline.UMG_TENANTS``) under a single ``billing_group``, so one row can
cover all of them.  Same precedence shape as
``pipeline._is_universal_account``.
"""

from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger("genly.tenant_style")

SCOPE_TENANT = "tenant"
SCOPE_BILLING_GROUP = "billing_group"
ALLOWED_SCOPES = frozenset({SCOPE_TENANT, SCOPE_BILLING_GROUP})

# Narrow on purpose.  Movement ceiling and background rules are the obvious
# next two keys, but 8/8 of UMG's background requests asked for something
# subtly different, so they need their own review with visual samples rather
# than a boolean.  Adding a key here is cheap; removing one is not.
ALLOWED_KEYS = frozenset({"strip_trailing_punctuation", "font_scale"})

# Same clamp the upload/generate endpoints already apply to the form field,
# and the same one `ass_render.lyric_fontsize` enforces internally.
FONT_SCALE_MIN = 0.6
FONT_SCALE_MAX = 1.5


class StyleProfileError(ValueError):
    """Raised when a stored/submitted style profile is not usable."""


def normalize_style_profile(value: str | dict[str, Any] | None) -> dict[str, Any]:
    """Parse and validate a sparse style profile.

    Returns a canonical, JSON-safe dict containing **only** the keys that
    were actually set.  ``None``/empty input yields ``{}`` (no opinion).
    Raises :class:`StyleProfileError` on unknown keys or bad values.
    """
    if value is None or value == "":
        return {}
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise StyleProfileError("style profile must be valid JSON") from exc
    if not isinstance(value, dict):
        raise StyleProfileError("style profile must be an object")

    unknown = sorted(set(value) - ALLOWED_KEYS)
    if unknown:
        raise StyleProfileError(
            f"unsupported style profile keys: {', '.join(unknown)}"
        )

    out: dict[str, Any] = {}

    if "strip_trailing_punctuation" in value:
        raw = value["strip_trailing_punctuation"]
        if isinstance(raw, bool):
            out["strip_trailing_punctuation"] = raw
        elif isinstance(raw, str):
            out["strip_trailing_punctuation"] = raw.strip().lower() in (
                "1", "true", "yes", "on",
            )
        else:
            raise StyleProfileError("strip_trailing_punctuation must be a boolean")

    if "font_scale" in value:
        try:
            scale = float(value["font_scale"])
        except (TypeError, ValueError) as exc:
            raise StyleProfileError("font_scale must be numeric") from exc
        if not (FONT_SCALE_MIN <= scale <= FONT_SCALE_MAX):
            raise StyleProfileError(
                f"font_scale must be between {FONT_SCALE_MIN} and {FONT_SCALE_MAX}"
            )
        out["font_scale"] = scale

    return out


def resolve_style_profile(
    db,
    *,
    tenant_id: str | None = None,
    billing_group: str | None = None,
) -> dict[str, Any]:
    """Resolve the effective account profile for a job about to be created.

    Never raises: a malformed stored row is logged and skipped rather than
    failing the upload.  A profile is a *preference*, so a bad row must
    degrade to platform defaults, not to a 500.
    """
    # Imported here so this module stays importable from tests and scripts
    # without dragging in the SQLAlchemy engine at module load.
    from database import TenantStyleProfile

    candidates: list[tuple[str, str]] = []
    if tenant_id:
        candidates.append((SCOPE_TENANT, str(tenant_id)))
    if billing_group:
        candidates.append((SCOPE_BILLING_GROUP, str(billing_group)))
    if not candidates:
        return {}

    try:
        rows = (
            db.query(TenantStyleProfile)
            .filter(
                TenantStyleProfile.scope.in_([c[0] for c in candidates]),
                TenantStyleProfile.scope_key.in_([c[1] for c in candidates]),
            )
            .all()
        )
    except Exception as e:  # pragma: no cover — DB hiccup must not block uploads
        logger.warning("[STYLE] lookup falló (%s) — defaults de plataforma", e)
        return {}

    by_scope = {(r.scope, r.scope_key): r for r in rows}
    # Most specific first; the first hit wins outright (no key-level merge —
    # a tenant row is a complete statement of that tenant's preferences).
    for scope, key in candidates:
        row = by_scope.get((scope, key))
        if row is None:
            continue
        try:
            return normalize_style_profile(row.profile)
        except StyleProfileError as e:
            logger.warning(
                "[STYLE] perfil inválido en %s=%s (%s) — ignorado", scope, key, e,
            )
    return {}


def strip_trailing_punctuation(text: str) -> str:
    """Drop sentence-final punctuation from a rendered lyric line.

    Only trailing ``.`` / ``…`` / ``,`` / ``;`` / ``:`` are removed, and only
    at the very end of the line.  Deliberately **not** removed:

      - ``?`` and ``!`` (and their Spanish opening marks) — they carry
        meaning UMG never asked us to drop, and losing them would change how
        a line reads.
      - anything mid-line — "No, no puedo" keeps its comma.

    A line that is nothing but punctuation is returned unchanged rather than
    emptied, so a stylistic "..." card does not silently disappear.
    """
    if not text:
        return text
    stripped = text.rstrip()
    if not stripped:
        return text
    trailing_ws = text[len(stripped):]
    cleaned = stripped.rstrip(".,;:…")
    if not cleaned:
        return text
    return cleaned + trailing_ws
