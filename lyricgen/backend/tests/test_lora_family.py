from __future__ import annotations

import json

from lora_family import attach_hypothesis, load_verified_family
from targeted_consensus import choose_consensus


def _words(text):
    return [{"word": item, "start": float(i), "end": float(i + 0.4)} for i, item in enumerate(text.split())]


def test_family_is_disabled_by_default(monkeypatch, tmp_path):
    monkeypatch.delenv("LORA_V1_FAMILY_ENABLED", raising=False)
    assert load_verified_family(tmp_path / "missing.json") is None


def test_smoke_adapter_cannot_enter_consensus(monkeypatch, tmp_path):
    report = tmp_path / "smoke.json"
    report.write_text(json.dumps({
        "pipeline_validated": False,
        "evaluation_passed": False,
        "adapter_path": str(tmp_path),
        "replacement_gate": {"additional_family_only": True, "runtime_replacement_allowed": False},
    }))
    monkeypatch.setenv("LORA_V1_FAMILY_ENABLED", "1")
    assert load_verified_family(report) is None


def test_attested_family_is_additional_only(monkeypatch, tmp_path):
    artifact = tmp_path / "adapter.bin"
    artifact.write_bytes(b"adapter")
    import hashlib
    report = tmp_path / "report.json"
    report.write_text(json.dumps({
        "pipeline_validated": True,
        "evaluation_passed": True,
        "adapter_path": str(artifact),
        "adapter_sha256": hashlib.sha256(b"adapter").hexdigest(),
        "replacement_gate": {
            "additional_family_only": True,
            "runtime_replacement_allowed": False,
        },
    }))
    monkeypatch.setenv("LORA_V1_FAMILY_ENABLED", "1")
    result = {}
    assert attach_hypothesis(result, _words("hola mundo"), report_path=str(report))
    assert result["_lora_asr_family"] == "openai_whisper_large_v3_turbo_lora_v1"
    agreed, evidence = choose_consensus(
        _words("hola mundo"), [], [], lora_words=_words("hola mundo"),
        stream_families={"stem": "base", "lora": "lora"}, threshold=0.9,
    )
    assert agreed is not None
    assert "lora" in evidence["sources"]


def test_evaluation_gate_report_is_accepted(monkeypatch, tmp_path):
    artifact = tmp_path / "adapter.bin"
    artifact.write_bytes(b"adapter")
    import hashlib
    report = tmp_path / "evaluation.json"
    report.write_text(json.dumps({
        "pipeline_validated": True, "evaluation_passed": True,
        "adapter_path": str(artifact),
        "adapter_sha256": hashlib.sha256(b"adapter").hexdigest(),
        "gate": {"passed": True, "additional_family_only": True,
                  "runtime_replacement_allowed": False},
    }))
    monkeypatch.setenv("LORA_V1_FAMILY_ENABLED", "1")
    family = load_verified_family(report)
    assert family is not None
    assert family["replacement_allowed"] is False


def test_attested_adapter_can_be_mounted_at_different_path(monkeypatch, tmp_path):
    mounted = tmp_path / "mounted" / "adapter_model.safetensors"
    mounted.parent.mkdir()
    mounted.write_bytes(b"adapter")
    import hashlib
    report = tmp_path / "evaluation.json"
    report.write_text(json.dumps({
        "pipeline_validated": True, "evaluation_passed": True,
        "adapter_path": "/research-pod/no-longer-mounted/adapter",
        "adapter_sha256": hashlib.sha256(b"adapter").hexdigest(),
        "replacement_gate": {
            "additional_family_only": True,
            "runtime_replacement_allowed": False,
        },
    }))
    monkeypatch.setenv("LORA_V1_FAMILY_ENABLED", "1")
    monkeypatch.setenv("LORA_V1_ADAPTER_PATH", str(mounted.parent))
    family = load_verified_family(report)
    assert family is not None
    assert family["artifact"] == str(mounted.parent)
    assert family["adapter_sha256"]


def test_transcribe_words_is_fail_closed_when_disabled(monkeypatch, tmp_path):
    monkeypatch.delenv("LORA_V1_FAMILY_ENABLED", raising=False)
    words, stats = __import__("lora_family").transcribe_words(tmp_path / "audio.wav")
    assert words == []
    assert stats["status"] == "declined"
    assert stats["reason"] == "not_attested_or_disabled"
