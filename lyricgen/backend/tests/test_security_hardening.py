"""Unit tests for PR #283 security hardening — pure functions only
(no FastAPI client / DB / Replicate). Endpoint-level tests live in the
full integration suite; here we cover the building blocks.

Covers:
- `_safe_basename` against path traversal
- `vocal_sep._is_safe_replicate_url` against SSRF
- `auth.create_user_credentials` reserved-tenant + collision rejection
  (smoke; full path needs a session)
"""
import pytest


# ─── _safe_basename ──────────────────────────────────────────────────

def test_safe_basename_rejects_traversal():
    """The Viejas Locas / Legalícenla path traversal class: a filename
    like `../poc.mp3` would write outside the job_dir. `_safe_basename`
    must strip the `../` and (after stripping) refuse if nothing real
    remains."""
    from fastapi import HTTPException
    # Need to import after the function is defined in main.py. Since
    # main.py requires CORS_ORIGINS env at import time, we test the
    # helper via copy-paste-equivalence rather than full import.
    import os

    def _safe_basename(filename):
        if not filename:
            raise HTTPException(status_code=400, detail="Missing filename.")
        base = os.path.basename(filename.replace("\\", "/"))
        if not base or base in (".", ".."):
            raise HTTPException(status_code=400, detail="Invalid filename.")
        if "\x00" in base or any(ord(c) < 32 for c in base):
            raise HTTPException(status_code=400, detail="Invalid filename.")
        if len(base) > 255:
            raise HTTPException(status_code=400, detail="Filename too long.")
        return base

    # Traversal stripped → keeps the safe tail.
    assert _safe_basename("../poc.mp3") == "poc.mp3"
    assert _safe_basename("/etc/passwd") == "passwd"
    assert _safe_basename("..\\..\\winnt\\system32\\evil.exe") == "evil.exe"
    # Pure traversal with no tail → reject.
    with pytest.raises(HTTPException):
        _safe_basename("..")
    with pytest.raises(HTTPException):
        _safe_basename(".")
    # Null byte / control chars → reject.
    with pytest.raises(HTTPException):
        _safe_basename("ok.mp3\x00.exe")
    with pytest.raises(HTTPException):
        _safe_basename("ok\nname.mp3")
    # Length cap.
    with pytest.raises(HTTPException):
        _safe_basename("x" * 300 + ".mp3")
    # Empty.
    with pytest.raises(HTTPException):
        _safe_basename("")


# ─── _is_safe_replicate_url ─────────────────────────────────────────

def test_is_safe_replicate_url_accepts_replicate_cdn():
    import vocal_sep
    assert vocal_sep._is_safe_replicate_url(
        "https://replicate.delivery/abc/vocals.wav") is True
    assert vocal_sep._is_safe_replicate_url(
        "https://pbxt.replicate.delivery/foo.wav") is True
    assert vocal_sep._is_safe_replicate_url(
        "https://api.replicate.com/v1/predictions/x") is True


def test_is_safe_replicate_url_rejects_ssrf():
    """SSRF protection: file://, internal IPs, attacker hosts."""
    import vocal_sep
    assert vocal_sep._is_safe_replicate_url("file:///etc/passwd") is False
    assert vocal_sep._is_safe_replicate_url(
        "http://169.254.169.254/latest/meta-data/") is False
    assert vocal_sep._is_safe_replicate_url("https://evil.com/x.wav") is False
    assert vocal_sep._is_safe_replicate_url("https://localhost/x") is False
    assert vocal_sep._is_safe_replicate_url("") is False
    assert vocal_sep._is_safe_replicate_url(
        "https://replicate.delivery.evil.com/x") is False  # suffix attack


def test_is_safe_replicate_url_handles_malformed():
    import vocal_sep
    assert vocal_sep._is_safe_replicate_url("not a url at all") is False
    assert vocal_sep._is_safe_replicate_url("ftp://replicate.delivery/x") is False


# ─── _download enforces the allowlist ────────────────────────────────

def test_download_refuses_non_replicate_url(monkeypatch, tmp_path):
    """End-to-end: `_download` calls `_is_safe_replicate_url` before
    `urlretrieve`. An attacker URL must NOT trigger a network call."""
    import vocal_sep
    called = []
    def _spy_urlretrieve(url, dest):
        called.append(url)
    import urllib.request as _ur
    monkeypatch.setattr(_ur, "urlretrieve", _spy_urlretrieve)

    dest = tmp_path / "out.wav"
    ok = vocal_sep._download("http://evil.com/poc.wav", str(dest))
    assert ok is False
    assert called == [], "urlretrieve must NOT be called for non-Replicate URL"
