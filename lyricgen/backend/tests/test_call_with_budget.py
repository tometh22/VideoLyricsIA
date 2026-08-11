"""Tests for `forced_align._call_with_budget` — bounded retry helper
that caps total wall-clock time and aborts immediately on known
non-retryable errors (Bug C + E from the QA full run incident
2026-05-24).

Bug E (no budget): three retries of ~90 minutes each, no cumulative
cap → Intoxicados elapsed 16057 s (4 h 27 min) in QA full run.

Bug C class (non-retryable): "Padding size should be less than the
corresponding input dimension" — a deterministic crash from the model,
yet the previous loop retried 3 times burning 32 s of sleeps + 3 RTTs
on a guaranteed failure.
"""
import time

import pytest

import forced_align


# ─── _is_non_retryable matcher ────────────────────────────────────────

def test_is_non_retryable_matches_padding_bug():
    """Bug C signature — model crashes with this when fed a degenerate
    audio tensor. Must abort instead of retrying."""
    err = RuntimeError(
        "Argument #4: Padding size should be less than the corresponding "
        "input dimension, but got: padding (200, 200) at dimension 2 of "
        "input [1, 2, 1]"
    )
    assert forced_align._is_non_retryable(err) is True


def test_is_non_retryable_matches_validation_error():
    assert forced_align._is_non_retryable(
        ValueError("ValidationError on field transcript"))
    assert forced_align._is_non_retryable(
        RuntimeError("Invalid input: transcript too long"))


def test_is_retryable_for_transient_network_errors():
    """The errors that ARE worth retrying — these regularly cleared up
    on the second attempt in the QA full run."""
    assert forced_align._is_non_retryable(
        ConnectionError("Server disconnected without sending a response.")
    ) is False
    assert forced_align._is_non_retryable(
        TimeoutError("The read operation timed out")
    ) is False
    assert forced_align._is_non_retryable(
        RuntimeError("HTTP 429 burst of 1")
    ) is False


# ─── _call_with_budget mechanics ──────────────────────────────────────

def test_succeeds_on_first_try(monkeypatch):
    """Happy path — the model returns on attempt 1, no sleeps consumed."""
    monkeypatch.setattr("replicate.run", lambda *a, **kw: {"ok": True})
    out = forced_align._call_with_budget(
        "fake/model", lambda: {"x": 1},
        total_budget_s=10, backoff=[0, 8, 24],
    )
    assert out == {"ok": True}


def test_retries_then_succeeds(monkeypatch):
    """Transient flake on attempt 1 → sleep → retry succeeds. Mirrors
    cureau's intermittent `[1, 2, 0]` first-attempt error pattern."""
    attempts = [0]
    def _flaky(*a, **kw):
        attempts[0] += 1
        if attempts[0] == 1:
            raise ConnectionError("Server disconnected without sending a response.")
        return {"ok": True}
    monkeypatch.setattr("replicate.run", _flaky)
    out = forced_align._call_with_budget(
        "m", lambda: {},
        total_budget_s=30,
        backoff=[0, 0],   # zero sleeps so the test runs fast
    )
    assert out == {"ok": True}
    assert attempts[0] == 2


def test_aborts_on_non_retryable(monkeypatch):
    """Bug C regression: Padding-size error → abort on attempt 1, do NOT
    burn the remaining attempts/sleeps. Caller falls through to whisperX
    seconds later instead of 5+ minutes later."""
    calls = []
    def _crash(*a, **kw):
        calls.append(1)
        raise RuntimeError(
            "Argument #4: Padding size should be less than the corresponding "
            "input dimension, but got: padding (200, 200) at dimension 2 of "
            "input [1, 2, 1]"
        )
    monkeypatch.setattr("replicate.run", _crash)
    t0 = time.monotonic()
    out = forced_align._call_with_budget(
        "m", lambda: {},
        total_budget_s=480,
        backoff=[0, 8, 24],   # the 8s+24s would burn 32s if retried
    )
    elapsed = time.monotonic() - t0
    assert out is None
    assert len(calls) == 1, "should NOT retry on non-retryable error"
    assert elapsed < 1.0, f"aborted in {elapsed:.2f}s (expected < 1s)"


def test_aborts_on_budget_exhaustion(monkeypatch):
    """Bug E regression: wall-clock budget enforced even when every
    attempt errors transient and would technically be retryable."""
    def _slow_fail(*a, **kw):
        time.sleep(0.4)
        raise ConnectionError("Server disconnected")
    monkeypatch.setattr("replicate.run", _slow_fail)
    t0 = time.monotonic()
    # 1 s budget, 0-sleep backoff with up to 5 attempts. Each attempt
    # takes 0.4 s, so we should fit ~2-3 attempts in 1 s, NEVER all 5.
    out = forced_align._call_with_budget(
        "m", lambda: {},
        total_budget_s=1.0,
        backoff=[0, 0, 0, 0, 0],
    )
    elapsed = time.monotonic() - t0
    assert out is None
    # Aborts well under the worst case (5 * 0.4 = 2.0 s)
    assert elapsed < 1.6, f"budget didn't cap retries (took {elapsed:.2f}s)"


def test_input_factory_called_fresh_each_attempt(monkeypatch):
    """File handles are consumed on upload — the factory MUST be called
    on each attempt so we get a fresh `open()`. A frozen input dict
    would have an already-read file pointer on the second try."""
    factory_calls = [0]
    def _factory():
        factory_calls[0] += 1
        return {"id": factory_calls[0]}
    seen_inputs = []
    def _spy(model, input):
        seen_inputs.append(input)
        if len(seen_inputs) < 3:
            raise ConnectionError("transient")
        return {"ok": True}
    monkeypatch.setattr("replicate.run", _spy)
    out = forced_align._call_with_budget(
        "m", _factory,
        total_budget_s=10,
        backoff=[0, 0, 0],
    )
    assert out == {"ok": True}
    assert factory_calls[0] == 3
    assert [i["id"] for i in seen_inputs] == [1, 2, 3]


def test_input_factory_failure_falls_back_instead_of_raising(monkeypatch):
    """Un temporal borrado/EMFILE mantiene el contrato `Never raises`."""
    monkeypatch.setattr(
        "replicate.run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("sin input no se llama a Replicate")
        ),
    )

    out = forced_align._call_with_budget(
        "m",
        lambda: (_ for _ in ()).throw(OSError("Too many open files")),
        total_budget_s=10,
        backoff=[0],
    )

    assert out is None
