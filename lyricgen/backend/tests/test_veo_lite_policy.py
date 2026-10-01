"""Stale settings cannot submit paid Veo Fast calls."""
from types import SimpleNamespace
import pytest
from campaign_models import VEO_LITE, effective_veo_assignment, model_for_campaign_job, veo_models


def test_lite_is_mandatory_despite_environment(monkeypatch):
    monkeypatch.setenv("VEO_MODEL", "veo-3.1-fast-generate-001")
    monkeypatch.setenv("VEO_MODEL_STATIC", "veo-3.1-generate-001")
    assert veo_models() == [{"id": VEO_LITE, "label": "Veo Lite"}]
    assert model_for_campaign_job(None, "veo-3.1-fast-generate-001") == VEO_LITE
    assert model_for_campaign_job("legacy-fast", "veo-3.1-generate-001") == VEO_LITE


def test_new_receipt_preserves_original_request_and_historical_assignment():
    old = {"requirement": "veo", "model": "veo-3.1-fast-generate-001", "revision": 7}
    current = effective_veo_assignment(old)
    assert current["model"] == VEO_LITE
    assert current["requested_model"] == old["model"]
    assert current["revision"] == 7
    assert old["model"] == "veo-3.1-fast-generate-001"
    assert effective_veo_assignment(current) == current
    assert effective_veo_assignment(None) is None


@pytest.mark.parametrize("movement,high_fidelity", [("", False), ("estatico", False), ("foto-estatica", True), ("animado", True)])
def test_provider_cache_and_provenance_use_lite(monkeypatch, tmp_path, movement, high_fidelity):
    import pipeline
    import provenance
    import requests
    monkeypatch.setenv("VEO_MODEL", "veo-3.1-fast-generate-001")
    monkeypatch.setenv("VEO_MODEL_STATIC", "veo-3.1-generate-001")
    monkeypatch.setattr(pipeline, "_last_veo_request", 0)
    monkeypatch.setattr(pipeline, "_veo_budget_enforced_for_job", lambda _: False)
    monkeypatch.setattr(pipeline, "_veo_access_token", lambda: "synthetic-token")
    monkeypatch.setattr(pipeline, "_release_veo_reservation", lambda *a, **kw: None)
    recorded, cached, sent = [], [], []
    def record(**kw):
        recorded.append(kw)
        return SimpleNamespace(_row_id=1, finish=lambda **kw: None)
    monkeypatch.setattr(provenance, "record_ai_call", record)
    cache_key = pipeline._veo_cache_key
    def cache(prompt, model, params):
        cached.append(model)
        return cache_key(prompt, model, params)
    monkeypatch.setattr(pipeline, "_veo_cache_key", cache)
    def reject(url, **kw):
        sent.append(url)
        response = requests.Response()
        response.status_code = 403
        response._content = b'{"error":{"message":"synthetic denied"}}'
        return response
    monkeypatch.setattr(requests, "post", reject)
    with pytest.raises(RuntimeError):
        pipeline._generate_veo_video("Quiet landscape", str(tmp_path / "clip.mp4"), job_id="liteonly0001", movement_style=movement, high_fidelity=high_fidelity)
    assert len(sent) == 1
    assert sent[0].endswith(f"/publishers/google/models/{VEO_LITE}:predictLongRunning")
    assert cached == [VEO_LITE]
    assert [r["tool_name"] for r in recorded] == [VEO_LITE]
