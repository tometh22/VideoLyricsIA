"""Encryption at rest for OAuth token blobs (YouTube channel connections).

Uses Fernet (AES-128-CBC + HMAC) with key rotation support: the primary
key comes from TOKEN_ENCRYPTION_KEY and older keys from
TOKEN_ENCRYPTION_KEYS_OLD (comma-separated). MultiFernet encrypts with
the primary key and decrypts with any of them, so rotation is:

  1. Generate a new key:
     python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
  2. Set it as TOKEN_ENCRYPTION_KEY, move the previous one to
     TOKEN_ENCRYPTION_KEYS_OLD. Deploy.
  3. Run scripts/rotate_token_keys.py to re-encrypt every stored blob
     with the new primary.
  4. Remove TOKEN_ENCRYPTION_KEYS_OLD.

Losing every key means every channel must be reconnected — keep the key
in the operator password manager, not only in Railway.
"""

import json
import os

from cryptography.fernet import Fernet, MultiFernet


class TokenCryptoNotConfiguredError(RuntimeError):
    """TOKEN_ENCRYPTION_KEY is missing or invalid."""


def get_fernet() -> MultiFernet:
    primary = os.environ.get("TOKEN_ENCRYPTION_KEY", "").strip()
    if not primary:
        env = os.environ.get("ENVIRONMENT", "development").lower()
        if env in ("production", "prod", "staging"):
            raise TokenCryptoNotConfiguredError(
                "TOKEN_ENCRYPTION_KEY is required to store YouTube channel "
                "tokens. Generate one with: python -c \"from cryptography.fernet "
                "import Fernet; print(Fernet.generate_key().decode())\""
            )
        # Dev/test fallback: a fixed key so local flows work without setup.
        # Never used in prod (guard above).
        primary = "sK6jSPBWKM0BJPHqzD5Vvg0OB0hHFbPRnbAHKN9tW1o="

    keys = [primary] + [
        k.strip() for k in os.environ.get("TOKEN_ENCRYPTION_KEYS_OLD", "").split(",") if k.strip()
    ]
    try:
        return MultiFernet([Fernet(k.encode()) for k in keys])
    except Exception as e:
        raise TokenCryptoNotConfiguredError(f"Invalid Fernet key in TOKEN_ENCRYPTION_KEY(S): {e}")


def encrypt_token(data: dict) -> str:
    """Encrypt a token dict → base64 ciphertext string."""
    return get_fernet().encrypt(json.dumps(data).encode()).decode()


def decrypt_token(blob: str) -> dict:
    """Decrypt a ciphertext string → token dict. Raises InvalidToken on
    tampered/foreign ciphertext."""
    return json.loads(get_fernet().decrypt(blob.encode()).decode())
