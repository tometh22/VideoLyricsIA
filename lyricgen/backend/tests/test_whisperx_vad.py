"""VAD sensitivity params for whisperX (fix/whisperx-vad-sensitivity).

Pins: (1) lowered defaults are on by default, (2) env overrides work,
(3) vad params are part of the cache key so a tweak can't serve stale results.
Pure — no network, no Replicate, no audio model.
"""
import os
import tempfile

import whisperx_transcribe as wx


def test_vad_params_default_is_lowered():
    """On by default: recovers sustained ad-libs the model defaults gate out."""
    for k in ("WHISPERX_VAD_ONSET", "WHISPERX_VAD_OFFSET"):
        os.environ.pop(k, None)
    assert wx._vad_params() == (0.2, 0.1)


def test_vad_params_env_override():
    os.environ["WHISPERX_VAD_ONSET"] = "0.5"
    os.environ["WHISPERX_VAD_OFFSET"] = "0.363"
    try:
        assert wx._vad_params() == (0.5, 0.363)
    finally:
        os.environ.pop("WHISPERX_VAD_ONSET", None)
        os.environ.pop("WHISPERX_VAD_OFFSET", None)


def test_vad_params_bad_env_falls_back():
    os.environ["WHISPERX_VAD_ONSET"] = "not-a-number"
    try:
        assert wx._vad_params() == (0.2, 0.1)
    finally:
        os.environ.pop("WHISPERX_VAD_ONSET", None)


def test_cache_key_includes_vad():
    """Different VAD → different cache key (else a tweak serves stale results)."""
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
        f.write(b"fake audio bytes for hashing")
        path = f.name
    try:
        k_default, _, _ = wx._compute_cache_key(path, "es", None, 0.5, 0.363)
        k_low, _, _ = wx._compute_cache_key(path, "es", None, 0.2, 0.1)
        assert k_default and k_low and k_default != k_low
        assert "vo0.2" in k_low and "vf0.1" in k_low
    finally:
        os.unlink(path)
