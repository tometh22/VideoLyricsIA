from __future__ import annotations

import json

from lora_v1 import data_improvement_curve, evaluate_predictions, song_bootstrap, word_error_rate
from scripts.evaluate_lora_v1 import _canonical_ids
from scripts.train_lora_v1 import _training_rows, load_training_rows


def _rows(hypotheses):
    return [
        {"song_id": "s1", "artist": "a", "difficulty": "easy", "reference": "hola mundo", "hypothesis": hypotheses[0]},
        {"song_id": "s2", "artist": "b", "difficulty": "difficult", "reference": "buenos dias", "hypothesis": hypotheses[1]},
    ]


def test_word_error_rate_normalizes_accents():
    assert word_error_rate("Canción", "cancion") == 0.0


def test_evaluation_reports_easy_difficult_and_family_gate():
    baseline = _rows(["hola", "buenos dias malos"])
    candidate = _rows(["hola mundo", "buenos dias"])
    report = evaluate_predictions(baseline, candidate, canonical_song_ids={"s1", "s2"})
    assert set(report["partitions"]) == {"overall", "easy", "difficult"}
    assert report["partitions"]["overall"]["relative_improvement"] > 0
    assert report["replacement_gate"]["additional_family_only"] is True
    assert report["replacement_gate"]["runtime_replacement_allowed"] is False


def test_song_bootstrap_is_deterministic():
    left = song_bootstrap({"s1": 0.1, "s2": 0.2})
    right = song_bootstrap({"s1": 0.1, "s2": 0.2})
    assert left == right
    assert left["songs"] == 2


def test_data_curve_marks_saturation():
    baseline = _rows(["hola", "buenos dias malos"])
    candidate = _rows(["hola mundo", "buenos dias"])
    curve = data_improvement_curve(baseline, candidate, sample_sizes=(0, 1, 5), canonical_song_ids={"s1", "s2"})
    assert curve[0]["relative_improvement"] == 0.0
    assert curve[-1]["saturated"] is True


def test_evaluator_accepts_preparation_report_as_cohort(tmp_path):
    report = tmp_path / "manifest.json"
    report.write_text(json.dumps({"canonical_eval_cohort": {"song_ids": ["s1"]}}))
    assert _canonical_ids(report) == {"s1"}


def test_historical_pairs_require_complete_evidence_and_audio_map(tmp_path):
    audio = tmp_path / "history.wav"
    audio.write_bytes(b"not decoded here")
    samples = tmp_path / "samples.jsonl"
    samples.write_text(json.dumps({
        "sample_id": "s-0", "song_id": "s", "audio_path": str(audio),
        "start_s": 0.0, "end_s": 1.0, "text": "hola", "language": "es",
    }) + "\n")
    history = tmp_path / "history.jsonl"
    history.write_text("\n".join([
        json.dumps({"complete": False, "job_id": "bad"}),
        json.dumps({
            "complete": True, "job_id": "good",
            "metadata": {"artist": "a"},
            "approved": {"segments": [{"start": 0.0, "end": 1.0, "text": "mundo"}]},
        }),
    ]) + "\n")
    audio_map = tmp_path / "audio-map.json"
    audio_map.write_text(json.dumps({"good": str(audio)}))
    rows, stats = load_training_rows(
        samples, historical_paths=[history], historical_audio_map=audio_map,
    )
    assert len(rows) == 2
    assert rows[-1]["source"] == "historical_pair"
    assert stats["rejected_incomplete"] == 1
    assert stats["accepted"] == 1


def test_leave_artist_out_rows_are_not_in_train_or_validation():
    rows = [
        {"song_id": f"s{i}", "artist": artist, "eval_only": False}
        for i, artist in enumerate(("a", "a", "b", "c", "d"))
    ]
    train, validation, held = _training_rows(rows)
    held_artists = {row["artist"] for row in held}
    assert held_artists
    assert all(row["artist"] not in held_artists for row in train + validation)
