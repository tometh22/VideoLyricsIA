"""Generate the static delivery portal shell.

Why this exists (v2):
  As of 2026-05, the portal fetches its listing dynamically from the
  GenLy backend (`/api/deliveries/items`). This script no longer signs
  R2 URLs per video — that happens server-side, on demand, with a
  60-second cache. What this script DOES still do:

    1. Embed the password hash + salt so the portal can verify entry
       client-side without round-tripping to the backend.
    2. Embed the static title/description that the API can't tailor
       (the API returns the listing only, not the page chrome copy).
    3. Build dist/index.html and prepare for `vercel --prod`.

  So now you run this ONCE per portal redesign (changing the HTML/CSS
  or rotating the password), not once per content update. Admins
  publish/delete videos from the main app and the portal picks up
  changes on the next page load — no redeploy needed.

Usage:
  cd /Users/tomi/genly-deliveries
  DELIVERY_PASSWORD=<the-shared-password> python gen_page.py

Env required:
  DELIVERY_PASSWORD — the shared password Universal types in. The hash
    of this is embedded; the plaintext is also what the portal sends
    as X-Portal-Token, so the API's `DELIVERY_PORTAL_TOKEN` env var
    on Railway MUST match this value verbatim.

Output:
  dist/index.html — single-file static shell ready for `vercel --prod`.
"""
from __future__ import annotations

import hashlib
import json
import os
import secrets
import sys
from pathlib import Path


HERE = Path(__file__).parent
DIST = HERE / "dist"
TEMPLATE_PATH = HERE / "index.template.html"

DELIVERY_PASSWORD = os.environ.get("DELIVERY_PASSWORD", "")
PRESERVE_FROM = os.environ.get("PORTAL_SHELL_SOURCE", "")

if not DELIVERY_PASSWORD and not PRESERVE_FROM:
    print("ERROR: DELIVERY_PASSWORD or PORTAL_SHELL_SOURCE required.", file=sys.stderr)
    sys.exit(1)


def _embedded_json(shell: str, name: str):
    marker = f"const {name} = "
    start = shell.find(marker)
    if start < 0:
        raise ValueError(f"{name} missing from existing portal shell")
    return json.JSONDecoder().raw_decode(shell[start + len(marker):])[0]


def main():
    # Password hash. Salt rotates per build so a screenshot of an old
    # build's hash can't be re-used against the new portal. Verifying
    # is client-side (sha256(salt + input) vs hash) — the gate is good
    # enough for "not indexable, not casually browseable"; the API
    # behind it is still a separate auth check.
    previous = Path(PRESERVE_FROM).read_text(encoding="utf-8") if PRESERVE_FROM else None
    if previous is not None:
        old_auth = _embedded_json(previous, "PAGE_DATA")
        salt, digest = old_auth["salt"], old_auth["expected_hash"]
        if not (isinstance(salt, str) and isinstance(digest, str)
                and len(salt) == 32 and len(digest) == 64
                and all(ch in "0123456789abcdef" for ch in salt + digest)):
            raise ValueError("Existing portal authentication data is invalid")
    else:
        salt = secrets.token_hex(16)
        digest = hashlib.sha256((salt + DELIVERY_PASSWORD).encode("utf-8")).hexdigest()

    # The shell only carries the page chrome + auth bits. Songs come
    # from /api/deliveries/items at runtime, not from this script.
    page_data = {
        "title": "Entregables — GenLy AI",
        "description": (
            "Videos y Art Tracks generados con GenLy AI. Bajo cada canción "
            "encontrarás todos los formatos disponibles (ProRes master para "
            "broadcast, MP4 para web/YouTube, Short vertical y Thumbnail). "
            "Cuando una canción ofrece más de una versión, elegí la que mejor "
            "se adapte a tu uso."
        ),
        "salt": salt,
        "expected_hash": digest,
    }

    # Help center content (FAQs + tutoriales + glosario). Lives in
    # help_content.py so non-devs can edit copy without touching this script.
    if previous is not None:
        HELP_CONTENT = _embedded_json(previous, "HELP_CONTENT")
    else:
        try:
            from help_content import HELP_CONTENT
        except ModuleNotFoundError:
            # Some deployment checkouts omit the optional help-content module.
            # Keep the portal deployable; the help drawer handles empty content.
            HELP_CONTENT = {"categories": [], "articles": []}

    template = TEMPLATE_PATH.read_text(encoding="utf-8")
    rendered = template.replace(
        "/*__PAGE_DATA__*/null",
        json.dumps(page_data, ensure_ascii=False, indent=2),
    ).replace(
        "/*__HELP_CONTENT__*/null",
        json.dumps(HELP_CONTENT, ensure_ascii=False, indent=2),
    )

    DIST.mkdir(exist_ok=True)
    out_path = DIST / "index.html"
    out_path.write_text(rendered, encoding="utf-8")

    print(f"✓ Generated {out_path}")
    print(f"  Auth shell only — listing fetched from /api/deliveries/items at runtime.")
    print()
    print("Next steps:")
    print(f"  1. Make sure DELIVERY_PORTAL_TOKEN on the Railway api service equals the password.")
    print(f"  2. cd {HERE} && vercel --prod")
    print(f"  3. vercel alias set <deployment-url> umg.genly.pro")


if __name__ == "__main__":
    main()
