"""Stable, privacy-minimizing identities for the rolling Veo budget."""

import hashlib


def scope_hash(tenant_id: str | None, song_identity: str) -> str:
    """Hash the tenant/song scope without retaining catalogue metadata.

    Deletable jobs must not reset the paid-call ceiling, but keeping artist
    and title after a hard delete would defeat the user's deletion intent.
    The ledger therefore stores only this deterministic one-way identifier.
    """
    scope = f"{(tenant_id or '').strip()}\0{song_identity}".encode("utf-8")
    return hashlib.sha256(scope).hexdigest()


def advisory_lock_key(scope_digest: str) -> int:
    """Signed 64-bit key accepted by PostgreSQL advisory locks."""
    return int.from_bytes(
        bytes.fromhex(scope_digest)[:8],
        byteorder="big",
        signed=True,
    )
