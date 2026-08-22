import pytest

from quality_windows import parent_coverage, tile_unsafe_windows


def test_tiles_cover_long_window_without_truncation():
    tiles = tile_unsafe_windows([{"id": "tail", "start": 40, "end": 100}])
    assert [(row["core_start"], row["core_end"]) for row in tiles] == [
        (40.0, 64.0), (64.0, 88.0), (88.0, 100.0),
    ]
    assert all(row["end"] - row["start"] <= 30.0 for row in tiles)
    assert all(row["analysis_truncated"] is False for row in tiles)


def test_parent_requires_every_tile():
    tiles = tile_unsafe_windows([{"id": "tail", "start": 0, "end": 60}])
    partial = parent_coverage(tiles, {tiles[0]["id"], tiles[1]["id"]})
    assert partial["tail"]["complete"] is False
    complete = parent_coverage(tiles, {row["id"] for row in tiles})
    assert complete["tail"]["complete"] is True


@pytest.mark.parametrize(
    ("duration", "expected_cores"),
    [
        (44.999, [(0.0, 24.0), (24.0, 44.999)]),
        (45.0, [(0.0, 24.0), (24.0, 45.0)]),
        (60.0, [(0.0, 24.0), (24.0, 48.0), (48.0, 60.0)]),
    ],
)
def test_boundary_windows_clamp_context_to_audio_without_losing_parent_coverage(
    duration, expected_cores,
):
    tiles = tile_unsafe_windows(
        [{"id": "whole-audio", "start": 0, "end": duration}],
        audio_duration=duration,
    )

    assert [(row["core_start"], row["core_end"]) for row in tiles] == expected_cores
    assert tiles[0]["start"] == 0.0
    assert tiles[-1]["end"] == duration
    assert all(0.0 <= row["start"] < row["end"] <= duration for row in tiles)
    coverage = parent_coverage(tiles, {row["id"] for row in tiles})
    assert coverage["whole-audio"] == {
        "tiles_total": len(expected_cores),
        "tiles_processed": len(expected_cores),
        "complete": True,
    }


@pytest.mark.parametrize("duration", [44.9994, 44.9996])
def test_submillisecond_media_bounds_are_not_rounded_into_false_coverage(duration):
    tiles = tile_unsafe_windows(
        [{"id": "precision", "start": 0, "end": duration}],
        audio_duration=duration,
    )

    assert tiles[-1]["core_end"] == duration
    assert tiles[-1]["end"] == duration
    assert all(row["end"] <= duration for row in tiles)
    assert parent_coverage(
        tiles, {row["id"] for row in tiles},
    )["precision"]["complete"] is True


def test_window_level_audio_duration_clamps_parent_and_context():
    tiles = tile_unsafe_windows([{
        "id": "tail", "start": 40, "end": 70, "audio_duration": 60,
    }])

    assert [(row["core_start"], row["core_end"]) for row in tiles] == [(40.0, 60.0)]
    assert tiles[0]["start"] == 37.0
    assert tiles[0]["end"] == 60.0
    assert tiles[0]["analysis_truncated"] is True
    assert tiles[0]["coverage_complete"] is False
    coverage = parent_coverage(tiles, {tiles[0]["id"]})
    assert coverage["tail"] == {
        "tiles_total": 1, "tiles_processed": 1, "complete": False,
        "truncated": True, "tiles_truncated": 1,
    }


def test_parent_coverage_refuses_manually_truncated_sibling_even_when_processed():
    tiles = tile_unsafe_windows([{"id": "tail", "start": 0, "end": 30}])
    tiles[-1]["analysis_truncated"] = True
    tiles[-1]["coverage_complete"] = False

    coverage = parent_coverage(tiles, {row["id"] for row in tiles})

    assert coverage["tail"]["complete"] is False
    assert coverage["tail"]["tiles_truncated"] == 1


@pytest.mark.parametrize("duration", [0, -1, float("nan"), float("inf"), "bad"])
def test_invalid_explicit_audio_duration_fails_closed(duration):
    with pytest.raises(ValueError, match="audio_duration"):
        tile_unsafe_windows(
            [{"id": "tail", "start": 0, "end": 45}],
            audio_duration=duration,
        )
