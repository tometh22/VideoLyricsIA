"""False-positive-sensitive tests for the physical film-frame detector."""

import numpy as np

from film_frame_guard import (
    FilmFrameArtifact,
    _crop_fraction,
    detect_from_grayscale_frames,
)


W, H = 640, 360


def _moving_scene(n=8, value=150):
    frames = []
    for i in range(n):
        frame = np.full((H, W), value, dtype=np.uint8)
        # Moving dark scene detail: it must not survive the temporal AND mask.
        x = 180 + i * 16
        frame[220:280, x:x + 90] = 12
        frames.append(frame)
    return frames


def _add_physical_frame(frames):
    for frame in frames:
        frame[0, :] = 4
        frame[-1, :] = 4
        frame[:, 0] = 4
        frame[:, -1] = 4


def _add_sprocket(frames, *, reflected=False):
    x0, x1 = (W - 56, W - 7) if reflected else (7, 56)
    for frame in frames:
        frame[154:217, x0:x1] = 3


def test_detects_only_compound_frame_plus_sprocket_signature():
    frames = _moving_scene()
    _add_physical_frame(frames)
    _add_sprocket(frames)

    finding = detect_from_grayscale_frames(frames)

    assert finding is not None
    assert finding.x == 7
    assert finding.y == 154
    assert finding.fill_ratio == 1.0
    assert finding.samples == 8


def test_dark_rectangle_without_four_sided_frame_is_not_flagged():
    """Windows, signs, mirrors and cars must not be classified by shape alone."""
    frames = _moving_scene()
    _add_sprocket(frames)

    assert detect_from_grayscale_frames(frames) is None


def test_four_sided_frame_without_inner_rectangle_is_not_flagged():
    """A decorative border alone is not enough to trigger remediation."""
    frames = _moving_scene()
    _add_physical_frame(frames)

    assert detect_from_grayscale_frames(frames) is None


def test_letterbox_is_not_mistaken_for_physical_film_frame():
    frames = _moving_scene()
    for frame in frames:
        frame[:36, :] = 3
        frame[-36:, :] = 3
    _add_sprocket(frames)

    assert detect_from_grayscale_frames(frames) is None


def test_moving_dark_object_is_not_screen_fixed_artifact():
    frames = _moving_scene(value=180)
    _add_physical_frame(frames)
    for i, frame in enumerate(frames):
        x = 7 + i * 12
        frame[145:215, x:x + 48] = 3

    assert detect_from_grayscale_frames(frames) is None


def test_reflected_right_edge_signature_is_detected():
    frames = _moving_scene()
    _add_physical_frame(frames)
    _add_sprocket(frames, reflected=True)

    finding = detect_from_grayscale_frames(frames)

    assert finding is not None
    assert finding.x == W - 56


def test_fewer_than_three_frames_fails_closed_to_no_detection():
    frames = _moving_scene(n=2)
    _add_physical_frame(frames)
    _add_sprocket(frames)

    assert detect_from_grayscale_frames(frames) is None


def test_crop_clears_left_component_with_small_safety_margin():
    finding = FilmFrameArtifact(
        x=7, y=154, width=49, height=63,
        frame_width=W, frame_height=H,
        fill_ratio=0.96, area_ratio=0.013,
        edge_black_ratios=(0.99, 0.99, 0.99, 0.99), samples=8,
    )

    crop = _crop_fraction(finding)

    assert crop > (finding.x + finding.width) / W
    assert crop == 0.095


def test_crop_is_symmetric_for_reflected_right_component():
    left = FilmFrameArtifact(
        x=7, y=154, width=49, height=63,
        frame_width=W, frame_height=H,
        fill_ratio=0.96, area_ratio=0.013,
        edge_black_ratios=(0.99,) * 4, samples=8,
    )
    right = FilmFrameArtifact(
        x=W - 56, y=154, width=49, height=63,
        frame_width=W, frame_height=H,
        fill_ratio=0.96, area_ratio=0.013,
        edge_black_ratios=(0.99,) * 4, samples=8,
    )

    assert _crop_fraction(left) == _crop_fraction(right)


def test_crop_has_a_hard_cap_for_unexpected_detection_geometry():
    finding = FilmFrameArtifact(
        x=20, y=100, width=80, height=70,
        frame_width=W, frame_height=H,
        fill_ratio=0.96, area_ratio=0.02,
        edge_black_ratios=(0.99,) * 4, samples=8,
    )

    assert _crop_fraction(finding) == 0.105
