"""Tests for the on_progress branch of call_with_budget.

The transcription pipeline shows "Aislando voz" stuck at 25% for the
whole 60-180 s Demucs call because the model runs on Replicate (no local
callback). We swap `replicate.run` for `predictions.create + poll` when
on_progress is supplied, parsing logs for a `%` token and falling back
to a time-based asymptote when the logs don't have one.

These tests mock Replicate's SDK to exercise both paths.
"""

import sys
import time
import types

import pytest


class _FakePrediction:
    """Mimics the bits of replicate.predictions.Prediction we use."""

    def __init__(self, transitions):
        # transitions: list of (status, logs, output) tuples; reload()
        # advances to the next on each call.
        self._transitions = list(transitions)
        self._idx = -1
        self.status = "starting"
        self.logs = ""
        self.output = None
        self.error = None

    def reload(self):
        if self._idx + 1 < len(self._transitions):
            self._idx += 1
            self.status, self.logs, self.output = self._transitions[self._idx]

    def cancel(self):
        self.status = "canceled"


def _install_fake_replicate(prediction):
    fake = types.ModuleType("replicate")
    fake.predictions = types.SimpleNamespace(create=lambda version, input: prediction)
    fake.run = lambda *a, **kw: pytest.fail("replicate.run should not be called when on_progress is set")
    sys.modules["replicate"] = fake


def test_progress_emitted_from_log_percent(monkeypatch):
    from replicate_budget import call_with_budget

    prediction = _FakePrediction([
        ("processing", "Selected model is mdx_extra\n  10%|█   |", None),
        ("processing", "Selected model is mdx_extra\n  55%|████  |", None),
        ("succeeded",  "100%|██████|", {"vocals": "https://replicate.delivery/x.wav"}),
    ])
    _install_fake_replicate(prediction)
    monkeypatch.setattr(time, "sleep", lambda *_a, **_kw: None)

    seen = []
    out = call_with_budget(
        "cjwbw/demucs:abc",
        lambda: {"audio": object(), "stem": "vocals"},
        total_budget_s=60.0,
        backoff=[0],
        call_label="demucs_test",
        on_progress=seen.append,
        typical_runtime_s=90.0,
    )

    assert out == {"vocals": "https://replicate.delivery/x.wav"}
    assert seen, "on_progress was never called"
    # First emission is the synthetic 0.05 created-tick.
    assert seen[0] == pytest.approx(0.05, abs=0.001)
    # Then we should see 0.10 and 0.55 from parsed logs.
    assert any(abs(v - 0.10) < 0.001 for v in seen), seen
    assert any(abs(v - 0.55) < 0.001 for v in seen), seen
    # Final tick is exactly 1.0 (handoff signal for the next stage).
    assert seen[-1] == 1.0


def test_progress_monotonic_and_time_based_fallback(monkeypatch):
    from replicate_budget import call_with_budget

    # Logs without a parseable % → forces the time-based asymptote path.
    prediction = _FakePrediction([
        ("processing", "loading model", None),
        ("processing", "running inference", None),
        ("succeeded",  "ok", {"vocals": "x"}),
    ])
    _install_fake_replicate(prediction)
    monkeypatch.setattr(time, "sleep", lambda *_a, **_kw: None)

    seen = []
    call_with_budget(
        "cjwbw/demucs:abc",
        lambda: {"audio": object()},
        total_budget_s=60.0,
        backoff=[0],
        call_label="demucs_fallback",
        on_progress=seen.append,
        typical_runtime_s=90.0,
    )

    # Must be monotonically non-decreasing.
    for a, b in zip(seen, seen[1:]):
        assert b >= a, f"progress went backwards: {a} -> {b} in {seen}"
    assert seen[-1] == 1.0


def test_no_on_progress_uses_blocking_run(monkeypatch):
    """When on_progress is not passed, the original `replicate.run` path
    stays in use — guards forced_align/whisperX from accidental changes."""
    from replicate_budget import call_with_budget

    called = {"n": 0}

    fake = types.ModuleType("replicate")
    fake.run = lambda model, input: (called.__setitem__("n", called["n"] + 1), {"ok": True})[1]
    fake.predictions = types.SimpleNamespace(create=lambda **_kw: pytest.fail(
        "predictions.create should NOT be called when on_progress is None"
    ))
    sys.modules["replicate"] = fake
    monkeypatch.setattr(time, "sleep", lambda *_a, **_kw: None)

    out = call_with_budget(
        "any/model:v",
        lambda: {"audio": object()},
        total_budget_s=60.0,
        backoff=[0],
        call_label="forced_test",
    )
    assert out == {"ok": True}
    assert called["n"] == 1


def test_failed_status_propagates_to_caller(monkeypatch):
    """Replicate-reported failure must surface so retry/budget logic runs."""
    from replicate_budget import call_with_budget

    prediction = _FakePrediction([
        ("processing", "loading", None),
        ("failed",     "OOM", None),
    ])
    prediction.error = "OOM"
    _install_fake_replicate(prediction)
    monkeypatch.setattr(time, "sleep", lambda *_a, **_kw: None)

    seen = []
    out = call_with_budget(
        "any/model:v",
        lambda: {"audio": object()},
        total_budget_s=60.0,
        backoff=[0],
        call_label="failed_test",
        on_progress=seen.append,
    )
    # Caller returns None when the attempt fails and no retries succeed.
    assert out is None
