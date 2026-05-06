"""Decode base64 vertex credentials env var to a real file at startup.

Hosts like Railway can't mount JSON files directly. The deploy passes the
service-account JSON as a base64-encoded env var
GOOGLE_APPLICATION_CREDENTIALS_JSON_B64; this module decodes it to the path
named by GOOGLE_APPLICATION_CREDENTIALS so the existing Vertex AI client code
finds it without modification.

No-op when either env var is missing, so local dev that already has the file
checked out keeps working.
"""

import base64
import os


def bootstrap_vertex_credentials() -> None:
    b64 = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS_JSON_B64", "").strip()
    path = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", "").strip()
    if not b64 or not path:
        return
    if os.path.exists(path):
        return
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        decoded = base64.b64decode(b64)
        with open(path, "wb") as f:
            f.write(decoded)
        os.chmod(path, 0o600)
        print(f"[bootstrap] wrote vertex credentials to {path} ({len(decoded)} bytes)")
    except Exception as e:
        print(f"[bootstrap] failed to write vertex credentials: {e}")
