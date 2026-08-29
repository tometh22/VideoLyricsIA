import pytest

from eval.classify_errors import submit
from eval.prepare_lora import _chunks
from eval.train_whisper_lora import validate_manifest


def test_external_taxonomy_submit_is_policy_blocked_by_default(tmp_path, monkeypatch):
    monkeypatch.delenv("ALLOW_EXTERNAL_CLIENT_TEXT_BATCH", raising=False)
    with pytest.raises(RuntimeError, match="policy-blocked"):
        submit(tmp_path / "client-text.jsonl", tmp_path / "batch.json")


def test_lora_chunks_never_exceed_25_seconds():
    source = [{
        "start_s": 10.0,
        "end_s": 44.2,
        "text": "una frase extensa que necesita dividirse sin duplicar etiquetas",
    }]
    chunks = _chunks(source, maximum_s=25.0)
    assert len(chunks) == 2
    assert all(chunk["end_s"] - chunk["start_s"] <= 25.0 for chunk in chunks)
    assert " ".join(chunk["text"] for chunk in chunks) == source[0]["text"]


def test_umg_lora_executor_is_policy_blocked_by_default(tmp_path, monkeypatch):
    audio = tmp_path / "audio.wav"
    audio.write_bytes(b"placeholder")
    manifest = tmp_path / "samples.jsonl"
    manifest.write_text(__import__("json").dumps({
        "sample_id": "s-1", "song_id": "song-1", "audio_path": str(audio),
        "start_s": 0, "end_s": 1, "text": "hola", "language": "es",
    }) + "\n")
    monkeypatch.delenv("ALLOW_UMG_TRAINING", raising=False)
    with pytest.raises(RuntimeError, match="policy-blocked"):
        validate_manifest(manifest, "umg")


def test_research_lora_manifest_requires_license(tmp_path):
    audio = tmp_path / "audio.wav"
    audio.write_bytes(b"placeholder")
    manifest = tmp_path / "samples.jsonl"
    manifest.write_text(__import__("json").dumps({
        "sample_id": "s-1", "song_id": "song-1", "audio_path": str(audio),
        "start_s": 0, "end_s": 1, "text": "hola", "language": "es",
    }) + "\n")
    with pytest.raises(ValueError, match="license"):
        validate_manifest(manifest, "research")
