import json

from delivery_preflight import build_delivery_preflight, frame_timecode


def test_frame_timecode_is_non_drop_and_configurable():
    assert frame_timecode(75.8, 30) == "00:01:15:24"
    assert frame_timecode(1.133, 30) == "00:00:01:04"


def test_universal_example_groups_spelling_and_flags_metadata():
    segments = [
        {"start": 74.367, "end": 75.5, "text": "TENDRAS una vida mejor"},
        {"start": 75.8, "end": 76.9, "text": "JAMAS podrás olvidarme"},
        {"start": 77.967, "end": 79.1, "text": "JAMAS, aunque lo intentes"},
        {"start": 186.7, "end": 188.2, "text": "POR LA AVENTRUA"},
    ]
    approved = [
        "TENDRÁS una vida mejor",
        "JAMÁS podrás olvidarme",
        "JAMÁS, aunque lo intentes",
        "POR LA AVENTURA",
    ]
    report = build_delivery_preflight(
        metadata={
            "artist": "Enanitos Verdes",
            "title": "Tu Cárcel",
            "version": "Lyric Video / En Vivo Desde Tijuana, Mexico/2004",
            "isrc": "MXUV72602826",
        },
        asset={
            "filename": "Enanitos_Verdes_-_Tu_Carcel_En_Vivo.mov",
            "duration": 241.909,
            "rendered_title": "Tu Carcel_En Vivo",
            "title_time": 1.133,
        },
        segments=segments,
        approved_lyrics=approved,
        reference_trusted=True,
        fps=30,
    )

    assert report["decision"] == "BLOCK"
    assert report["summary"] == {
        "issue_count": 4,
        "fail_count": 1,
        "warn_count": 3,
        "open_count": 4,
        "segment_count": 4,
    }
    by_actual = {item["actual"]: item for item in report["issues"]}
    assert by_actual["Tu Carcel_En Vivo"]["timecode"] == "00:00:01:04"
    assert by_actual["Tu Carcel_En Vivo"]["expected"] == "Tu Cárcel"
    assert by_actual["TENDRAS"]["expected"] == "TENDRÁS"
    assert by_actual["JAMAS"]["occurrence_count"] == 2
    assert by_actual["JAMAS"]["frequency"] == "INTERMITTENT"
    assert by_actual["JAMAS"]["timecodes"] == ["00:01:15:24", "00:01:17:29"]
    assert by_actual["AVENTRUA"]["expected"] == "AVENTURA"
    assert by_actual["AVENTRUA"]["timecode"] == "00:03:06:21"
    assert by_actual["AVENTRUA"]["auto_fixable"] is False


def test_untrusted_catalogue_cannot_raise_lyric_corrections():
    report = build_delivery_preflight(
        metadata={"title": "Live song"},
        asset={"duration": 20},
        segments=[{"start": 1, "end": 3, "text": "Improvised live lyric"}],
        approved_lyrics=["Completely different studio lyric"],
        reference_trusted=False,
    )
    assert report["issues"] == []
    assert report["decision"] == "PASS"
    assert {item["reason"] for item in report["abstentions"]} == {
        "reference_not_trusted", "rendered_title_or_ocr_missing"
    }


def test_timeline_invariants_can_block_delivery():
    report = build_delivery_preflight(
        metadata={"title": "Broken"},
        asset={"duration": 10},
        segments=[
            {"start": 2, "end": 2, "text": "zero"},
            {"start": 9, "end": 12, "text": "outside"},
        ],
    )
    assert report["decision"] == "BLOCK"
    assert {item["code"] for item in report["issues"]} >= {
        "INVALID_LYRIC_RANGE", "LYRIC_OUTSIDE_ASSET"
    }


def test_clean_delivery_passes():
    report = build_delivery_preflight(
        metadata={"artist": "Artist", "title": "Song", "version": "Live"},
        asset={
            "rendered_artist": "Artist", "rendered_title": "Song",
            "rendered_version": "Live",
            "duration": 10,
        },
        segments=[{"start": 1, "end": 3, "text": "Jamás te olvidaré"}],
        approved_lyrics=["Jamás te olvidaré"],
        reference_trusted=True,
    )
    assert report["decision"] == "PASS"
    assert report["issues"] == []


def test_reference_health_blocks_wrong_or_incomplete_catalogue_text():
    report = build_delivery_preflight(
        metadata={"title": "Wrong catalogue"},
        asset={"duration": 200},
        segments=[{"start": 10, "end": 40, "text": "catalogue candidate"}],
        reference_health={
            "text_status": "unsafe_without_witness",
            "timeline_status": "incomplete",
            "allow_vocabulary_reconciliation": False,
            "reasons": ["reference_text_not_attested", "timeline_incomplete"],
            "metrics": {
                "timeline_completion_ratio": 0.2,
                "trailing_gap_s": 160,
            },
        },
    )
    assert report["decision"] == "BLOCK"
    assert {item["code"] for item in report["issues"]} == {
        "REFERENCE_TEXT_UNATTESTED", "REFERENCE_TIMELINE_INCOMPLETE"
    }


def test_report_is_strict_json_even_with_non_finite_upstream_diagnostics():
    report = build_delivery_preflight(
        metadata={"artist": "Artist", "title": "Song"},
        asset={"duration": float("inf")},
        segments=[{
            "start": 1.0, "end": float("nan"), "text": "corrupt timing",
        }],
        quality={"metrics": {"endpoint_error": float("nan")}},
        reference_health={"metrics": {"coverage": float("inf")}},
        acoustic_findings=[{"seconds": float("nan"), "score": float("inf")}],
        fps=float("nan"),
    )

    # allow_nan=False models the strict JSON accepted by PostgreSQL JSONB.
    json.dumps(report, allow_nan=False)
    assert report["asset"]["fps"] == 30.0
    assert report["asset"]["duration"] is None
    assert report["upstream_quality"]["metrics"]["endpoint_error"] is None
    assert report["reference_health"]["metrics"]["coverage"] is None
