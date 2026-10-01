"""Pinned pretrained singing-activity adapter, not an endpoint heuristic.

Uses unmodified CPJKU/veracity inference network and checkpoint. Outputs raw
learned activity probabilities only; no threshold, boundary or auto-repair.
"""
import importlib.util
from pathlib import Path
import subprocess
import time
from reviewer_shadow_audio import pcm, file_sha

REVISION = '0983900f136173015f3c5d0b116be014edd33905'


def run_activity(vendor, audio, window):
    import torch
    import torchaudio
    vendor = Path(vendor)
    if subprocess.check_output(['git', '-C', str(vendor), 'rev-parse', 'HEAD'], text=True).strip() != REVISION:
        raise ValueError('unapproved_activity_revision')
    if subprocess.check_output(['git', '-C', str(vendor), 'status', '--porcelain'], text=True).strip():
        raise ValueError('modified_activity_vendor')
    started = time.monotonic()
    torch.set_num_threads(2)
    spec = importlib.util.spec_from_file_location('reviewer_pinned_svd', vendor/'svd/core/model.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    config = {'n_mels': 80, 'sample_rate': 22050, 'f_min': 27.5, 'f_max': 8000,
        'n_stft': 513, 'frame_len': 1024, 'filterbank': 'mel_orig', 'spec_len': 115,
        'magscale': 'log', 'arch.batch_norm': 1, 'arch.convdrop': 'none',
        'arch': 'ismir2015', 'arch.firstconv_zeromean': '0mean'}
    net = module.SingingVoiceDetector(config).eval()
    checkpoint = vendor/'dataset/jamendo/models/model_log_0mean/model.pt'
    net.load_state_dict(torch.load(checkpoint, map_location='cpu', weights_only=True), strict=True)
    rate, hop = 22050, 315
    # Decode from original zero, preserving exact frame clock. No stem transfer.
    signal = pcm(audio, rate=rate).copy()
    start, end = window['start'], window['end']
    wave = torch.from_numpy(signal[int(start*rate):int(end*rate)])
    spectrum = torchaudio.transforms.Spectrogram(n_fft=1024, hop_length=hop, power=1.)(wave).T
    # Original predictor pads 57 frames on both sides for one output per input.
    padding = torch.zeros(57, 513)
    x = torch.cat([padding, spectrum, padding])[None,None]
    with torch.inference_mode():
        probabilities = net.probabilities(x).tolist()
    if len(probabilities) != len(spectrum):
        raise ValueError('activity_frame_clock_mismatch')
    return {'model': 'CPJKU/veracity/model_log_0mean', 'revision': REVISION,
        'checkpoint_sha256': file_sha(checkpoint), 'window': window,
        'audio_sha256': file_sha(audio), 'clock': 'original_mix_decoded',
        'output_kind': 'singing_activity_probability', 'phoneme_output': False,
        'perceptual_endpoint': False, 'spanish_accuracy_validated': False,
        'input_language_constraint': 'none_no_tokenizer', 'receptive_field_seconds': 115/70,
        'frame_step_seconds': hop/rate,
        'frames': [{'time': start+i*hop/rate, 'probability': p} for i,p in enumerate(probabilities)],
        'latency_seconds': round(time.monotonic()-started, 3), 'provider_calls': 0}
