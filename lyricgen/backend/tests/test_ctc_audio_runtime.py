"""Exercise native audio decoding, resampling and alignment without downloads."""

import numpy as np
import pytest
import soundfile as sf
import subprocess
import torch
import torchaudio.functional as AF

from ctc_audio import audio_info, load_audio
from ctc_align import _load_window


@pytest.mark.parametrize("extension,subtype", [("wav", "PCM_16"), ("wav", "FLOAT"), ("flac", "PCM_16")])
def test_bounded_stereo_decode_preserves_samples(tmp_path, extension, subtype):
    # Exactly representable PCM16 samples expose normalization/channel errors.
    samples = np.array([[0, 16384], [-16384, 8192], [32767, -32768], [4096, 0]], dtype=np.int16)
    expected = samples.astype(np.float32) / 32768
    path = tmp_path / f"audio.{extension}"
    sf.write(path, expected, 48000, subtype=subtype)

    info = audio_info(path)
    assert (info.sample_rate, info.num_frames) == (48000, 4)
    audio, rate = load_audio(path, frame_offset=1, num_frames=2)
    assert rate == 48000
    assert audio.dtype == torch.float32
    np.testing.assert_array_equal(audio.numpy(), expected[1:3].T)
    tail, _ = load_audio(path, frame_offset=3, num_frames=10)
    np.testing.assert_array_equal(tail.numpy(), expected[3:].T)
    empty, _ = load_audio(path, frame_offset=4, num_frames=1)
    assert empty.shape == (2, 0)


def test_window_downmixes_and_resamples_only_requested_interval(tmp_path):
    rate = 48000
    time = np.arange(rate * 2, dtype=np.float32) / rate
    samples = np.column_stack((np.sin(2 * np.pi * 220 * time), np.sin(2 * np.pi * 440 * time)))
    path = tmp_path / "stereo.wav"
    sf.write(path, samples, rate, subtype="FLOAT")

    actual = _load_window(path, 0.5, 1.0)
    expected = AF.resample(torch.from_numpy(samples[24000:48000].mean(axis=1)).unsqueeze(0), rate, 16000)
    assert actual.shape == (1, 8000)
    torch.testing.assert_close(actual, expected)
    assert torch.isfinite(actual).all()


def test_native_forced_alignment_retains_repeated_token_boundaries():
    path = [0, 1, 1, 0, 1, 1, 2, 2, 0]
    logits = torch.full((1, len(path), 3), -10.0)
    for frame, token in enumerate(path):
        logits[0, frame, token] = 10.0
    aligned, scores = AF.forced_align(
        logits.log_softmax(-1), torch.tensor([[1, 1, 2]], dtype=torch.int32), blank=0,
    )
    assert aligned.tolist() == [path]
    spans = AF.merge_tokens(aligned[0], scores[0].exp(), blank=0)
    assert [(span.token, span.start, span.end) for span in spans] == [(1, 1, 3), (1, 4, 6), (2, 6, 8)]


def test_mp3_upload_can_be_decoded_and_windowed(tmp_path):
    rate = 44100
    time = np.arange(rate, dtype=np.float32) / rate
    samples = 0.5 * np.sin(2 * np.pi * 440 * time)
    wav, mp3 = tmp_path / "source.wav", tmp_path / "upload.mp3"
    sf.write(wav, samples, rate)
    subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(wav), "-codec:a", "libmp3lame", str(mp3)],
        check=True, capture_output=True, timeout=30,
    )
    info = audio_info(mp3)
    assert info.sample_rate == rate
    decoded, _ = load_audio(mp3, frame_offset=rate // 4, num_frames=rate // 2)
    assert decoded.shape == (1, rate // 2)
    # MP3 is lossy; verify the signal survives, not bit-for-bit equality.
    assert np.corrcoef(decoded[0].numpy(), samples[rate // 4:rate // 4 + rate // 2])[0, 1] > 0.99
    assert _load_window(mp3, 0.25, 0.75).shape == (1, 8000)


def test_whisper_decoder_forward_with_new_torch():
    # A tiny random model verifies the actual local Whisper runtime, without
    # a network download or dependence on a particular transcription output.
    from whisper.model import ModelDimensions, Whisper

    dims = ModelDimensions(
        n_mels=80, n_audio_ctx=8, n_audio_state=16, n_audio_head=2, n_audio_layer=1,
        n_vocab=32, n_text_ctx=8, n_text_state=16, n_text_head=2, n_text_layer=1,
    )
    model = Whisper(dims).eval()
    with torch.inference_mode():
        # Pretrained weights normally populate this torch.empty parameter.
        model.decoder.positional_embedding.zero_()
        logits = model(torch.zeros(1, 80, 16), torch.tensor([[1, 2, 3]]))
    assert logits.shape == (1, 3, 32)
    assert torch.isfinite(logits).all()
