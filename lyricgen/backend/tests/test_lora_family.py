from __future__ import annotations

import json

from lora_family import (
    attach_hypothesis,
    load_verified_family,
    song_disagreement_score,
)
from targeted_consensus import _record_lora_shadow, choose_consensus


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
        "holdout_gate": {"passed": True, "ci_low": 0.01, "ci_high": 0.09, "songs": 11},
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
        "holdout_gate": {"passed": True, "ci_low": 0.01, "ci_high": 0.09, "songs": 11},
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
        "holdout_gate": {"passed": True, "ci_low": 0.01, "ci_high": 0.09, "songs": 11},
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


def test_lora_shadow_counts_only_new_consensus_attribution():
    stats = {}
    words = _words("hola mundo")
    _record_lora_shadow(
        stats,
        lora_words=words,
        with_agreed=words,
        with_evidence={"sources": ["stem", "lora"]},
        without_agreed=None,
        without_evidence={},
    )

    shadow = stats["lora_shadow"]
    assert shadow["comparisons"] == 1
    assert shadow["with_consensus"] == 1
    assert shadow["without_consensus"] == 0
    assert shadow["lora_contributed_lines"] == 1
    assert shadow["new_consensus_lines"] == 1
    assert shadow["lost_consensus_lines"] == 0


def test_song_disagreement_score_is_gold_free_and_abstains_without_both_families():
    base = _words("hola mundo")
    lora = _words("hola ruido")
    score = song_disagreement_score(base, lora)
    assert score["score"] == 0.5
    assert score["source"] == "paired_asr_disagreement"
    assert score["gold_free"] is True
    assert song_disagreement_score(base, []) is None


def test_family_without_holdout_gate_does_not_load(tmp_path, monkeypatch):
    """LoRA v1 archivado: un evaluation.json sin holdout_gate no atesta nada,
    aunque LORA_V1_FAMILY_ENABLED vuelva a 1. 'evaluation_passed' solo decia
    que se decodificaron las 23; no que mejoro sobre holdout."""
    import json
    import lora_family
    adapter = tmp_path / "adapter"
    adapter.mkdir()
    (adapter / "adapter_model.safetensors").write_bytes(b"x")
    report = tmp_path / "evaluation.json"
    report.write_text(json.dumps({
        "pipeline_validated": True, "evaluation_passed": True,
        "gate": {"passed": True, "additional_family_only": True,
                 "runtime_replacement_allowed": False},
        "adapter_path": str(adapter),
    }))
    monkeypatch.setenv("LORA_V1_FAMILY_ENABLED", "1")
    assert lora_family.load_verified_family(report) is None

    report.write_text(json.dumps({
        "pipeline_validated": True, "evaluation_passed": True,
        "gate": {"passed": True, "additional_family_only": True,
                 "runtime_replacement_allowed": False},
        "holdout_gate": {"passed": False, "reasons": ["ci_crosses_zero_or_worse"]},
        "adapter_path": str(adapter),
    }))
    assert lora_family.load_verified_family(report) is None
