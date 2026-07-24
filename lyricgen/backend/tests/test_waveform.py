"""Unit tests for the waveform peak-envelope math (waveform_utils).

The endpoint's R2 download + librosa + cache wiring is validated on
staging with a real job; here we lock the pure downsample/normalize
contract that the timeline canvas depends on."""

import pytest

np = pytest.importorskip("numpy")

from waveform_utils import peak_envelope


def test_empty_input_returns_zeros():
    assert peak_envelope(np.array([]), 8) == [0.0] * 8
    assert peak_envelope(None, 5) == [0.0] * 5


def test_output_length_matches_n_even_when_shorter_than_n():
    out = peak_envelope(np.array([0.5, 0.9, 0.2]), 8)
    assert len(out) == 8  # array_split pads buckets, never fewer than n


def test_normalized_to_unit_peak():
    out = peak_envelope(np.array([0.1, 0.2, 0.4, 0.8]), 4)
    assert max(out) == 1.0           # loudest bucket pegs at 1.0
    assert all(0.0 <= v <= 1.0 for v in out)


def test_peak_per_bucket_not_mean():
    # First bucket has a spike, second is quiet. Peak (not mean) must
    # surface the spike → first bucket pegs ~1.0, second stays low.
    amp = np.concatenate([np.array([0.0, 1.0]), np.array([0.05, 0.05])])
    out = peak_envelope(amp, 2)
    assert out[0] == 1.0
    assert out[1] < 0.2
