"""Policy tests for the shared audio-first postprocess on live uploads."""

import asyncio
import inspect
import pytest

import main
import pipeline
import transcription_worker


@pytest.fixture(autouse=True)
def _signed_quality_gate(monkeypatch):
    monkeypatch.setattr(main, "_quality_mutation_authorized", lambda _job_id: True)


def _run(segments, *, audio_path="/tmp/live.wav", canonical="studio words",
         artist="Artist", song="Song", language="es"):
    return asyncio.run(
        main._postprocess_live_whisperx(
            segments, audio_path=audio_path, canonical=canonical,
            artist=artist, song=song, language=language,
        )
    )


def test_disabled_flags_are_identity_and_schedule_no_work(monkeypatch):
    monkeypatch.delenv("LLM_SEGMENT_ENABLED", raising=False)
    monkeypatch.delenv("GAP_RECOVERY_ENABLED", raising=False)
    monkeypatch.delenv("LIVE_LEXICAL_CONSENSUS_ENABLED", raising=False)

    async def unexpected_to_thread(*_args, **_kwargs):
        raise AssertionError("disabled postpasses must not schedule work")

    monkeypatch.setattr(main.asyncio, "to_thread", unexpected_to_thread)
    segments = [{"start": 1.0, "end": 2.0, "text": "Como se oyó"}]

    assert _run(segments) is segments


def test_enabled_legacy_mutators_are_identity_without_signed_quality_gate(monkeypatch):
    monkeypatch.setattr(main, "_quality_mutation_authorized", lambda _job_id: False)
    monkeypatch.setenv("LLM_SEGMENT_ENABLED", "1")
    monkeypatch.setenv("GAP_RECOVERY_ENABLED", "1")
    raw = [{"start": 1.0, "end": 2.0, "text": "raw"}]

    async def unexpected_to_thread(*_args, **_kwargs):
        raise AssertionError("uncalibrated mutators must not run")

    monkeypatch.setattr(main.asyncio, "to_thread", unexpected_to_thread)
    assert _run(raw) is raw


def test_enabled_postpasses_run_in_audio_first_order(monkeypatch):
    monkeypatch.setenv("LLM_SEGMENT_ENABLED", "1")
    monkeypatch.setenv("GAP_RECOVERY_ENABLED", "true")
    calls = []
    raw = [{"start": 1.0, "end": 2.0, "text": "raw"}]
    segmented = [{"start": 1.0, "end": 2.0, "text": "segmented"}]
    recovered = segmented + [
        {"start": 4.0, "end": 5.0, "text": "recovered"},
    ]

    def fake_segment(segments, *, audio_path, artist, song, language):
        calls.append(("segment", segments, audio_path, artist, song, language))
        return segmented

    def fake_recover(segments, *, audio_path, canonical, prompt_reference,
                     artist, song, language):
        calls.append((
            "recover", segments, audio_path, canonical, prompt_reference,
            artist, song, language,
        ))
        return recovered

    monkeypatch.setattr(pipeline, "_llm_segment_words", fake_segment)
    monkeypatch.setattr(pipeline, "_recover_gap_lyrics", fake_recover)

    assert _run(raw, canonical="reference guard") is recovered
    assert calls == [
        ("segment", raw, "/tmp/live.wav", "Artist", "Song", "es"),
        ("recover", segmented, "/tmp/live.wav", "reference guard", False,
         "Artist", "Song", "es"),
    ]


def test_each_postpass_keeps_its_independent_flag(monkeypatch):
    raw = [{"start": 1.0, "end": 2.0, "text": "raw"}]
    calls = []

    def fake_segment(segments, **_kwargs):
        calls.append("segment")
        return segments

    def fake_recover(segments, **_kwargs):
        calls.append("recover")
        return segments

    monkeypatch.setattr(pipeline, "_llm_segment_words", fake_segment)
    monkeypatch.setattr(pipeline, "_recover_gap_lyrics", fake_recover)

    monkeypatch.setenv("LLM_SEGMENT_ENABLED", "yes")
    monkeypatch.setenv("GAP_RECOVERY_ENABLED", "0")
    assert _run(raw) is raw
    assert calls == ["segment"]

    calls.clear()
    monkeypatch.setenv("LLM_SEGMENT_ENABLED", "off")
    monkeypatch.setenv("GAP_RECOVERY_ENABLED", "on")
    assert _run(raw) is raw
    assert calls == ["recover"]


def test_both_live_policies_postprocess_before_emit_and_reconcile():
    src = inspect.getsource(main._run_transcription_for_job)
    branch = src.index("if _live_no_hint or _live_audio_truth:")
    postprocess = src.index("await _postprocess_live_whisperx(", branch)
    emit = src.index("return _emit_segments(", postprocess)
    reconcile = src.index("_reconciled = _wxr.reconcile", emit)

    assert branch < postprocess < emit < reconcile
    live_exit = src[branch:reconcile]
    assert "_wxr.reconcile" not in live_exit
    assert "forced_align" not in live_exit


def test_worker_gap_rescue_is_the_only_owner_when_both_flags_are_on(
        monkeypatch):
    monkeypatch.setenv("LLM_SEGMENT_ENABLED", "0")
    monkeypatch.setenv("GAP_RECOVERY_ENABLED", "1")
    monkeypatch.setenv("GAP_RESCUE_ENABLED", "1")

    async def unexpected_to_thread(*_args, **_kwargs):
        raise AssertionError("Gemini gap recovery must not run before GAP_RESCUE")

    monkeypatch.setattr(main.asyncio, "to_thread", unexpected_to_thread)
    raw = [{"start": 1.0, "end": 2.0, "text": "raw"}]
    assert _run(raw) is raw


def test_unexpected_postpass_error_falls_back_to_last_valid_segments(monkeypatch):
    monkeypatch.setenv("LLM_SEGMENT_ENABLED", "1")
    monkeypatch.setenv("GAP_RECOVERY_ENABLED", "0")
    monkeypatch.delenv("GAP_RESCUE_ENABLED", raising=False)
    monkeypatch.delenv("LIVE_LEXICAL_CONSENSUS_ENABLED", raising=False)
    raw = [{"start": 1.0, "end": 2.0, "text": "raw"}]

    async def broken_to_thread(*_args, **_kwargs):
        raise RuntimeError("unexpected")

    monkeypatch.setattr(main.asyncio, "to_thread", broken_to_thread)
    assert _run(raw) is raw


def test_live_lexical_pass_runs_after_audio_owned_postpasses(monkeypatch):
    monkeypatch.setenv("LLM_SEGMENT_ENABLED", "0")
    monkeypatch.setenv("GAP_RECOVERY_ENABLED", "0")
    monkeypatch.setenv("LIVE_LEXICAL_CONSENSUS_ENABLED", "1")
    monkeypatch.setenv("LIVE_INDEPENDENT_VERIFY_ENABLED", "1")
    raw = [{"start": 1.0, "end": 2.0,
            "text": "Muy temprano estuve pensando en vos"}]
    out = _run(raw, canonical="Hoy temprano estuve pensando en vos")
    assert out[0]["text"] == "Muy temprano estuve pensando en vos"
    assert out[0]["live_lexical_suggestion"] == \
        "Hoy temprano estuve pensando en vos"
    assert out[0]["start"] == 1.0


def test_live_lexical_mutation_declines_without_independent_verifier(monkeypatch):
    monkeypatch.setenv("LLM_SEGMENT_ENABLED", "0")
    monkeypatch.setenv("GAP_RECOVERY_ENABLED", "0")
    monkeypatch.setenv("LIVE_LEXICAL_CONSENSUS_ENABLED", "1")
    monkeypatch.setenv("LIVE_INDEPENDENT_VERIFY_ENABLED", "0")
    raw = [{"start": 1.0, "end": 2.0,
            "text": "Muy temprano estuve pensando en vos"}]
    assert _run(raw, canonical="Hoy temprano estuve pensando en vos") is raw


def test_heuristic_phrase_segmenter_skips_llm_partition(monkeypatch):
    monkeypatch.setenv("PHRASE_SEGMENTER_ENABLED", "1")
    import phrase_segmenter

    monkeypatch.setattr(
        phrase_segmenter, "resegment",
        lambda *_a, **_k: (_ for _ in ()).throw(
            AssertionError("must not segment twice")
        ),
    )
    result = {"segments": [
        {"start": 1.0, "end": 2.0, "text": "line", "llm_segmented": True},
    ]}
    out = main._maybe_phrase_segment(result, "job")
    assert out["segments"] == result["segments"]
    assert out["postpass_stats"]["phrase_seg"]["skipped"] == \
        "already_llm_segmented"


def test_live_auto_language_never_comes_from_catalogue():
    assert not main._can_infer_primary_language_from_reference(
        "", live=True, title="Eso Es Real", filename="audio.mp3",
    )
    assert not main._can_infer_primary_language_from_reference(
        "", live=False, title="Eso Es Real (Live)", filename="audio.mp3",
    )
    assert not main._can_infer_primary_language_from_reference(
        "", live=False, title="Eso Es Real", filename="show_en_vivo.mp3",
    )


def test_studio_auto_language_keeps_catalogue_hint():
    assert main._can_infer_primary_language_from_reference(
        "", live=False, title="Studio Cut", filename="studio.mp3",
    )
    assert not main._can_infer_primary_language_from_reference(
        "es", live=False, title="Studio Cut", filename="studio.mp3",
    )


def test_audio_truth_live_disables_catalogue_suffix_repair():
    # Regression contract for Los Pericos: the authoritative live stream must
    # not be replaced by a second copy of its own collapsed wx_raw suffix.
    src = inspect.getsource(main._maybe_adlib_filter)
    assert 'live_hint and not result.get("live_audio_truth")' in src
    assert "_audit_on = _catalogue_suffix_repair" in src
    assert "if (_catalogue_suffix_repair and _wx_raw" in src


def test_adlib_filter_cannot_run_without_signed_quality_gate(monkeypatch):
    monkeypatch.setattr(main, "_quality_mutation_authorized", lambda _job_id: False)
    monkeypatch.setenv("ADLIB_CONSENSUS_ENABLED", "1")
    source = {"segments": [
        {"start": 1.0, "end": 2.0, "text": "one"},
        {"start": 3.0, "end": 4.0, "text": "two"},
        {"start": 5.0, "end": 6.0, "text": "uoh"},
    ], "wx_raw": [{"text": "private transport"}]}
    out = asyncio.run(main._maybe_adlib_filter(source, "/missing.wav", "job"))
    assert out["segments"] == source["segments"]
    assert "wx_raw" not in out
    assert out["postpass_stats"]["adlib_consensus"] == {
        "mode": "observe", "mutation_authorized": False,
    }


def test_divergent_llm_branch_is_bound_to_shared_mutation_gate():
    src = inspect.getsource(main._run_transcription_for_job)
    branch = src[src.index("_llm_segs = ("):]
    assert "_is_divergent\n                                and _quality_mutation_authorized(job_id)" in branch


def test_final_credit_filter_preserves_unverified_cc_text_and_sung_repetition():
    source = {"segments": [
        {"start": 79.0, "end": 83.0, "text": "No no no no no no no no"},
        {"start": 95.0, "end": 104.0,
         "text": "CC por Antarctica Films Argentina."},
        {"start": 121.0, "end": 123.0, "text": "¡Gracias!"},
    ]}
    out = transcription_worker._drop_final_credit_hallucinations(source, "job")
    assert [segment["text"] for segment in out["segments"]] == [
        "No no no no no no no no",
        "CC por Antarctica Films Argentina.",
        "¡Gracias!",
    ]
    assert (out.get("postpass_stats") or {}).get(
        "final_credit_filter", {"dropped": 0},
    ) == {"dropped": 0}
