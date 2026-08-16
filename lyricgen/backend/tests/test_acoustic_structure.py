import numpy as np

import acoustic_structure as acoustic
import structural_hybrid


def _view(seconds=40):
    boundary_frames = int(seconds * acoustic.SAMPLE_RATE / acoustic.HOP_LENGTH) + 20
    embedding_frames = int(seconds * acoustic.SAMPLE_RATE / acoustic.EMBEDDING_HOP) + 20
    return {
        "rms": np.full(boundary_frames, .45, dtype=np.float32),
        "onset": np.zeros(boundary_frames, dtype=np.float32),
        "flatness": np.full(boundary_frames, .2, dtype=np.float32),
        "harmonicity": np.full(boundary_frames, .7, dtype=np.float32),
        "pitch": np.full(boundary_frames, 2.0, dtype=np.float32),
        "voicing": np.full(boundary_frames, .75, dtype=np.float32),
        "embedding": np.zeros((72, embedding_frames), dtype=np.float32),
    }


def test_irregular_pericos_events_survive_before_text_mapping():
    primitives = [
        (60.80, 62.67), (63.17, 65.00),
        (66.97, 68.61), (69.44, 71.17),
        (73.12, 74.82), (75.55, 77.18),
        (79.25, 81.22), (81.53, 83.47),
    ]
    view = _view()
    partitions = acoustic._partition_candidates(
        primitives, view, view, 54.0, n_best=32,
    )
    # Acoustic N-best keeps multiple cardinalities.  No text count was passed
    # into the detector and no nearly-periodic grid can erase 63.17/75.55.
    counts = {part["event_count"] for part in partitions}
    assert 6 in counts
    assert 8 in counts

    structure = {"n_best": partitions}
    rotor = [{
        "start": start, "end": end, "text": text, "kind": kind,
    } for start, end, text, kind in [
        (60.85, 63.77, "Real uoh uoh", "sung"),
        (63.77, 67.04, "Real uoh uoh", "sung"),
        (67.05, 73.17, "Real uoh uoh", "sung"),
        (73.18, 75.65, "Real uoh uoh", "sung"),
        (75.65, 75.75, "no", "vocalization"),
        (79.31, 83.27, "noooo", "vocalization"),
    ]]
    mapped = acoustic.map_content(
        structure,
        [{"source": "human_gold", "family": "human", "events": rotor}],
    )
    assert len(mapped["events"]) == 6
    assert mapped["accepted"] is False
    assert mapped["reason"] == "phonetic_evidence_unavailable"
    assert mapped["events"][-1]["text"] == "noooo"
    assert mapped["events"][-1]["start"] >= 79.0
    assert not any(
        event.get("text", "").startswith("Real") and event["start"] >= 79.0
        for event in mapped["events"]
    )


def test_production_verifier_is_acoustic_first_and_suggestion_only(monkeypatch):
    monkeypatch.setenv("TARGETED_ACOUSTIC_CTC_ENABLED", "1")
    six = [{
        "id": f"ae{index}", "start": start, "end": end,
        "text": text, "confidence": .8, "content_source": "gemini_audio",
    } for index, (start, end, text) in enumerate([
        (60.85, 63.77, "Real uoh uoh"),
        (63.77, 67.04, "Real uoh uoh"),
        (67.05, 73.17, "Real uoh uoh"),
        (73.18, 75.65, "Real uoh uoh"),
        (75.65, 75.75, "no"),
        (79.31, 83.27, "noooo"),
    ])]
    seen = {}

    def analyze(_stem, _mix, **kwargs):
        seen.update(kwargs)
        return {
            "accepted": True, "reason": "analyzed",
            "n_best": [{"rank": 1, "score": 0, "event_count": 6, "events": six}],
        }

    monkeypatch.setattr(acoustic, "analyze_window", analyze)
    monkeypatch.setattr(acoustic, "map_content", lambda *_args, **_kwargs: {
        "accepted": False, "reason": "phonetic_evidence_unavailable", "margin": .4,
        "topology_mapping_supported": True,
        "events": six, "strong_unassigned_events": 0,
        "evidence_lineage": [{"source": "gemini_audio", "family": "gemini_audio"}],
    })
    verdict = structural_hybrid.verify(
        "stem.wav", "mix.wav",
        [{"start": 60.8, "end": 63.7, "text": "Real uoh uoh"}],
        window_start=54, window_end=87,
    )
    assert seen == {"window_start": 54, "window_end": 87}
    assert verdict["accepted"] is False
    assert verdict["automatic_apply_allowed"] is False
    assert len(verdict["suggested_events"]) == 6
    assert verdict["suggested_events"][-1]["text"] == "noooo"


def test_identical_timing_with_garbage_text_cannot_be_certified():
    events = [{
        "id": f"ae{i}", "start": float(i * 2), "end": float(i * 2 + 1.5),
        "confidence": .8,
        "type_posterior": {"lexical_phrase": .8},
    } for i in range(3)]
    structure = {
        "n_best": [{"rank": 1, "score": 0, "events": events}],
        "boundaries": [],
    }
    garbage = [{"start": event["start"], "end": event["end"],
                "text": "banana reloj"} for event in events]
    mapped = acoustic.map_content(
        structure,
        [{"source": "asr", "family": "openai_whisper", "events": garbage}],
    )
    assert mapped["events"]
    assert mapped["accepted"] is False
    assert mapped["phonetic_verified"] is False


def test_content_mapping_exposes_real_deduplicated_ctc_candidates():
    events = [{
        "id": f"ae{i}", "start": float(i * 2), "end": float(i * 2 + 1.5),
        "confidence": .8, "type_posterior": {"lexical_phrase": .8},
    } for i in range(2)]
    structure = {
        "n_best": [{"rank": 1, "score": 0, "events": events}],
        "boundaries": [],
    }
    mapped = acoustic.map_content(structure, [
        {"source": "whisper-a", "family": "whisper", "events": [
            {"start": 0, "end": 1.5, "text": "real uoh"},
            {"start": 2, "end": 3.5, "text": "noooo"},
        ]},
        {"source": "whisper-duplicate", "family": "whisper", "events": [
            {"start": 0, "end": 1.5, "text": "real uoh"},
            {"start": 2, "end": 3.5, "text": "noooo"},
        ]},
        {"source": "gemini", "family": "gemini", "events": [
            {"start": 0, "end": 1.5, "text": "no"},
            {"start": 2, "end": 3.5, "text": "real"},
        ]},
    ])
    assert len(mapped["phonetic_candidates"]) == 2
    assert mapped["selected_candidate_id"]
    assert len({item["candidate_id"] for item in mapped["phonetic_candidates"]}) == 2


def test_energy_without_vocal_cues_does_not_become_a_lyric_event():
    stem = _view(seconds=3)
    mix = _view(seconds=3)
    for view in (stem, mix):
        view["rms"][:] = 1.0
        view["voicing"][:] = 0.0
        view["harmonicity"][:] = 0.0
        view["flatness"][:] = 1.0
    assert acoustic._primitive_regions(stem, mix, 0.0) == []


def test_multiscale_self_similarity_is_constructed_and_auditable():
    stem = _view(seconds=8)
    mix = _view(seconds=8)
    pattern = np.sin(np.linspace(0, 8 * np.pi, stem["embedding"].shape[1]))
    stem["embedding"][0] = pattern
    mix["embedding"][0] = pattern
    summary = acoustic._self_similarity_summary(stem, mix)
    assert summary["available"] is True
    assert set(summary["scales"]) == {"phonetic", "syllabic", "event", "phrase"}
    assert len(summary["scales"]["phonetic"]["matrix_sha256"]) == 64
    assert summary["scales"]["phonetic"]["shape"][0] > 3


def test_coarse_vad_start_refines_to_local_acoustic_attack_without_text():
    partitions = [{"events": [{"start": 63.2, "end": 65.26}]}]
    acoustic._refine_partition_starts(partitions, [
        {"time": 63.2, "start_probability": .31},
        {"time": 63.54, "start_probability": .57},
        {"time": 63.79, "start_probability": .35},
        {"time": 64.5, "start_probability": .99},  # outside local bound
    ])
    event = partitions[0]["events"][0]
    assert event["coarse_start"] == 63.2
    assert event["start"] == 63.54
