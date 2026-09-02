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


def test_no_on_progress_still_uses_the_bounded_poll_path(monkeypatch):
    """CONTRATO INVERTIDO (incidente 2026-08-26/28).

    Este test afirmaba lo contrario: que sin `on_progress` se usaba
    `replicate.run`. Ese era exactamente el bug — `replicate.run` es un
    `prediction.wait()` sin timeout global, así que `total_budget_s` no se
    aplicaba dentro de la llamada y sólo se chequeaba ENTRE intentos.
    En prod eso dio una llamada de whisperX de 26,5 min con presupuesto 480 s
    y demucs huérfanos que colgaban el teardown de `asyncio.run`.

    Ahora la barra de progreso es lo único opcional; el enforcement del
    presupuesto no lo es. El fallback a `replicate.run` sobrevive sólo para
    modelos sin version hash — ver
    `test_model_without_version_hash_keeps_legacy_path`.
    """
    from replicate_budget import call_with_budget

    prediction = _FakePrediction([
        ("succeeded", "", {"ok": True}),
    ])
    used_run = []

    fake = types.ModuleType("replicate")
    fake.predictions = types.SimpleNamespace(
        create=lambda version, input: prediction)
    fake.run = lambda *a, **kw: used_run.append(True)
    sys.modules["replicate"] = fake
    monkeypatch.setattr(time, "sleep", lambda *_a, **_kw: None)

    out = call_with_budget(
        "any/model:v",
        lambda: {"audio": object()},
        total_budget_s=60.0,
        backoff=[0],
        call_label="forced_test",
        # sin on_progress a propósito
    )

    assert out == {"ok": True}
    assert not used_run, (
        "sin on_progress se volvió a usar replicate.run: el presupuesto "
        "deja de aplicarse dentro de la llamada"
    )


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


# --- Regresión incidente 2026-08-26/28 (UMG Chile) -------------------------
#
# `total_budget_s` sólo se hacía cumplir en el camino con `on_progress`. Sin
# él, `call_with_budget` caía a `replicate.run()`, que internamente es un
# `prediction.wait()` sin timeout global: el presupuesto se chequeaba
# únicamente ENTRE intentos. En prod eso produjo una llamada de whisperX de
# 26,5 min con total_budget_s=480, y demucs huérfanos que colgaban el
# `loop.shutdown_default_executor()` del teardown de `asyncio.run` hasta que
# RQ mataba el job por death penalty — tirando transcripciones YA terminadas.
#
# OJO al escribir estos tests: `call_with_budget` ATRAPA toda excepción del
# SDK y la degrada a "attempt failed" → devolver None. Por eso un stub de
# `replicate.run` que hace `pytest.fail()` NO sirve como aserción: el fallo
# queda tragado y el test pasa igual. El discriminante real es si la
# predicción terminó `canceled` (sólo el camino con deadline la cancela).


class _StuckPrediction:
    """Prediction que nunca sale de `processing` — el modelo lento del incidente."""

    def __init__(self):
        self.status = "processing"
        self.logs = "sin porcentaje parseable"
        self.output = None
        self.error = None
        self.reloads = 0

    def reload(self):
        self.reloads += 1

    def cancel(self):
        self.status = "canceled"


class _FakeClock:
    """Reloj determinista: cada `sleep(n)` adelanta n segundos."""

    def __init__(self, start=1000.0):
        self.now = start

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


def test_budget_enforced_within_call_without_on_progress(monkeypatch):
    """Sin `on_progress`, el deadline corta DENTRO de la llamada y cancela.

    Antes del fix se usaba `replicate.run` (sin tope) y la predicción nunca
    se cancelaba: `used_run` quedaba True y `status` seguía en "processing".
    """
    import replicate_budget

    prediction = _StuckPrediction()
    used_run = []

    fake = types.ModuleType("replicate")
    fake.predictions = types.SimpleNamespace(
        create=lambda version, input: prediction)
    fake.run = lambda *a, **kw: used_run.append(True)
    sys.modules["replicate"] = fake
    monkeypatch.setattr(replicate_budget, "_t", _FakeClock())

    out = replicate_budget.call_with_budget(
        "cjwbw/demucs:abc123",
        lambda: {"audio": "x"},
        total_budget_s=30.0,
        backoff=[0],
        call_label="VOCALSEP",
        # on_progress deliberadamente ausente: ese era el camino roto.
    )

    assert not used_run, (
        "sin on_progress se volvió a usar replicate.run — presupuesto "
        "inaplicable dentro de la llamada"
    )
    assert prediction.status == "canceled", (
        "al vencer el deadline hay que cancelar la predicción en Replicate; "
        "si no, se sigue pagando y el thread queda huérfano"
    )
    assert out is None, "budget agotado debe abortar, no devolver output"


def test_model_without_version_hash_keeps_legacy_path(monkeypatch):
    """Un modelo sin `:version` no sirve para `predictions.create`.

    Ese caso (override por env var, no el default) conserva el camino
    histórico en vez de romper.
    """
    import replicate_budget

    called = {}
    fake = types.ModuleType("replicate")
    fake.predictions = types.SimpleNamespace(
        create=lambda version, input: pytest.fail(
            "predictions.create no puede usarse sin version hash"),
    )

    def _run(model, input):
        called["model"] = model
        return {"ok": True}

    fake.run = _run
    sys.modules["replicate"] = fake
    monkeypatch.setattr(replicate_budget, "_t", _FakeClock())

    out = replicate_budget.call_with_budget(
        "owner/sin-version",
        lambda: {"audio": "x"},
        total_budget_s=60.0,
        backoff=[0],
        call_label="VOCALSEP",
    )

    assert out == {"ok": True}
    assert called["model"] == "owner/sin-version"
