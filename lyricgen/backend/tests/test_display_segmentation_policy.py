import base64
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import json

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PrivateFormat, PublicFormat, NoEncryption
import pytest

import display_segmentation_policy as policy
from evidence_attestation import sign_artifact
from transcription_quality import runtime_identity


@pytest.fixture
def authorization(monkeypatch, tmp_path):
    monkeypatch.setenv("DISPLAY_SEGMENTATION_MODE", "enforce")
    private = Ed25519PrivateKey.generate()
    private_b64 = base64.b64encode(private.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption())).decode()
    public_b64 = base64.b64encode(private.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)).decode()
    monkeypatch.setenv("DISPLAY_SEGMENTATION_PUBLIC_KEYS", json.dumps({"test": public_b64}))
    identity = runtime_identity()
    artifact = {
        "schema": policy.SCHEMA, "policy_version": "display-phrases-v1", "decision": "GO",
        "pipeline_release": identity["pipeline_release"],
        "pipeline_config_fingerprint": identity["pipeline_config_fingerprint"],
        "expires_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
        "checks": {key: True for key in policy.REQUIRED_CHECKS},
        "boundary_validation": {"reviewed": 539, "precision_lower_95": 0.995,
                                "damaging": 0, "holdout_songs": 20},
    }
    path = tmp_path / "authorization.json"
    monkeypatch.setenv("DISPLAY_SEGMENTATION_AUTHORIZATION_PATH", str(path))

    def write(value=None, *, sign=True):
        raw = json.dumps(sign_artifact(value or artifact, private_b64, "test") if sign else value or artifact).encode()
        path.write_bytes(raw)
        monkeypatch.setenv("DISPLAY_SEGMENTATION_AUTHORIZATION_SHA256", hashlib.sha256(raw).hexdigest())
    write()
    return artifact, write


def test_separate_signed_grant_required_and_bound_to_release_config(monkeypatch, authorization):
    assert policy.runtime_authorization()["authorized"]
    monkeypatch.setenv("PHRASE_SEG_MAX_LEN", "7")
    assert policy.runtime_authorization()["status"] == "authorization_runtime_mismatch"


@pytest.mark.parametrize("alter", [
    lambda a: a.update(policy_version="lyrics-quality-v6"),
    lambda a: a.update(decision="NO_GO"),
    lambda a: a.update(expires_at="2020-01-01T00:00:00Z"),
    lambda a: a.update(expires_at="not-a-date"),
    lambda a: a["checks"].update(song_holdout=False),
    lambda a: a["boundary_validation"].update(reviewed=0),
    lambda a: a["boundary_validation"].update(precision_lower_95=float("nan")),
    lambda a: a["boundary_validation"].update(damaging=1),
    lambda a: a["boundary_validation"].update(holdout_songs=0),
    lambda a: a.update(pipeline_release="other-release"),
])
def test_bad_or_expired_grant_never_authorizes(authorization, alter):
    artifact, write = authorization
    altered = deepcopy(artifact)
    alter(altered)
    write(altered)
    assert not policy.runtime_authorization()["authorized"]


def test_unsigned_and_changed_file_rejected(authorization, monkeypatch):
    artifact, write = authorization
    write(artifact, sign=False)
    assert policy.runtime_authorization()["status"] == "authorization_untrusted"
    write()
    monkeypatch.setenv("DISPLAY_SEGMENTATION_AUTHORIZATION_SHA256", "0" * 64)
    assert policy.runtime_authorization()["status"] == "authorization_hash_mismatch"


def test_shadow_never_reads_authorization(monkeypatch):
    monkeypatch.setenv("DISPLAY_SEGMENTATION_MODE", "shadow")
    monkeypatch.setenv("DISPLAY_SEGMENTATION_AUTHORIZATION_PATH", "/unreadable")
    assert policy.runtime_authorization() == {"authorized": False, "status": "not_requested"}


def test_runtime_fingerprint_covers_new_and_legacy_segmentation_knobs(monkeypatch):
    baseline = runtime_identity()["pipeline_config_fingerprint"]
    for key, value in [("DISPLAY_SEGMENTATION_MODE", "off"), ("PHRASE_SEG_MAX_LEN", "6"),
                       ("PHRASE_SEG_TARGET_LEN", "5"), ("LLM_SEGMENT_ENABLED", "0"),
                       ("QUALITY_LINE_ASR_SCORE_MIN", "0.6")]:
        with monkeypatch.context() as m:
            m.setenv(key, value)
            assert runtime_identity()["pipeline_config_fingerprint"] != baseline
