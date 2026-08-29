import pytest

from eval.classify_errors import submit
from eval.prepare_lora import _chunks


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
