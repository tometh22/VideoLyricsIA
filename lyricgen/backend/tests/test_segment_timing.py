import json
from pathlib import Path

from segment_timing import (
    normalize_segments_timing,
    normalize_editor_segments,
    sort_segments_chronologically,
    timing_anomalies,
)
from editor import normalize_segments


FIXTURE = Path(__file__).parent / "fixtures" / "job_42_editor_segments.json"


def test_repairs_regressed_start_without_reordering_lyrics():
    segments = [
        {"start": 45.0, "end": 45.8, "text": "uoo"},
        {"start": 45.9, "end": 46.4, "text": "te alejaste"},
        {"start": 45.1, "end": 46.0, "text": "Las palabras"},
        {"start": 47.0, "end": 47.8, "text": "No fue"},
    ]

    normalized = normalize_segments_timing(segments)

    assert [segment["text"] for segment in normalized] == [
        "uoo",
        "te alejaste",
        "Las palabras",
        "No fue",
    ]
    assert [segment["start"] for segment in normalized] == sorted(
        segment["start"] for segment in normalized
    )
    assert normalized[2]["start"] == 45.95
    assert normalized[2]["end"] > normalized[2]["start"]


def test_repairs_preserve_the_original_line_duration_when_shifted():
    segments = [
        {"start": 10.0, "end": 10.5, "text": "first"},
        {"start": 9.0, "end": 9.8, "text": "second"},
    ]

    normalized = normalize_segments_timing(segments)

    assert normalized[1]["start"] == 10.05
    assert normalized[1]["end"] == 10.85


def test_editor_order_repairs_tail_duplicates_without_changing_timestamps():
    segments = [
        {"_id": 22, "start": 114.766, "end": 115.967, "text": "¡Gracias!"},
        {"_id": 23, "start": 45.1106, "end": 45.8752, "text": "uoo no no te hice daño,"},
        {"_id": 24, "start": 45.9252, "end": 46.5273, "text": "te alejaste de mi"},
    ]

    ordered = sort_segments_chronologically(segments)

    assert [segment["_id"] for segment in ordered] == [23, 24, 22]
    assert [segment["start"] for segment in ordered] == [45.1106, 45.9252, 114.766]


def test_editor_canonicalization_repairs_regressed_overlap_and_drops_copies():
    payload = json.loads(FIXTURE.read_text())

    canonical = normalize_editor_segments(payload["segments"])

    assert [segment["_id"] for segment in canonical] == [9, 10, 11, 22]
    assert [segment["start"] for segment in canonical] == [
        45.1106, 45.9252, 46.5773, 114.766,
    ]
    assert canonical[1]["end"] + 0.05 <= canonical[2]["start"]
    assert all(
        current["start"] >= previous["start"]
        for previous, current in zip(canonical, canonical[1:])
    )


def test_stable_sort_keeps_duplicate_timestamps_deterministic():
    segments = [
        {"_id": 3, "start": 10.0, "end": 11.0, "text": "first"},
        {"_id": 4, "start": 10.0, "end": 11.0, "text": "duplicate"},
    ]

    assert [segment["_id"] for segment in sort_segments_chronologically(segments)] == [3, 4]


def test_real_job_shape_is_detected_before_repair():
    payload = json.loads(FIXTURE.read_text())

    assert timing_anomalies(payload["segments"]) == {
        "regressions": 2,
        "overlaps": 4,
        "duplicate_starts": 2,
    }


def test_editor_repair_keeps_original_baseline_separate():
    payload = json.loads(FIXTURE.read_text())

    ordered = sort_segments_chronologically(payload["segments"])

    assert payload["original_segments"][0]["start"] == 40.25
    assert [segment["_id"] for segment in ordered] == [9, 23, 11, 10, 24, 22]


def test_editor_persistence_normalizer_canonicalizes_the_durable_order():
    payload = json.loads(FIXTURE.read_text())

    normalized = normalize_segments(payload["segments"])

    assert [segment["_id"] for segment in normalized] == [9, 10, 11, 22]
    assert [segment["start"] for segment in normalized] == [
        45.1106, 45.9252, 46.5773, 114.766,
    ]


def test_final_normalization_repairs_postpass_duplicate_starts():
    """Adlib/word postpasses can replace rows after the first emit guard."""
    segments = [
        {"start": 5.54, "end": 16.99, "text": "Hoy temprano"},
        {"start": 19.50, "end": 23.37, "text": "Paso el tiempo"},
        {"start": 47.65, "end": 52.52, "text": "Oh no, no, no"},
        {"start": 47.65, "end": 58.13, "text": "Y te alejaste de mí"},
    ]

    normalized = normalize_segments_timing(segments)

    assert [s["text"] for s in normalized] == [s["text"] for s in segments]
    assert [s["start"] for s in normalized] == [5.54, 19.5, 47.65, 47.7]
    assert timing_anomalies(normalized)["regressions"] == 0
    assert timing_anomalies(normalized)["duplicate_starts"] == 0
    assert round(normalized[2]["end"] - normalized[2]["start"], 2) == 4.87
