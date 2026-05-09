"""Regression test: production guards must honor ENVIRONMENT as well as ENV."""

import importlib
import os
import sys

import pytest


def _reload_auth(monkeypatch, **env):
    for k in ("ENV", "ENVIRONMENT", "JWT_SECRET"):
        monkeypatch.delenv(k, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    sys.modules.pop("auth", None)
    return importlib.import_module("auth")


def test_jwt_guard_triggers_with_environment_production(monkeypatch):
    with pytest.raises(RuntimeError, match="JWT_SECRET must be set"):
        _reload_auth(monkeypatch, ENVIRONMENT="production", JWT_SECRET="genly-default-secret-change-me")


def test_jwt_guard_does_not_trigger_in_dev(monkeypatch):
    mod = _reload_auth(monkeypatch, ENVIRONMENT="dev", JWT_SECRET="genly-default-secret-change-me")
    assert mod.JWT_SECRET == "genly-default-secret-change-me"
