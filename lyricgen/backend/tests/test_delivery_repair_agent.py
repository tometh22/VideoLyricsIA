from copy import deepcopy

from delivery_repair_agent import RepairPolicy, repair_delivery_manifest


def _umg_manifest():
    return {
        "metadata": {
            "artist": "Enanitos Verdes", "title": "Tu Cárcel",
            "version": "Lyric Video / En Vivo Desde Tijuana, Mexico/2004",
        },
        "asset": {
            "rendered_title": "Tu Carcel_En Vivo", "title_time": 1.133,
            "duration": 241.909,
        },
        "segments": [
            {"start": 74.367, "end": 75.5, "text": "TENDRAS una vida mejor"},
            {"start": 75.8, "end": 76.9, "text": "JAMAS podrás olvidarme"},
            {"start": 77.967, "end": 79.1, "text": "JAMAS, aunque lo intentes"},
            {"start": 186.7, "end": 188.2, "text": "POR LA AVENTRUA"},
        ],
        "approved_lyrics": [
            "TENDRÁS una vida mejor", "JAMÁS podrás olvidarme",
            "JAMÁS, aunque lo intentes", "POR LA AVENTURA",
        ],
        "reference_trusted": True,
        "fps": 30,
    }


def test_repair_agent_fixes_umg_example_and_keeps_audit():
    original = _umg_manifest()
    frozen = deepcopy(original)
    result = repair_delivery_manifest(original)

    assert original == frozen
    assert result["status"] == "REPAIRED"
    assert result["summary"]["applied_count"] == 5  # title + four occurrences
    # A visible title/metadata mismatch is delivery-blocking, not advisory.
    assert result["summary"]["risk_before"] == (1, 3, 5)
    assert result["summary"]["risk_after"] == (0, 0, 0)
    assert result["manifest"]["asset"]["rendered_title"] == "Tu Cárcel"
    assert [row["text"] for row in result["manifest"]["segments"]] == [
        "TENDRÁS una vida mejor",
        "JAMÁS podrás olvidarme",
        "JAMÁS, aunque lo intentes",
        "POR LA AVENTURA",
    ]
    assert all(item["status"] == "APPLIED" for item in result["actions"])
    assert result["editor_review"]["review_only"] is True
    assert result["editor_review"]["items"][0]["timecode"] == "00:00:01:04"
    assert "rerun_transcription_quality_for_new_segment_revision" in result[
        "requirements_before_delivery"
    ]


def test_untrusted_reference_never_changes_lyrics_without_verifier():
    manifest = _umg_manifest()
    manifest["reference_trusted"] = False
    result = repair_delivery_manifest(manifest)
    assert result["manifest"]["segments"] == manifest["segments"]
    assert result["manifest"]["asset"]["rendered_title"] == "Tu Cárcel"
    assert result["summary"]["changed_domains"] == ["metadata"]


def test_ambiguous_typo_can_use_independent_verifier():
    manifest = _umg_manifest()
    manifest["reference_trusted"] = True
    result = repair_delivery_manifest(
        manifest,
        policy=RepairPolicy(trusted_typo_min_match=1.01),
        lexical_verifier=lambda context: {
            "accepted": context["issue"]["actual"] == "AVENTRUA",
            "confidence": 0.97,
            "reason": "stem_mix_ctc_agreement",
        },
    )
    aventrua = [item for item in result["actions"] if item["code"] == "LYRIC_TOKEN_TYPO"]
    assert aventrua[0]["status"] == "APPLIED"
    assert aventrua[0]["reason"] == "bounded_token_replacement"


def test_small_overlap_is_clamped_but_large_overlap_is_proposed():
    base = {
        "metadata": {"title": "Song"}, "asset": {"duration": 20},
        "segments": [
            {"start": 1, "end": 4.2, "text": "one"},
            {"start": 4, "end": 6, "text": "two"},
            {"start": 8, "end": 12, "text": "three"},
            {"start": 10, "end": 13, "text": "four"},
        ],
    }
    result = repair_delivery_manifest(
        base, policy=RepairPolicy(auto_small_overlap=True)
    )
    assert result["manifest"]["segments"][0]["end"] == 4.0
    assert result["manifest"]["segments"][2]["end"] == 12
    overlap_actions = [item for item in result["actions"] if item["code"] == "LYRIC_OVERLAP"]
    assert {item["status"] for item in overlap_actions} == {"APPLIED", "PROPOSED"}


def test_outside_asset_is_clamped_only_when_duration_remains_valid():
    result = repair_delivery_manifest({
        "metadata": {"title": "Song"}, "asset": {"duration": 10},
        "segments": [
            {"start": 8, "end": 12, "text": "clamp me"},
            {"start": 11, "end": 12, "text": "cannot infer me"},
        ],
    })
    assert result["manifest"]["segments"][0]["end"] == 10
    assert result["manifest"]["segments"][1]["end"] == 12
    assert any(item["status"] == "PROPOSED" for item in result["actions"])


def test_invalid_range_requires_and_accepts_independent_endpoint():
    manifest = {
        "metadata": {"title": "Song"}, "asset": {"duration": 10},
        "segments": [{"start": 2, "end": 2, "text": "held word"}],
    }
    without = repair_delivery_manifest(manifest)
    assert without["manifest"]["segments"][0]["end"] == 2
    assert without["actions"][0]["status"] == "PROPOSED"

    repaired = repair_delivery_manifest(
        manifest,
        timing_verifier=lambda context: {
            "accepted": True,
            "confidence": 0.96,
            "reason": "stem_mix_endpoint_agreement",
            "start": 2.0,
            "end": 3.4,
        },
    )
    assert repaired["manifest"]["segments"][0]["end"] == 3.4
    assert repaired["actions"][0]["status"] == "APPLIED"


def test_live_timing_is_never_auto_repaired_without_verifier():
    result = repair_delivery_manifest({
        "metadata": {"title": "Live"}, "asset": {"duration": 20},
        "is_live": True,
        "segments": [
            {"start": 1, "end": 4.2, "text": "lead"},
            {"start": 4, "end": 6, "text": "crowd"},
        ],
    })
    assert result["manifest"]["segments"][0]["end"] == 4.2
    assert result["actions"][0]["status"] == "PROPOSED"


def test_untrusted_reference_is_escalated_not_rewritten():
    result = repair_delivery_manifest({
        "metadata": {"title": "Wrong reference"}, "asset": {"duration": 100},
        "segments": [{"start": 1, "end": 90, "text": "candidate"}],
        "reference_health": {
            "text_status": "unsafe_without_witness", "timeline_status": "complete",
            "allow_vocabulary_reconciliation": False,
            "reasons": ["reference_text_not_attested"], "metrics": {},
        },
    })
    assert result["before_preflight"]["decision"] == "BLOCK"
    assert result["actions"][0]["status"] == "ESCALATED"
    assert result["summary"]["escalated_count"] == 1
    assert "reprocess_or_review_blocking_findings" in result[
        "requirements_before_delivery"
    ]
