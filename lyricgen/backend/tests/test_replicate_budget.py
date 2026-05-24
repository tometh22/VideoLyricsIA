"""Tests for the shared `replicate_budget.call_with_budget` helper.

The QA audit revealed that whisperX and vocal_sep had the same unbounded
retry bug that PR #281 fixed only for forced_align. PR #284 (this PR)
extracts the helper to a shared module and re-applies it to both. These
tests verify the extracted helper preserves the PR #281 invariants:

- happy path: returns model output on first attempt
- retry path: transient errors trigger sleep + retry within budget
- abort path: non-retryable errors short-circuit immediately
- budget path: total wall-clock cap enforced
- `is_non_retryable`: matches the deterministic-failure fragments
"""
import time

import pytest

from replicate_budget import (
    call_with_budget,
    is_non_retryable,
    NON_RETRYABLE_FRAGMENTS,
    _budget_for,
)


def test_is_non_retryable_matches_padding_bug():
    """The `[1, 2, 1]` cureau tensor crash (Bug C from PR #281 audit)."""
    err = RuntimeError(
        "Argument #4: Padding size should be less than the corresponding "
        "input dimension, but got: padding (200, 200) at dimension 2"
    )
    assert is_non_retryable(err) is True


def test_is_non_retryable_matches_validation():
    assert is_non_retryable(ValueError("ValidationError on transcript"))
    assert is_non_retryable(RuntimeError("Invalid input: transcript too long"))
    assert is_non_retryable(RuntimeError("Authentication required"))


def test_is_retryable_for_transient():
    assert is_non_retryable(
        ConnectionError("Server disconnected without sending a response.")
    ) is False
    assert is_non_retryable(TimeoutError("read timed out")) is False
    assert is_non_retryable(RuntimeError("HTTP 429 burst of 1")) is False


def test_call_with_budget_happy_path(monkeypatch):
    monkeypatch.setattr("replicate.run", lambda *a, **kw: {"ok": True})
    out = call_with_budget("m", lambda: {}, total_budget_s=10,
                            backoff=[0, 8, 24], call_label="TEST")
    assert out == {"ok": True}


def test_call_with_budget_retries_then_succeeds(monkeypatch):
    attempts = [0]
    def _flaky(*a, **kw):
        attempts[0] += 1
        if attempts[0] == 1:
            raise ConnectionError("Server disconnected")
        return {"ok": True}
    monkeypatch.setattr("replicate.run", _flaky)
    out = call_with_budget("m", lambda: {}, total_budget_s=30,
                            backoff=[0, 0], call_label="TEST")
    assert out == {"ok": True}


def test_call_with_budget_aborts_on_non_retryable(monkeypatch):
    """Padding-size error MUST abort attempt 1, no sleeps."""
    calls = []
    def _crash(*a, **kw):
        calls.append(1)
        raise RuntimeError("Padding size should be less than ...")
    monkeypatch.setattr("replicate.run", _crash)
    t0 = time.monotonic()
    out = call_with_budget("m", lambda: {}, total_budget_s=480,
                            backoff=[0, 8, 24], call_label="TEST")
    elapsed = time.monotonic() - t0
    assert out is None
    assert len(calls) == 1
    assert elapsed < 1.0


def test_call_with_budget_caps_wallclock(monkeypatch):
    """Bug E regression: total budget enforced even with transient
    errors that would normally retry."""
    def _slow_fail(*a, **kw):
        time.sleep(0.4)
        raise ConnectionError("Server disconnected")
    monkeypatch.setattr("replicate.run", _slow_fail)
    t0 = time.monotonic()
    out = call_with_budget("m", lambda: {}, total_budget_s=1.0,
                            backoff=[0, 0, 0, 0, 0], call_label="TEST")
    elapsed = time.monotonic() - t0
    assert out is None
    assert elapsed < 1.6


def test_budget_for_env_override(monkeypatch):
    monkeypatch.setenv("REPLICATE_BUDGET_S_TEST", "120")
    assert _budget_for("test", default_s=480) == 120.0
    # Bad value falls back to default.
    monkeypatch.setenv("REPLICATE_BUDGET_S_TEST", "not-a-number")
    assert _budget_for("test", default_s=480) == 480.0


def test_input_factory_called_fresh(monkeypatch):
    """File handles consumed on upload → factory must be re-called each
    attempt to reopen."""
    factory_calls = [0]
    def _factory():
        factory_calls[0] += 1
        return {"id": factory_calls[0]}
    def _spy(model, input):
        if factory_calls[0] < 3:
            raise ConnectionError("transient")
        return {"ok": True}
    monkeypatch.setattr("replicate.run", _spy)
    out = call_with_budget("m", _factory, total_budget_s=10,
                            backoff=[0, 0, 0], call_label="TEST")
    assert out == {"ok": True}
    assert factory_calls[0] == 3
