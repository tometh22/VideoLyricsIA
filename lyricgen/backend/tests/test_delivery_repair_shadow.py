from copy import deepcopy

from delivery_repair_shadow import attach_delivery_repair_shadow


def _result(reference="JAMÁS podrás olvidarme"):
    return {
        "segments": [
            {"start": 1.0, "end": 3.0, "text": "JAMAS podrás olvidarme"},
        ],
        "reference_lyrics": reference,
        "reference_attestation": {
            "text_status": "independently_attested",
            "allow_vocabulary_reconciliation": True,
            "allow_global_forced_alignment": True,
            "reasons": [],
            "metrics": {},
        },
        "transcription_quality": {"decision": "review_required", "metrics": {
            "audio_duration_s": 10.0,
        }},
    }


def test_shadow_is_disabled_and_preserves_identity(monkeypatch):
    monkeypatch.delenv("DELIVERY_REPAIR_SHADOW_MODE", raising=False)
    source = _result()
    assert attach_delivery_repair_shadow(source) is source


def test_shadow_proposes_safe_correction_without_mutating_output(monkeypatch):
    monkeypatch.setenv("DELIVERY_REPAIR_SHADOW_MODE", "observe")
    source = _result()
    frozen = deepcopy(source)
    output = attach_delivery_repair_shadow(
        source, artist="Artist", title="Song", filename="song.wav",
    )
    assert source == frozen
    assert output["segments"] == source["segments"]
    shadow = output["delivery_repair_shadow"]
    assert shadow["mutated_output"] is False
    assert shadow["t4_word_line_boundaries"]["mutated_segments"] is False
    assert shadow["t4_structural_shadow"]["mutated_segments"] is False
    assert shadow["t4_structural_shadow"][
        "automatic_timing_change_allowed"
    ] is False
    assert shadow["summary"]["applied_count"] == 1
    assert shadow["candidate_segments"][0]["text"] == "JAMÁS podrás olvidarme"
    assert shadow["editor_review"]["review_only"] is True
    assert output["transcription_quality"]["delivery_repair_shadow"] == shadow


def test_shadow_abstains_when_catalogue_is_not_attested(monkeypatch):
    monkeypatch.setenv("DELIVERY_REPAIR_SHADOW_MODE", "observe")
    source = _result()
    source["reference_attestation"]["allow_vocabulary_reconciliation"] = False
    output = attach_delivery_repair_shadow(source)
    assert output["delivery_repair_shadow"] | {
        "t4_word_line_boundaries": None,
        "t4_structural_shadow": None,
    } == {
        "schema_version": "genly-delivery-repair-shadow-v1",
        "mode": "observe",
        "status": "ABSTAINED",
        "reason": "reference_unattested",
        "mutated_output": False,
        "t4_word_line_boundaries": None,
        "t4_structural_shadow": None,
    }
    assert output["delivery_repair_shadow"]["t4_word_line_boundaries"][
        "mutated_segments"
    ] is False
    assert output["segments"] == source["segments"]
    assert output["transcription_quality"]["delivery_repair_shadow"] == output[
        "delivery_repair_shadow"
    ]
