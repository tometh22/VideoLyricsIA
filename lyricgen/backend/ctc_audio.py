"""Bounded audio decoding independent of TorchAudio's removed I/O APIs."""
from types import SimpleNamespace


def audio_info(path):
    import soundfile

    info = soundfile.info(path)
    return SimpleNamespace(sample_rate=info.samplerate, num_frames=info.frames)


def load_audio(path, *, frame_offset=0, num_frames=-1):
    import soundfile
    import torch

    # Read only the requested frames. Preserve TorchAudio's channels-first,
    # normalized float32 representation before mono conversion/resampling.
    with soundfile.SoundFile(path) as source:
        source.seek(frame_offset)
        samples = source.read(frames=num_frames, dtype="float32", always_2d=True)
        return torch.from_numpy(samples.T.copy()), source.samplerate
