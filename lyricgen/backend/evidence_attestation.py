"""Detached Ed25519 receipts for benchmark and operational evidence.

Verification keys are supplied by the trusted runner as a JSON object mapping
key IDs to base64-encoded raw Ed25519 public keys. Private keys never belong in
the repository, manifest, benchmark fixture, or application database.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)


def canonical_json(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")


def unsigned_payload(artifact: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in artifact.items() if key != "attestation"}


def lyric_snapshot_hash(segments: Any, *, include_event_type: bool = False) -> str:
    """Canonical content/timing hash shared by editor evidence and benchmark."""
    canonical = []
    for item in (segments or []):
        if not isinstance(item, dict):
            continue
        row = {
            "start": round(float(item.get("start") or 0), 6),
            "end": round(float(item.get("end") or 0), 6),
            "text": str(item.get("text") or ""),
        }
        if include_event_type:
            row["event_type"] = str(item.get("event_type") or "")
        canonical.append(row)
    return hashlib.sha256(canonical_json(canonical)).hexdigest()


def sign_artifact(artifact: dict[str, Any], private_key_b64: str,
                  key_id: str) -> dict[str, Any]:
    """Return a copy with a deterministic detached Ed25519 receipt."""
    unsigned = unsigned_payload(artifact)
    payload = canonical_json(unsigned)
    private_key = Ed25519PrivateKey.from_private_bytes(
        base64.b64decode(private_key_b64, validate=True)
    )
    signed = dict(unsigned)
    signed["attestation"] = {
        "algorithm": "Ed25519",
        "key_id": str(key_id),
        "payload_sha256": hashlib.sha256(payload).hexdigest(),
        "signature": base64.b64encode(private_key.sign(payload)).decode("ascii"),
    }
    return signed


def verify_artifact(artifact: Any, public_keys_env: str) -> tuple[bool, str]:
    """Verify an artifact against a runner-owned allow-list of public keys."""
    if not isinstance(artifact, dict):
        return False, "artifact is not an object"
    receipt = artifact.get("attestation")
    if not isinstance(receipt, dict) or receipt.get("algorithm") != "Ed25519":
        return False, "missing Ed25519 attestation"
    key_id = str(receipt.get("key_id") or "")
    try:
        configured = json.loads(os.environ.get(public_keys_env, ""))
    except json.JSONDecodeError:
        return False, f"{public_keys_env} is not valid JSON"
    if not isinstance(configured, dict) or key_id not in configured:
        return False, f"untrusted key_id for {public_keys_env}"
    unsigned = unsigned_payload(artifact)
    payload = canonical_json(unsigned)
    if receipt.get("payload_sha256") != hashlib.sha256(payload).hexdigest():
        return False, "attested payload hash mismatch"
    try:
        public_raw = base64.b64decode(str(configured[key_id]), validate=True)
        signature = base64.b64decode(str(receipt.get("signature") or ""), validate=True)
        Ed25519PublicKey.from_public_bytes(public_raw).verify(signature, payload)
    except (ValueError, TypeError, InvalidSignature):
        return False, "invalid Ed25519 signature"
    return True, "verified"


def write_json_exclusive(path: Path, payload: Any) -> None:
    """Durably publish JSON without overwrite or a partially-written final."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_name = ""
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent,
            prefix=f".{path.name}.", suffix=".tmp", delete=False,
        ) as handle:
            temporary_name = handle.name
            handle.write(json.dumps(
                payload, ensure_ascii=False, indent=2, sort_keys=True,
            ) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        # A hard link publishes atomically and fails if the immutable target
        # already exists. Rename alone would overwrite on POSIX.
        os.link(temporary_name, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if temporary_name:
            try:
                os.unlink(temporary_name)
            except FileNotFoundError:
                pass
