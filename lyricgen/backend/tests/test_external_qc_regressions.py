from delivery_preflight import build_delivery_preflight
from external_qc_regressions import (
    evaluate_preflight_recall,
    normalize_external_report,
)


UMG_TU_CARCEL_FINDINGS = [
    {
        "severity": "WARN", "frequency": "ISOLATED",
        "timecode": "00:00:01:04",
        "description": (
            'R2 metadata lists the song title "Tu Cárcel" while the graphic '
            'art video asset displays it as "Tu Carcel_En Vivo"'
        ),
    },
    {
        "severity": "WARN", "frequency": "INTERMITTENT",
        "timecodes": ["00:01:15:24", "00:01:17:29"],
        "description": 'Misspelled in lyrics, "JAMAS" should be "JAMÁS"',
    },
    {
        "severity": "WARN", "frequency": "INTERMITTENT",
        "timecode": "00:01:14:11",
        "description": 'Misspelled in lyrics "TENDRAS" should be "TENDRÁS"',
    },
    {
        "severity": "WARN", "frequency": "ISOLATED",
        "timecode": "00:03:06:21",
        "description": (
            'Sung lyric "POR LA AVENTURA" appears in lyric text as '
            '"POR LA AVENTRUA."'
        ),
    },
]


def _preflight():
    return build_delivery_preflight(
        metadata={"artist": "Enanitos Verdes", "title": "Tu Cárcel"},
        asset={"rendered_title": "Tu Carcel_En Vivo", "title_time": 1.133},
        segments=[
            {"start": 74.367, "end": 75.2, "text": "TENDRAS"},
            {"start": 75.8, "end": 76.6, "text": "JAMAS"},
            {"start": 77.967, "end": 78.8, "text": "JAMAS"},
            {"start": 186.7, "end": 188.0, "text": "POR LA AVENTRUA"},
        ],
        approved_lyrics=None,
        reference_trusted=False,
        fps=30,
    )


def test_umg_report_parses_into_regression_schema():
    report = normalize_external_report(
        source="umg", report_id="umg-job-154061-submission-1",
        findings=UMG_TU_CARCEL_FINDINGS,
    )

    assert report["finding_count"] == 4
    assert [(row["code"], row["actual"], row["expected"])
            for row in report["findings"]] == [
        ("METADATA_TITLE_MISMATCH", "Tu Carcel_En Vivo", "Tu Cárcel"),
        ("LYRIC_ORTHOGRAPHY_MISMATCH", "JAMAS", "JAMÁS"),
        ("LYRIC_ORTHOGRAPHY_MISMATCH", "TENDRAS", "TENDRÁS"),
        ("LYRIC_TOKEN_TYPO", "AVENTRUA", "AVENTURA"),
    ]


def test_umg_tu_carcel_regression_requires_all_four_findings():
    preflight = _preflight()
    result = evaluate_preflight_recall(preflight["issues"], UMG_TU_CARCEL_FINDINGS)

    assert result["gate_passed"] is True
    assert result["caught_count"] == 4
    assert result["missed_count"] == 0
    assert result["recall"] == 1.0


def test_regression_gate_fails_when_one_umg_finding_is_missing():
    issues = [
        row for row in _preflight()["issues"]
        if row["actual"] != "AVENTRUA"
    ]

    result = evaluate_preflight_recall(issues, UMG_TU_CARCEL_FINDINGS)

    assert result["gate_passed"] is False
    assert result["caught_count"] == 3
    assert result["missed_count"] == 1


def test_authentic_umg_schema_normalizes_without_a_fixture_rewrite():
    report = normalize_external_report(
        source="umg", report_id="154061", findings=[
            {
                "description": "Metadata title mismatch",
                "actual": "Tu Carcel_En Vivo", "expected": "Tu Cárcel",
                "reported_timecodes": ["00:00:01:04"],
            },
            {
                "description": "Missing accent",
                "actual": "JAMAS", "expected": "JAMÁS",
                "reported_timecodes": ["00:01:15:24", "00:01:17:29"],
            },
            {
                "description": "Sung lyric mismatch",
                "sung_expected": "AVENTURA",
                "displayed_actual": "AVENTRUA.",
                "reported_timecodes": ["00:03:06:21"],
            },
        ],
    )

    assert [(row["code"], row["actual"], row["expected"], row["timecodes"])
            for row in report["findings"]] == [
        ("METADATA_TITLE_MISMATCH", "Tu Carcel_En Vivo", "Tu Cárcel", ["00:00:01:04"]),
        ("LYRIC_ORTHOGRAPHY_MISMATCH", "JAMAS", "JAMÁS", ["00:01:15:24", "00:01:17:29"]),
        ("LYRIC_TOKEN_TYPO", "AVENTRUA", "AVENTURA", ["00:03:06:21"]),
    ]
