"""Optional Qwen3-ASR listener for an isolated Apple Silicon MLX environment.

No model download occurs here. Install mlx-audio separately and supply a local,
immutable checkpoint directory; inference receives audio and language only.
"""
from pathlib import Path
import os

from reviewer_shadow_audio import file_sha
from shadow_reference_import import digest


def transcribe_without_reference(model, clip):
    result = model.generate(str(clip), language="Spanish", max_tokens=384,
                            temperature=0.0, batch_size=1, chunk_duration=24.0,
                            verbose=False, system_prompt=None, hotwords=None)
    text = result.text
    if not isinstance(text, str):
        raise ValueError("invalid_qwen_output")
    return text.strip()


class LocalQwenMLX:
    def __init__(self, model_path: Path, *, memory_limit_bytes=2 * 1024**3):
        path = Path(model_path).resolve()
        if not path.is_dir() or not (path / "config.json").is_file():
            raise ValueError("local_qwen_checkpoint_required")
        weights = sorted(path.glob("*.safetensors"))
        if not weights:
            raise ValueError("local_qwen_weights_missing")
        identity_files = sorted({*weights, *path.glob("*.json"), *path.glob("*.txt"), *path.glob("*.jinja")})
        identity = {p.name: file_sha(p) for p in identity_files}
        os.environ.update(HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1")
        import mlx.core as mx
        from mlx_audio.stt.utils import load_model
        mx.set_memory_limit(memory_limit_bytes)
        mx.set_cache_limit(128 * 1024**2)
        self.model = load_model(path, strict=True)
        self.model_id = "local-qwen3-asr-mlx-" + digest(identity)

    def listen(self, clip: Path) -> str:
        return transcribe_without_reference(self.model, clip)
