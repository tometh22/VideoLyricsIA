import shutil
import wave
from pathlib import Path

import numpy as np
import pytest

import targeted_consensus as consensus


pytestmark = pytest.mark.skipif(
    shutil.which("ffmpeg") is None,
    reason="ffmpeg is required for the real residual smoke test",
)


def _write_wave(path: Path, frequencies: tuple[float, ...], *, seconds=2.0):
    sample_rate = 16_000
    timeline = np.arange(int(sample_rate * seconds), dtype=np.float32) / sample_rate
    signal = sum(.16 * np.sin(2 * np.pi * frequency * timeline) for frequency in frequencies)
    pcm = np.clip(signal, -.95, .95)
    pcm = (pcm * 32767).astype("<i2")
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(sample_rate)
        handle.writeframes(pcm.tobytes())


def test_real_ffmpeg_residual_is_nonempty_and_maps_back_to_source_clock(tmp_path):
    mix = tmp_path / "mix.wav"
    stem = tmp_path / "stem.wav"
    _write_wave(mix, (220.0, 440.0))
    _write_wave(stem, (220.0,))
    observed = {}

    def transcribe(clip, start, duration, language, job_id):
        observed["clip"] = clip
        observed["args"] = (start, duration, language, job_id)
        with wave.open(clip, "rb") as handle:
            samples = np.frombuffer(handle.readframes(handle.getnframes()), dtype="<i2")
            observed["sample_rate"] = handle.getframerate()
        observed["rms"] = float(np.sqrt(np.mean(samples.astype(np.float32) ** 2)))
        return [{"word": "uoh", "start": .10, "end": .40}]

    words = consensus._transcribe_residual_window(
        str(mix), str(stem), .25, .75, "es", "job-smoke", transcribe,
    )

    assert observed["args"] == (0.0, .75, "es", "job-smoke")
    assert observed["sample_rate"] == 16_000
    assert observed["rms"] > 100.0
    assert words == [{
        "word": "uoh",
        "start": .35,
        "end": .65,
        "audio_view": "derived_residual",
        "correlated_family": "source_audio_demucs",
    }]
    assert not Path(observed["clip"]).exists()
