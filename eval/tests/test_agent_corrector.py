import json
from pathlib import Path

import pytest

from eval.agent_corrector import canonical_family, run_agent, score, validate_agent_response, validate_proposal


def _proposal(*families, category="text", text="hola"):
    return {
        "candidate_id": "candidate-1",
        "category": category,
        "value": {"text": text} if category != "timing" else {"start": 1.0, "end": 2.0},
        "supporting_families": [{"name": family, "group": "untrusted-producer-value"} for family in families],
    }


def _write_jsonl(path: Path, rows):
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")


def test_family_variants_are_not_independent_and_agent_cannot_be_source():
    assert canonical_family("faster-whisper-large-v2") == "whisper"
    assert canonical_family("openai/whisper-large-v3") == "whisper"
    valid, reason = validate_proposal(_proposal("whisper-large-v2", "whisper-large-v3"))
    assert not valid
    assert reason == "fewer_than_two_independent_family_groups"
    valid, reason = validate_proposal(_proposal("whisper-large-v3", "Gemini-2.5-Pro"))
    assert not valid
    assert reason == "agent_family_cannot_be_candidate_source"
    assert validate_proposal(_proposal("whisper-large-v3", "qwen3-asr")) == (True, "ok")


def test_gold_derived_candidate_is_rejected():
    proposal = _proposal("whisper-large-v3", "qwen3-asr")
    proposal["derived_from_approved"] = True
    assert validate_proposal(proposal)[1] == "approved_gold_provenance_forbidden"


def test_verified_deletion_is_a_valid_candidate():
    proposal = _proposal("whisper-large-v3", "qwen3-asr")
    proposal["value"] = {"delete": True}
    assert validate_proposal(proposal) == (True, "ok")


def test_agent_can_only_choose_or_minimally_edit_supplied_candidate():
    request = {"zone_id": "song:1", "proposals": [_proposal("whisper-large-v3", "qwen3-asr", text="quiero volar")]}
    chosen = validate_agent_response(request, {"decisions": [{
        "category": "text", "action": "choose_candidate", "candidate_id": "candidate-1",
    }]})
    assert chosen["decisions"][0]["value"]["text"] == "quiero volar"
    edited = validate_agent_response(request, {"decisions": [{
        "category": "text", "action": "edit_candidate", "candidate_id": "candidate-1",
        "value": {"text": "quiero votar"},
    }]})
    assert edited["decisions"][0]["value"]["text"] == "quiero votar"
    with pytest.raises(ValueError, match="reference a candidate"):
        validate_agent_response(request, {"decisions": [{
            "category": "text", "action": "choose_candidate", "candidate_id": "invented",
        }]})
    with pytest.raises(ValueError, match="not minimal"):
        validate_agent_response(request, {"decisions": [{
            "category": "text", "action": "edit_candidate", "candidate_id": "candidate-1",
            "value": {"text": "una frase completamente inventada"},
        }]})


def test_external_audio_replay_requires_explicit_egress_flag(tmp_path, monkeypatch):
    monkeypatch.delenv("ALLOW_EXTERNAL_CLIENT_AUDIO_AGENT_REPLAY", raising=False)
    with pytest.raises(RuntimeError, match="client-audio egress blocked"):
        run_agent(tmp_path / "requests.jsonl", tmp_path / "responses.jsonl", "gemini-test", None)


def _score_fixture(tmp_path: Path, wrong_first=False, count=50):
    tmp_path.mkdir(parents=True, exist_ok=True)
    requests, gold, responses, adjudications = [], [], [], []
    for index in range(count):
        song_id = f"song-{index % 10}"
        zone_id = f"{song_id}:{index}"
        approved = "correcto"
        answer = "incorrecto" if wrong_first and index == 0 else approved
        requests.append({
            "zone_id": zone_id, "song_id": song_id, "is_live": False,
            "proposals": [{"candidate_id": f"c-{index}", "category": "text", "value": {"text": answer}}],
        })
        gold.append({
            "zone_id": zone_id, "song_id": song_id, "categories": ["text"],
            "approved": {"text": approved, "start_s": 1.0, "end_s": 2.0},
            "difficulty": "candidate_available",
        })
        responses.append({
            "zone_id": zone_id,
            "decisions": [{"category": "text", "action": "choose_candidate", "candidate_id": f"c-{index}", "value": {"text": answer}}],
        })
        if wrong_first and index == 0:
            for judge in ("qwen-judge", "mistral-judge", "gemma-judge"):
                adjudications.append({"zone_id": zone_id, "category": "text", "judge_family": judge, "verdict": "agent_wrong"})
    paths = [tmp_path / name for name in ("requests.jsonl", "gold.jsonl", "responses.jsonl", "adjudications.jsonl")]
    for path, rows in zip(paths, (requests, gold, responses, adjudications)):
        _write_jsonl(path, rows)
    output = tmp_path / "report.json"
    return score(paths[0], paths[1], paths[2], paths[3], output)


def test_category_gate_requires_50_resolutions_across_10_songs(tmp_path):
    small = _score_fixture(tmp_path / "small", count=49)
    assert small["categories"]["text"]["gate"] == "BLOCKED_INSUFFICIENT_EVIDENCE"
    report = _score_fixture(tmp_path, count=50)
    result = report["categories"]["text"]
    assert result["songs_resolved"] == 10
    assert result["resolved"] == 50
    assert result["gate"] == "GO_TIER_AGENT"
    assert result["production"] == {"tier_agent_enabled": True, "live_enabled": False}


def test_false_resolved_case_blocks_category(tmp_path):
    report = _score_fixture(tmp_path, wrong_first=True)
    result = report["categories"]["text"]
    assert result["functional_agreement"] == pytest.approx(0.98)
    assert result["false_resolved_rate_judged"] == 1.0
    assert result["gate"] == "NO_GO"
