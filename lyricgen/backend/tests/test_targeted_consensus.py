import logging
import inspect

import targeted_consensus as tc
import quality_mutation
import transcription_quality
import vocal_sep
from quality_v6_contracts import PROPOSAL_CANDIDATE_SCHEMA, ReviewProposalCandidate


def test_targeted_window_cap_allows_difficult_live_songs(monkeypatch):
    monkeypatch.setenv("TARGETED_CONSENSUS_MAX_WINDOWS", "200")
    assert tc._max_targeted_windows() == 64


def words(text, start=10.0, step=0.7):
    return [
        {"word": token, "start": start + i * step, "end": start + i * step + 0.5}
        for i, token in enumerate(text.split())
    ]


def event_words(lines, starts):
    out = []
    for line, start in zip(lines, starts):
        out.extend(words(line, start=start, step=0.35))
    return out


def gemini_events(lines, starts):
    return [
        {"start": start, "end": start + 1.2, "text": line, "kind": "sung"}
        for line, start in zip(lines, starts)
    ]


def test_consensus_requires_stem_and_a_distinct_recognition_family():
    agreed, evidence = tc.choose_consensus(
        words("hoy temprano pienso"), words("hoy temprano pienso"), []
    )
    assert agreed is None
    assert evidence["family_rejections"] == 1
    agreed, evidence = tc.choose_consensus(
        words("hoy temprano pienso"), words("hoy temprano pienso"), [],
        stream_families={"stem": "asr_a", "mix": "asr_b"},
    )
    assert agreed
    assert evidence["sources"] == ["stem", "mix"]
    assert evidence["source_families"] == ["asr_a", "asr_b"]
    assert tc.choose_consensus(words("hoy temprano pienso"), words("otra cosa distinta"), [])[0] is None
    assert tc.choose_consensus(
        words("hoy temprano estuve pensando en vos"),
        words("muy temprano estuve pensando en vos"), [],
    )[0] is None


def test_targeted_whisper_defaults_to_original_family_and_cannot_self_confirm():
    result = {"_primary_asr_family": "attested-original-family"}
    families = tc._stream_family_map(result)

    assert families["stem"] == families["mix"] == families["primary"]
    agreed, evidence = tc.choose_consensus(
        words("hoy temprano pienso"), words("hoy temprano pienso"),
        words("hoy temprano pienso"), stream_families=families,
    )
    assert agreed is None
    assert len({value for value in evidence["families"].values() if value}) == 1


def test_explicit_attested_targeted_family_can_confirm_distinct_original():
    families = tc._stream_family_map({
        "_primary_asr_family": "original-independent-model",
        "_targeted_asr_family": "openai-whisper-1-family",
    })
    agreed, evidence = tc.choose_consensus(
        words("hoy temprano pienso"), words("hoy temprano pienso"),
        words("hoy temprano pienso"), stream_families=families,
    )
    assert agreed
    assert len(set(evidence["source_families"])) == 2


def test_slowed_stem_can_win_only_with_independent_corroboration():
    normal = words("muy temprano pienso")
    slowed = words("hoy temprano pienso")
    mix = words("hoy temprano pienso")
    agreed, evidence = tc.choose_consensus(
        normal, mix, [], slowed_words=slowed,
    )
    assert agreed is None
    assert tc.choose_consensus(
        normal, [], [], slowed_words=slowed,
    )[0] is None
    agreed, evidence = tc.choose_consensus(
        [], [], [], slowed_words=slowed,
        witness_words=words("hoy temprano pienso"),
        stream_families={
            "slowed_stem": "targeted_whisper",
            "witness": "independent_asr",
        },
    )
    assert agreed == slowed
    assert evidence["sources"] == ["slowed_stem", "witness"]


def test_slowed_timestamp_mapping_returns_to_original_clock(tmp_path, monkeypatch):
    stem = tmp_path / "stem.wav"
    stem.write_bytes(b"audio")

    monkeypatch.setattr(tc.subprocess, "run", lambda *_a, **_k: None)
    heard = words("hoy temprano pienso", start=1.0, step=1.0)
    mapped = tc._transcribe_slowed_window(
        str(stem), 40.0, 10.0, 0.88, "es", "job",
        lambda *_args: heard,
    )
    assert mapped[0]["start"] == 40.88
    assert mapped[0]["end"] == 41.32


def test_reprocess_suggests_but_never_destroys_a_mismatched_line():
    result = {
        "segments": [{"start": 10, "end": 13, "text": "texto totalmente malo"}],
        "_asr_words": words("hoy temprano pienso"),
        "_primary_asr_family": "independent_primary_asr",
        "_targeted_asr_family": "targeted_openai_asr",
    }

    def transcribe(path, *_args):
        return words("hoy temprano pienso") if path in {"stem.wav", "mix.wav"} else []

    output, stats = tc.reprocess(
        result, "mix.wav",
        [{"start": 9, "end": 14, "segment_indices": [0]}],
        transcribe_fn=transcribe, stem_path="stem.wav",
    )
    assert output["segments"][0]["text"] == "texto totalmente malo"
    assert output["segments"][0]["consensus_suggestion"] == "hoy temprano pienso"
    assert output["segments"][0]["review"] is True
    assert stats["lines_replaced"] == 0
    assert stats["lines_suggested"] == 1
    assert stats["asr_calls"] == 2
    assert stats["provider_attempts"] == 2
    proposal = stats["quality_proposal_windows"][0]
    assert proposal["kind"] == "review_proposal_candidate"
    assert proposal["schema"] == PROPOSAL_CANDIDATE_SCHEMA
    assert "certification" not in proposal
    assert ReviewProposalCandidate.from_mapping(proposal).parent_window_id
    # An opaque test path has no attestable media duration. The safe fallback
    # preserves left context but clamps the tile at the requested parent end;
    # stem+mix therefore submit two bounded eight-second clips.
    assert stats["submitted_audio_seconds"] == 16.0


def test_reprocess_respects_cost_budget(monkeypatch):
    monkeypatch.setenv("TARGETED_CONSENSUS_MAX_BILLED_SECONDS", "5")
    result = {"segments": [], "_asr_words": []}
    output, stats = tc.reprocess(
        result, "mix.wav", [{"start": 0, "end": 10}],
        transcribe_fn=lambda *_: [], stem_path="stem.wav",
    )
    assert output["segments"] == result["segments"]
    assert stats["asr_calls"] == 0
    assert "cost_budget" in stats["declined"]


def test_reprocess_tiles_long_windows_without_truncating(monkeypatch):
    monkeypatch.setenv("TARGETED_CONSENSUS_MAX_CLIP_SECONDS", "10")
    _output, stats = tc.reprocess(
        {"segments": [], "_asr_words": []}, "mix.wav",
        [{"start": 0, "end": 20}],
        transcribe_fn=lambda *_: [], stem_path="stem.wav",
    )
    assert stats["windows_truncated"] == 0
    assert "truncated_windows" not in stats
    assert stats["windows_tiled"] > 0
    assert "window_truncated" not in stats["declined"]


def test_reprocess_counts_truncated_tiles_in_canonical_stats():
    tile = {
        "id": "tail:tile:1:1", "parent_window_id": "tail",
        "start": 37.0, "end": 60.0, "core_start": 40.0,
        "core_end": 60.0, "analysis_truncated": True,
        "coverage_complete": False,
    }
    _output, stats = tc.reprocess(
        {"segments": [], "_asr_words": []}, "mix.wav", [tile],
        transcribe_fn=lambda *_: [], stem_path="stem.wav",
    )

    assert stats["windows_truncated"] == 1
    assert stats["parent_coverage"]["tail"]["complete"] is False
    assert stats["parent_coverage"]["tail"]["tiles_truncated"] == 1


def test_proposal_candidate_propagates_only_existing_calibrated_certification():
    from quality_v6_calibration import PREDICTION_CERTIFICATION_SCHEMA

    certification = {
        "kind": "review_proposal_certification",
        "schema": PREDICTION_CERTIFICATION_SCHEMA,
        "policy_version": "lyrics-quality-v6",
        "eligible_offline": True,
        "review_proposal_allowed": True,
        "automatic_apply_allowed": False,
        "runtime_authorization": False,
        "blockers": [],
    }
    common = {
        "candidate_id": "tile", "parent_window_id": "parent",
        "start": 1.0, "end": 2.0, "reasons": ["event_count"],
        "current_segments": [],
        "proposed_segments": [{"start": 1.0, "end": 2.0, "text": "candidate"}],
        "source": "test",
    }
    certified = tc._proposal_candidate_payload(
        **common, calibrated_evidence={
            "review_proposal_certification": certification,
        },
    )
    uncalibrated = tc._proposal_candidate_payload(
        **common, calibrated_evidence={
            "review_proposal_certification": {
                **certification, "eligible_offline": False,
            },
        },
    )

    assert certified["certification"] == certification
    assert "certification" not in uncalibrated
    assert ReviewProposalCandidate.from_mapping(certified).certification == certification


def test_structural_producer_propagates_existing_calibrated_certification(
        monkeypatch):
    from quality_v6_calibration import PREDICTION_CERTIFICATION_SCHEMA

    certification = {
        "kind": "review_proposal_certification",
        "schema": PREDICTION_CERTIFICATION_SCHEMA,
        "policy_version": "lyrics-quality-v6",
        "eligible_offline": True,
        "review_proposal_allowed": True,
        "automatic_apply_allowed": False,
        "runtime_authorization": False,
        "blockers": [],
    }
    starts = [60.0, 64.0]
    recovered = event_words(["Real uoh uoh"] * 2, starts)
    proposed = [
        {"id": f"event-{index}", "start": start, "end": start + 1.4,
         "text": "Real uoh uoh", "confidence": 0.9}
        for index, start in enumerate(starts)
    ]
    monkeypatch.setenv("TARGETED_SLOW_STEM_ENABLED", "1")
    monkeypatch.setenv("TARGETED_GEMINI_VERIFY_ENABLED", "1")
    monkeypatch.setenv("TARGETED_ACOUSTIC_STRUCTURE_ENABLED", "1")
    monkeypatch.setenv("TARGETED_CONSENSUS_MAX_BILLED_SECONDS", "120")
    monkeypatch.setattr(
        tc, "_transcribe_slowed_window", lambda *_args, **_kwargs: recovered,
    )

    def hybrid(*_args, **_kwargs):
        return {
            "accepted": True,
            "events": proposed,
            "suggested_events": proposed,
            "automatic_apply_allowed": False,
            "review_proposal_certification": certification,
            "acoustic_structure": {"accepted": True},
            "content_mapping": {"accepted": True, "events": proposed},
        }

    _output, stats = tc.reprocess(
        {
            "segments": [
                {"start": start, "end": start + 1.4, "text": "Real",
                 "live_structural_suggestion": "Real uoh uoh"}
                for start in starts
            ],
            "_asr_words": recovered,
        },
        "mix.wav",
        [{"id": "tail", "start": 59.0, "end": 68.0,
          "reasons": ["live_structural_disagreement"]}],
        transcribe_fn=lambda *_args, **_kwargs: recovered,
        gemini_fn=lambda *_args, **_kwargs: gemini_events(
            ["Real uoh uoh"] * 2, starts,
        ),
        stem_path="stem.wav", hybrid_fn=hybrid,
    )

    proposal = stats["quality_proposal_windows"][0]
    assert proposal["kind"] == "review_proposal_candidate"
    assert proposal["schema"] == PROPOSAL_CANDIDATE_SCHEMA
    assert proposal["certification"] == certification
    assert ReviewProposalCandidate.from_mapping(proposal).certification == certification


def test_gap_candidate_is_dark_in_observe_and_reviewable_in_enforce(monkeypatch):
    window = [{"start": 9, "end": 14, "reasons": ["voiced_gap"]}]
    def transcribe(*_args):
        return words("hoy temprano pienso")

    monkeypatch.setenv("TRANSCRIPTION_QUALITY_MODE", "observe")
    observed, stats = tc.reprocess(
        {
            "segments": [], "_asr_words": words("hoy temprano pienso"),
            "_primary_asr_family": "independent_primary_asr",
            "_targeted_asr_family": "targeted_openai_asr",
        }, "mix.wav", window,
        transcribe_fn=transcribe, stem_path="stem.wav",
    )
    assert observed["segments"] == []
    assert stats["lines_suggested"] >= 1

    monkeypatch.setenv("TRANSCRIPTION_QUALITY_MODE", "enforce")
    enforced, stats = tc.reprocess(
        {
            "segments": [], "_asr_words": words("hoy temprano pienso"),
            "_primary_asr_family": "independent_primary_asr",
            "_targeted_asr_family": "targeted_openai_asr",
        }, "mix.wav", window,
        transcribe_fn=transcribe, stem_path="stem.wav",
    )
    assert stats["lines_inserted"] == 0
    assert stats["lines_suggested"] >= 1
    assert enforced["segments"] == []


def test_gap_gemini_consensus_becomes_one_click_vocalization(monkeypatch):
    monkeypatch.setenv("QUALITY_OPERATOR_SUGGESTIONS_ENABLED", "1")
    monkeypatch.setenv("TARGETED_GEMINI_VERIFY_ENABLED", "1")
    recovered = words("oh oh oh", start=10.0, step=0.3)

    output, stats = tc.reprocess(
        {
            "segments": [], "_asr_words": [],
            "_targeted_asr_family": "targeted_openai_asr",
        },
        "mix.wav",
        [{"id": "gap-oh", "start": 9, "end": 14,
          "reasons": ["voiced_gap"]}],
        transcribe_fn=lambda *_a, **_k: recovered,
        gemini_fn=lambda *_a, **_k: [{
            "start": 10.0, "end": 11.2, "text": "oh oh oh",
            "kind": "vocalization",
        }],
        stem_path="stem.wav",
    )

    assert output["segments"] == []
    proposal = stats["quality_proposal_windows"][0]
    assert proposal["current_segments"] == []
    assert proposal["proposed_segments"][0]["text"] == "(oh oh oh)"
    assert "vocalization" in proposal["reasons"]
    assert proposal["source_families"] == [
        "gemini_audio", "targeted_openai_asr",
    ]


def test_slowed_and_same_model_witness_cannot_suggest_insertion(
        monkeypatch):
    recovered = words("real wow wow", start=60.0)
    monkeypatch.setenv("TRANSCRIPTION_QUALITY_MODE", "enforce")
    monkeypatch.setenv("TARGETED_SLOW_STEM_ENABLED", "1")
    monkeypatch.setattr(
        tc, "_transcribe_slowed_window", lambda *_a, **_k: recovered,
    )
    result = {
        "segments": [], "_asr_words": [],
        "_independent_asr_words": recovered,
        "_independent_asr_family": "targeted_openai_asr",
    }
    out, stats = tc.reprocess(
        result, "mix.wav",
        [{"start": 59, "end": 64,
          "reasons": ["independent_uncovered_asr"]}],
        transcribe_fn=lambda *_a, **_k: [], stem_path="stem.wav",
    )
    assert stats["lines_inserted"] == 0
    assert stats["lines_suggested"] == 0
    assert out["segments"] == []


def test_cross_model_primary_can_confirm_bounded_insertion(monkeypatch):
    recovered = words("real wow wow", start=60.0)
    monkeypatch.setenv("TRANSCRIPTION_QUALITY_MODE", "enforce")
    monkeypatch.setattr(
        transcription_quality, "calibration_identity",
        lambda: {
            "calibrated": True,
            "policy_version": "lyrics-quality-v5",
        },
    )
    result = {
        "segments": [], "_asr_words": recovered,
        "live_audio_truth": True,
        "_primary_asr_family": "independent_live_asr",
        "_targeted_asr_family": "targeted_openai_asr",
    }
    out, stats = tc.reprocess(
        result, "mix.wav",
        [{"start": 59, "end": 64,
          "reasons": ["uncovered_asr"]}],
        transcribe_fn=lambda *_a, **_k: recovered, stem_path="stem.wav",
    )
    assert stats["lines_inserted"] == 1
    assert out["segments"][0]["text"] == "real wow wow"
    assert "primary" in out["segments"][0]["consensus_sources"]


def test_global_enforce_does_not_mutate_job_outside_rollout(monkeypatch):
    recovered = words("real wow wow", start=60.0)
    monkeypatch.setenv("TRANSCRIPTION_QUALITY_MODE", "enforce")
    monkeypatch.setenv("TRANSCRIPTION_QUALITY_ENFORCE_PERCENT", "0")
    monkeypatch.setattr(
        transcription_quality, "calibration_identity",
        lambda: {"calibrated": True},
    )
    monkeypatch.setattr(quality_mutation, "_tenant_for_job", lambda _job: "")
    result = {
        "segments": [], "_asr_words": recovered,
        "live_audio_truth": True,
        "_primary_asr_family": "independent_live_asr",
        "_targeted_asr_family": "targeted_openai_asr",
    }
    out, stats = tc.reprocess(
        result, "mix.wav",
        [{"start": 59, "end": 64, "reasons": ["uncovered_asr"]}],
        job_id="outside-cohort", transcribe_fn=lambda *_a, **_k: recovered,
        stem_path="stem.wav",
    )
    assert out["segments"] == []
    assert stats["lines_inserted"] == 0
    assert stats["lines_suggested"] == 1


def test_full_song_witness_reserves_budget_before_targeted_windows(monkeypatch):
    monkeypatch.setenv("LIVE_ASR_MAX_BILLED_SECONDS", "600")
    result = {
        "segments": [], "_asr_words": [],
        "postpass_stats": {
            "word_vote": {"audio_seconds_billed": 590.0},
        },
    }
    out, stats = tc.reprocess(
        result, "mix.wav", [{"start": 0, "end": 10}],
        transcribe_fn=lambda *_a, **_k: (_ for _ in ()).throw(
            AssertionError("reserved budget must prevent another ASR call")
        ),
        stem_path="stem.wav",
    )
    assert out["segments"] == []
    assert stats["prior_audio_seconds_billed"] == 590.0
    assert "cost_budget" in stats["declined"]


def test_owned_cached_stem_is_removed_after_retry(tmp_path, monkeypatch):
    stem = tmp_path / "cached-stem.wav"
    stem.write_bytes(b"stem")
    monkeypatch.setattr(
        vocal_sep, "separate_vocals", lambda *_a, **_k: str(stem),
    )
    tc.reprocess(
        {"segments": [], "_asr_words": []}, "mix.wav",
        [{"start": 0, "end": 5}], transcribe_fn=lambda *_a, **_k: [],
    )
    assert not stem.exists()


def test_residual_asr_runs_only_for_acoustic_crowd_evidence(monkeypatch):
    monkeypatch.setenv("TARGETED_RESIDUAL_ASR_ENABLED", "1")
    recovered = words("coro uoh uoh", start=10.0)
    calls = []

    def residual(*_args, **_kwargs):
        calls.append("residual")
        return recovered

    monkeypatch.setattr(tc, "_transcribe_residual_window", residual)
    _output, stats = tc.reprocess(
        {"segments": [], "_asr_words": []}, "mix.wav",
        [{
            "start": 9, "end": 14, "reasons": ["voiced_gap"],
            "acoustic_crowd_evidence": True,
        }],
        transcribe_fn=lambda *_a, **_k: recovered,
        stem_path="stem.wav",
    )
    assert calls == ["residual"]
    assert stats["residual_asr_calls"] == 1
    # With an opaque path the right context is clamped to the parent end.
    assert stats["residual_audio_seconds"] == 8.0

    calls.clear()
    tc.reprocess(
        {"segments": [], "_asr_words": []}, "mix.wav",
        [{"start": 9, "end": 14, "reasons": ["voiced_gap"]}],
        transcribe_fn=lambda *_a, **_k: recovered,
        stem_path="stem.wav",
    )
    assert calls == []


def test_provider_exception_message_never_leaks_into_logs(
        monkeypatch, caplog):
    secret_lyric = "SECRETO letra privada completa"
    monkeypatch.setenv("TARGETED_RESIDUAL_ASR_ENABLED", "1")

    def failing_provider(*_args, **_kwargs):
        raise RuntimeError(secret_lyric)

    monkeypatch.setattr(tc, "_transcribe_residual_window", failing_provider)
    with caplog.at_level(logging.WARNING, logger="genly.targeted_consensus"):
        _output, stats = tc.reprocess(
            {"segments": [], "_asr_words": []}, "mix.wav",
            [{
                "start": 9, "end": 14, "reasons": ["voiced_gap"],
                "acoustic_crowd_evidence": True,
            }],
            transcribe_fn=lambda *_a, **_k: words(
                "candidato vocal seguro", start=10.0,
            ),
            stem_path="stem.wav",
        )

    assert secret_lyric not in caplog.text
    assert "error_type=RuntimeError" in caplog.text
    assert "residual_exception:RuntimeError" in stats["declined"]


def test_cross_model_cardinality_repairs_repeated_motif_monotonically():
    starts = [60.8, 64.0, 67.2, 73.2]
    lines = ["Real wow wow"] * 4
    segments = [
        {"start": start, "end": start + 1.4,
         "text": "Real", "live_structural_suggestion": "Real wow wow"}
        for start in starts
    ] + [{
        "start": 88.0, "end": 89.0, "text": "Real",
        "live_structural_suggestion": "Real wow wow",
    }]
    slow = event_words(lines, starts)
    support = event_words(["Real oh oh"] * 4, starts)
    repaired, stats = tc._repair_structural_repetition(
        segments, slow, gemini_events(lines, starts), support,
        window_start=59.0, window_end=85.0, enforce=True,
    )
    assert stats["applied"] is True
    assert stats["events"] == 4
    assert len(repaired) == 5
    assert [segment["start"] for segment in repaired[:4]] == starts
    assert all(segment["text"] == "Real wow wow" for segment in repaired[:4])
    assert repaired[4]["text"] == "Real"  # unmatched event is never deleted
    assert all(segment["consensus_sources"] == [
        "slowed_stem_whisper_1", "gemini_audio",
    ] for segment in repaired[:4])


def test_structural_repair_declines_when_models_disagree_on_repeat_count():
    starts = [60.8, 64.0, 67.2, 73.2]
    lines = ["Real wow wow"] * 4
    segments = [{
        "start": 62.0, "end": 67.0, "text": "Real real",
        "live_structural_suggestion": "Real wow wow",
    }]
    original = [dict(segment) for segment in segments]
    repaired, stats = tc._repair_structural_repetition(
        segments, event_words(lines, starts),
        gemini_events(lines[:3], starts[:3]), event_words(lines, starts),
        window_start=59.0, window_end=85.0, enforce=True,
    )
    assert repaired == original
    assert stats["applied"] is False
    assert stats["reason"] == "cardinality_disagreement"


def test_gemini_event_schema_is_bounded_to_vocal_events():
    schema = tc._GEMINI_EVENT_SCHEMA
    events = schema["properties"]["events"]
    item = events["items"]
    assert events["maxItems"] == 16
    assert item["additionalProperties"] is False
    assert item["properties"]["kind"]["enum"] == [
        "sung", "vocalization", "speech",
    ]


def test_gemini_provenance_freezes_raw_events_before_candidate_filters():
    source = inspect.getsource(tc._transcribe_gemini_events)
    raw_record = source.index("events=events")
    candidate_filter = source.index("for event in events")
    assert raw_record < candidate_filter
    assert "events=out" not in source


def test_structural_repair_is_suggestion_only_in_observe_mode():
    starts = [60.8, 64.0]
    lines = ["Real wow wow"] * 2
    segments = [{
        "start": 62.0, "end": 67.0, "text": "Real real",
        "live_structural_suggestion": "Real wow wow",
    }]
    repaired, stats = tc._repair_structural_repetition(
        segments, event_words(lines, starts), gemini_events(lines, starts),
        event_words(lines, starts), window_start=59.0, window_end=70.0,
        enforce=False,
    )
    assert repaired == segments
    assert stats["suggested"] is True
    assert stats["applied"] is False


def test_spoken_or_metadata_events_remain_diagnostics_not_lyrics():
    segments = [{
        "start": 60.0, "end": 62.0, "text": "Real",
        "live_structural_suggestion": "Real uoh uoh",
    }]
    repaired, stats = tc._repair_structural_repetition(
        segments,
        event_words(["Real uoh uoh"], [60.0]),
        [
            {"start": 60, "end": 61, "text": "Gracias", "kind": "speech"},
            {"start": 61, "end": 62, "text": "Lyrics: Example", "kind": "sung"},
        ],
        event_words(["Real uoh uoh"], [60.0]),
        window_start=59.0, window_end=63.0, enforce=False,
        hybrid_enabled=True, stem_path="stem.wav", mix_path="mix.wav",
    )
    assert repaired == segments
    assert stats["suggested"] is False
    assert stats["excluded_content_diagnostics"] == {
        "METADATA": 1, "SPEECH": 1,
    }


def test_reprocess_keeps_gemini_structural_block_as_suggestion(monkeypatch):
    starts = [60.8, 64.0, 67.2, 73.2]
    lines = ["Real wow wow"] * 4
    slow = event_words(lines, starts)
    support = event_words(["Real oh oh"] * 4, starts)
    monkeypatch.setenv("TRANSCRIPTION_QUALITY_MODE", "enforce")
    monkeypatch.setenv("TARGETED_SLOW_STEM_ENABLED", "1")
    monkeypatch.setenv("TARGETED_GEMINI_VERIFY_ENABLED", "1")
    monkeypatch.setenv("TARGETED_STRUCTURAL_AUTOREPAIR_ENABLED", "1")
    monkeypatch.setenv("TARGETED_CONSENSUS_MAX_BILLED_SECONDS", "180")
    monkeypatch.setattr(
        tc, "_transcribe_slowed_window", lambda *_a, **_k: slow,
    )
    result = {
        "segments": [
            {"start": start, "end": start + 1.4,
             "text": "Real", "live_structural_suggestion": "Real wow wow"}
            for start in starts
        ],
        "_asr_words": support,
    }
    out, stats = tc.reprocess(
        result, "mix.wav",
        [{"start": 59, "end": 85,
          "reasons": ["live_structural_disagreement"]}],
        transcribe_fn=lambda *_a, **_k: support,
        gemini_fn=lambda *_a, **_k: gemini_events(lines, starts),
        stem_path="stem.wav",
    )
    assert stats["structural_repairs"] == 0
    assert stats["structural_events"] == 0
    assert stats["lines_suggested"] == 4
    assert len(out["segments"]) == 4
    assert [row["text"] for row in out["segments"]] == ["Real"] * 4
