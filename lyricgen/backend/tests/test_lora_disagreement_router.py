import json
from pathlib import Path

from scripts.pilot_lora_disagreement_router import (
    auc,
    build_report,
    sample_disagreement,
)


def test_sample_disagreement_ignores_case_and_punctuation():
    assert sample_disagreement("Hola, mundo!", "hola mundo") == (0, 2)
    edits, tokens = sample_disagreement("hola mundo", "hola ruido")
    assert edits == 1
    assert tokens == 2


def test_auc_uses_difficult_as_positive_class():
    assert auc([0.1, 0.9, 0.2, 0.8], [False, True, False, True]) == 1.0


def test_build_report_marks_reconstructed_as_non_gate(tmp_path: Path):
    base = tmp_path / "base.jsonl"
    lora = tmp_path / "lora.jsonl"
    evaluation = tmp_path / "evaluation.json"
    rows = [
        {"sample_id": "a-0", "song_id": "a", "artist": "A", "hypothesis": "hola mundo"},
        {"sample_id": "b-0", "song_id": "b", "artist": "B", "hypothesis": "hola mundo"},
    ]
    candidate = [
        {"sample_id": "a-0", "song_id": "a", "artist": "A", "hypothesis": "hola mundo"},
        {"sample_id": "b-0", "song_id": "b", "artist": "B", "hypothesis": "ruido ruido"},
    ]
    base.write_text("\n".join(json.dumps(row) for row in rows) + "\n")
    lora.write_text("\n".join(json.dumps(row) for row in candidate) + "\n")
    evaluation.write_text(json.dumps({"by_song": {
        "a": {"baseline_wer": 0.02},
        "b": {"baseline_wer": 0.80, "raw_quality": "reconstructed"},
    }}))
    report = build_report([("reconstructed", base, lora, evaluation)], bootstrap_iterations=50)
    assert report["songs"] == 2
    assert report["difficult_songs"] == 1
    assert report["by_raw_quality"] == {"reconstructed": 2}
    assert report["runtime_uses_gold"] is False
    assert report["auc"]["estimate"] == 1.0
