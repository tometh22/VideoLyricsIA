"""P3 2026-07-17 — validación Vision del fondo único en paralelo al encode.

Bajo enforcement=observe el veredicto nunca rechaza ni reasigna el fondo —
corrida inline solo sumaba ~65s al camino crítico. Con
BACKGROUND_VALIDATION_PARALLEL=1 (y SOLO bajo observe) el Vision check se
despacha a un hilo y se junta antes de _verify_deliverables.

El paralelismo se prueba por ORDENAMIENTO con threading.Event, no por
umbral de wall-clock (flaky en runners de CI — regla anti-flake del repo).
"""
import threading

import pytest

import pipeline


class TestGate:
    def test_off_por_default(self, monkeypatch):
        monkeypatch.setenv("BACKGROUND_VALIDATION_ENFORCEMENT", "observe")
        monkeypatch.delenv("BACKGROUND_VALIDATION_PARALLEL", raising=False)
        assert pipeline._validation_parallel_enabled() is False

    def test_on_solo_con_observe(self, monkeypatch):
        monkeypatch.setenv("BACKGROUND_VALIDATION_ENFORCEMENT", "observe")
        monkeypatch.setenv("BACKGROUND_VALIDATION_PARALLEL", "1")
        assert pipeline._validation_parallel_enabled() is True

    def test_block_ignora_el_flag(self, monkeypatch):
        """Prod (block) jamás paraleliza, aunque el flag esté prendido:
        en block el veredicto SÍ puede rechazar/reemplazar el fondo y el
        orden secuencial es la garantía."""
        monkeypatch.delenv("BACKGROUND_VALIDATION_ENFORCEMENT", raising=False)
        monkeypatch.setenv("BACKGROUND_VALIDATION_PARALLEL", "1")
        assert pipeline._validation_parallel_enabled() is False


class TestRunObserveValidation:
    def _capture_updates(self, monkeypatch):
        calls = []
        monkeypatch.setattr(
            pipeline, "update_job",
            lambda job_id, **kw: calls.append((job_id, kw)),
        )
        return calls

    def test_passed_un_solo_update(self, monkeypatch):
        calls = self._capture_updates(monkeypatch)
        verdict = {"passed": True, "issues": []}
        out = pipeline._run_observe_validation("job1", lambda: dict(verdict))
        assert out["passed"] is True
        assert len(calls) == 1
        assert calls[0][1]["validation_result"]["passed"] is True

    def test_failed_dos_updates_con_observed(self, monkeypatch):
        calls = self._capture_updates(monkeypatch)
        verdict = {"passed": False, "issues": [{"type": "people"}]}
        out = pipeline._run_observe_validation("job1", lambda: dict(verdict))
        assert out["passed"] is True
        assert out["observed_violation"] is True
        assert out["observed_issues"] == [{"type": "people"}]
        assert out["enforcement"] == "observe"
        assert len(calls) == 2
        assert calls[0][1]["validation_result"]["passed"] is False
        assert calls[1][1]["validation_result"]["observed_violation"] is True

    def test_excepcion_se_propaga_via_result(self, monkeypatch):
        """El error del hilo debe re-lanzarse en el join (patrón fan-out de
        escenas) — no perderse en silencio."""
        from concurrent.futures import ThreadPoolExecutor

        self._capture_updates(monkeypatch)

        def _boom():
            raise RuntimeError("vision caída")

        with ThreadPoolExecutor(max_workers=1) as pool:
            fut = pool.submit(pipeline._run_observe_validation, "job1", _boom)
            with pytest.raises(RuntimeError, match="vision caída"):
                fut.result(timeout=5)

    def test_paralelismo_por_ordenamiento(self, monkeypatch):
        """El main thread avanza más allá del submit MIENTRAS la validación
        sigue bloqueada — demostrado con Events, sin depender del reloj."""
        from concurrent.futures import ThreadPoolExecutor

        self._capture_updates(monkeypatch)
        started = threading.Event()
        release = threading.Event()
        verdict = {"passed": True, "issues": []}

        def _slow_validate():
            started.set()
            assert release.wait(timeout=5), "el test nunca liberó el hilo"
            return dict(verdict)

        with ThreadPoolExecutor(max_workers=1) as pool:
            fut = pool.submit(
                pipeline._run_observe_validation, "job1", _slow_validate,
            )
            # El hilo arrancó y sigue bloqueado — y ESTE hilo (el "encode")
            # sigue ejecutando: eso ES el overlap.
            assert started.wait(timeout=5)
            main_thread_avanzo = True
            assert main_thread_avanzo and not fut.done()
            release.set()
            assert fut.result(timeout=5)["passed"] is True

    def test_veredicto_se_persiste_aunque_el_encode_explote(self, monkeypatch):
        """Simula el finally de rescate: encode muere después del fork; el
        join best-effort igual persiste el veredicto del hilo."""
        from concurrent.futures import ThreadPoolExecutor

        calls = self._capture_updates(monkeypatch)
        release = threading.Event()

        def _slow_validate():
            assert release.wait(timeout=5)
            return {"passed": False, "issues": [{"type": "brand"}]}

        pool = ThreadPoolExecutor(max_workers=1)
        fut = pool.submit(pipeline._run_observe_validation, "job1", _slow_validate)
        try:
            raise RuntimeError("encode explotó")
        except RuntimeError:
            # finally de run_pipeline: rescue join best-effort
            release.set()
            try:
                fut.result(timeout=90)
            except Exception:
                pass
            pool.shutdown(wait=True)
        # El veredicto quedó grabado (2 updates: crudo + observed-pass)
        assert len(calls) == 2
        assert calls[1][1]["validation_result"]["observed_violation"] is True
