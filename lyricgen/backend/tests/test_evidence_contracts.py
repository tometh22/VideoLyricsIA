from __future__ import annotations

from dataclasses import FrozenInstanceError
import inspect
import json

import pytest

from evidence_contracts import (
    EvidenceRole,
    SourceKind,
    analytics_projection,
    build_line_evidence_contract,
    model_view_lineage,
    reference_fingerprint,
    verify_content_provenance_attestation,
)
import line_evidence
import main


STRONG_TEST_HMAC_KEY = "test-privacy-hmac-key-0123456789abcdef"


@pytest.fixture(autouse=True)
def _privacy_hmac(monkeypatch):
    monkeypatch.setenv(
        "QUALITY_CONTENT_FINGERPRINT_HMAC_KEY", STRONG_TEST_HMAC_KEY,
    )
    monkeypatch.setenv("QUALITY_CONTENT_FINGERPRINT_HMAC_KEY_ID", "test-v1")


def _raw_line(**overrides):
    return {
        "start": 1.25,
        "end": 2.75,
        "text": "Frase privada de prueba",
        "words": [{
            "word": "Frase", "start": 1.25, "end": 1.7, "score": 0.82,
        }],
        **overrides,
    }


def test_contracts_are_immutable_and_separate_recognition_from_alignment():
    row = _raw_line(
        content_source="whisperx_primary",
        ctc_mean_score=0.21,
        ctc_model="spanish-ctc",
        evidence_lineage=[{
            "provider": "openai", "model": "whisper-1",
            "revision": "r1", "family": "primary-whisper",
        }],
    )
    contract = build_line_evidence_contract(
        row, content_source="whisperx_primary",
    )

    assert contract.content_provenance.source_kind is SourceKind.ASR
    assert contract.content_provenance.role is EvidenceRole.ASR_WITNESS
    assert contract.recognition_score == pytest.approx(0.82)
    assert contract.alignment_score == pytest.approx(0.21)
    assert contract.content_provenance.lineage.model == "whisper-1"
    assert contract.timing_provenance.lineage.model == "spanish-ctc"
    with pytest.raises(FrozenInstanceError):
        contract.recognition_score = 0.99


def test_reference_is_candidate_and_never_asr_witness_even_with_high_scores():
    annotated = line_evidence.annotate_provider_evidence([
        _raw_line(
            content_source="operator_reference",
            content_asr_score=0.99,
            content_asr_min_score=0.99,
        )
    ])[0]

    assert annotated["content_provenance"]["source_kind"] == "reference"
    assert annotated["content_provenance"]["role"] == "content_candidate"
    assert annotated["content_provenance"]["is_asr_witness"] is False
    assert annotated["recognition_score"] is None
    assert "content_asr_score" not in annotated
    assert "content_asr_min_score" not in annotated
    assert annotated["reference_fingerprint"].startswith(
        "hmac-sha256:v1:test-v1:"
    )
    assert annotated["content_provenance"]["attested"] is True
    # The legacy provider score may remain private, but quality cannot consume
    # it as ASR evidence.
    assert annotated["provider_evidence"]["mean_score"] == pytest.approx(0.82)
    assert not any(
        "low_asr_content_confidence" in issue["reasons"]
        for issue in line_evidence.evidence_issues([annotated])
    )


def test_catalog_result_is_inferred_as_candidate_before_default_asr_label():
    frozen = line_evidence.freeze_result_provider_evidence({
        "reference_lyrics": "Texto de catálogo completo",
        "segments": [_raw_line()],
    })
    line = frozen["segments"][0]

    assert line["content_source"] == "catalog_reference"
    assert line["content_provenance"]["source_kind"] == "catalog"
    assert line["content_provenance"]["role"] == "content_candidate"
    assert line["recognition_score"] is None


def test_reference_dominates_payload_declared_asr_and_cannot_self_attest():
    secret = "LETRA_PRIVADA_REAL_UOH_UOH"
    frozen = line_evidence.freeze_result_provider_evidence({
        "reference_lyrics": secret,
        "content_source": "whisperx_primary",
        "segments": [_raw_line(
            text=secret, asr_confidence=0.999,
            content_source="whisperx_primary",
        )],
    })
    line = frozen["segments"][0]
    assert line["content_provenance"]["source_kind"] == "catalog"
    assert line["content_provenance"]["role"] == "content_candidate"
    assert line["content_provenance"]["is_asr_witness"] is False
    assert line["provider_evidence"]["source"] == "catalog_reference"
    assert line["recognition_score"] is None
    assert line["reference_fingerprint"].startswith("hmac-sha256:v1:test-v1:")


def test_reference_fingerprint_never_falls_back_to_plain_sha(monkeypatch):
    monkeypatch.delenv("QUALITY_CONTENT_FINGERPRINT_HMAC_KEY", raising=False)
    monkeypatch.delenv("QUALITY_CONTENT_ATTESTATION_KEY", raising=False)
    monkeypatch.delenv("QUALITY_LEARNING_HMAC_KEY", raising=False)
    assert reference_fingerprint("known low entropy lyric") is None


@pytest.mark.parametrize("weak_key", ["x", "x" * 64, "placeholder-key"])
def test_reference_fingerprint_rejects_weak_or_placeholder_hmac_keys(
    monkeypatch, weak_key,
):
    monkeypatch.setenv("QUALITY_CONTENT_FINGERPRINT_HMAC_KEY", weak_key)
    assert reference_fingerprint("known low entropy lyric") is None


def test_raw_provider_snapshot_is_deep_frozen_and_idempotent():
    original = _raw_line(content_source="whisperx_primary")
    first = line_evidence.annotate_provider_evidence([original])[0]
    original["text"] = "Texto mutado fuera del snapshot"
    original["words"][0]["score"] = 0.01
    first_hash = first["provider_evidence"]["raw_output_sha256"]

    # A later timing/content pass may change the visible line; annotating again
    # must retain the original provider payload and identity.
    first["text"] = "Versión editorial posterior"
    first["words"][0]["score"] = 0.11
    first["ctc_mean_score"] = 0.11
    second = line_evidence.annotate_provider_evidence([first])[0]

    assert second["provider_evidence"]["text"] == "Frase privada de prueba"
    assert second["provider_evidence"]["words"][0]["score"] == pytest.approx(0.82)
    assert second["provider_evidence"]["raw_output_sha256"] == first_hash
    assert second["recognition_score"] == pytest.approx(0.82)
    assert second["alignment_score"] == pytest.approx(0.11)
    assert second["provider_output_integrity"] is True


def test_tampered_frozen_provider_output_loses_asr_witness_status():
    frozen = line_evidence.annotate_provider_evidence([
        _raw_line(content_source="whisperx_primary")
    ])[0]
    frozen["provider_evidence"]["text"] = "Contenido alterado después del freeze"
    checked = line_evidence.annotate_provider_evidence([frozen])[0]

    assert checked["provider_output_integrity"] is False
    assert checked["content_provenance"]["role"] == "diagnostic"
    assert checked["content_provenance"]["is_asr_witness"] is False
    assert checked["recognition_score"] is None
    assert "content_asr_score" not in checked


def test_tampered_content_lineage_invalidates_provenance_attestation():
    frozen = line_evidence.annotate_provider_evidence([
        _raw_line(content_source="whisperx_primary")
    ], correlated_family="family-a")[0]

    assert verify_content_provenance_attestation(
        frozen["content_provenance"],
    ) is True
    frozen["content_provenance"]["lineage"]["correlated_family"] = "family-b"
    assert verify_content_provenance_attestation(
        frozen["content_provenance"],
    ) is False


def test_views_from_same_parent_and_model_share_one_correlated_family():
    common = {
        "source": "whisperx_primary",
        "provider": "openai",
        "model": "whisper-1",
        "model_revision": "2026-08",
        "parent_audio_sha256": "a" * 64,
    }
    mix = model_view_lineage(view="mix", transformation="original", **common)
    stem = model_view_lineage(view="stem", transformation="demucs-v4", **common)
    slow = model_view_lineage(view="stem", transformation="speed-0.88", **common)
    other_model = model_view_lineage(
        **{**common, "model": "independent-asr"}, view="mix",
    )

    assert mix.correlated_family == stem.correlated_family == slow.correlated_family
    assert other_model.correlated_family != mix.correlated_family
    assert {mix.view, stem.view} == {"mix", "stem"}


def test_analytics_projection_never_contains_lyrics_or_provider_words():
    secret = "LETRA-SECRETA-QUE-NO-DEBE-SALIR"
    line = line_evidence.annotate_provider_evidence([
        _raw_line(text=secret, content_source="operator_reference")
    ])[0]

    safe = line_evidence.evidence_analytics_projection(line)
    encoded = json.dumps(safe, ensure_ascii=False)
    assert secret not in encoded
    assert "Frase" not in encoded
    assert '"text"' not in encoded
    assert '"word"' not in encoded
    assert safe == analytics_projection(line)
    assert safe["frozen_provider_output"]["text_fingerprint"].startswith(
        "hmac-sha256:v1:test-v1:"
    )
    assert "text_sha256" not in safe["frozen_provider_output"]
    assert safe["content_provenance"]["role"] == "content_candidate"

    poisoned = dict(line)
    poisoned["content_provenance"] = {
        **line["content_provenance"],
        "text": secret,
        "lineage": {"provider": secret, "text": secret},
    }
    poisoned["provider_evidence"] = {
        **line["provider_evidence"],
        "frozen_provider_output": {
            **line["provider_evidence"]["frozen_provider_output"],
            "start": secret,
        },
    }
    assert secret not in json.dumps(analytics_projection(poisoned), ensure_ascii=False)
    assert analytics_projection(poisoned)["content_provenance"]["lineage"] == {
        "provider": "unknown", "model": "unknown", "model_revision": "unknown",
        "view": "unknown", "transformation": "unknown",
        "parent_audio_fingerprint": None,
        "correlated_family_fingerprint": None,
    }


def test_analytics_replaces_raw_audio_digest_with_versioned_hmac():
    raw_hash = "a" * 64
    contract = build_line_evidence_contract(
        _raw_line(), content_source="whisperx_primary",
        parent_audio_sha256=raw_hash,
    )
    lineage = analytics_projection(contract)["content_provenance"]["lineage"]
    encoded = json.dumps(lineage)

    assert "parent_audio_sha256" not in lineage
    assert raw_hash not in encoded
    assert lineage["parent_audio_fingerprint"].startswith(
        "hmac-sha256:v1:test-v1:"
    )


def test_analytics_rekeys_payload_declared_correlated_family():
    attacker_family = "hmac-sha256:v1:attacker:" + "a" * 64
    contract = build_line_evidence_contract(
        _raw_line(), content_source="whisperx_primary",
        parent_audio_sha256="b" * 64,
        correlated_family=attacker_family,
    )
    lineage = analytics_projection(contract)["content_provenance"]["lineage"]
    projected = lineage["correlated_family_fingerprint"]

    assert projected.startswith("hmac-sha256:v1:test-v1:")
    assert projected != attacker_family
    assert attacker_family not in json.dumps(lineage)


def test_existing_public_helpers_remain_compatible():
    assert line_evidence.tokens("Árbol, canción") == ["arbol", "cancion"]
    assert line_evidence.canonical_content_sequence([
        {"text": "Árbol, canción"}, {"text": "¡Oh!"},
    ]) == ["arbol cancion", "oh"]
    assert line_evidence.freeze_result_provider_evidence(None) is None


def test_cascade_freezes_selected_provider_before_timing_postprocess():
    source = inspect.getsource(main._run_transcription_for_job)
    emit = source[source.index("def _emit_segments"):source.index("async def _get_align_audio")]
    assert emit.index("annotate_provider_evidence(") < emit.index("_dedup_collisions(")
    assert emit.index("_dedup_collisions(") < emit.index("_snap(")
