import asyncio

import gap_rescue
import main
import pipeline
import vocal_sep
import word_vote


def test_live_verifier_persists_independent_words_for_quality_gate(
        tmp_path, monkeypatch):
    audio = tmp_path / "mix.wav"
    stem = tmp_path / "stem.wav"
    audio.write_bytes(b"mix")
    stem.write_bytes(b"stem")
    witness = [
        {"word": f"w{i}", "start": i * 0.3, "end": i * 0.3 + 0.2}
        for i in range(10)
    ]
    monkeypatch.setenv("LIVE_INDEPENDENT_VERIFY_ENABLED", "1")
    monkeypatch.delenv("WORD_VOTE_ENABLED", raising=False)
    monkeypatch.setattr(vocal_sep, "separate_vocals", lambda *_a, **_k: str(stem))
    monkeypatch.setattr(pipeline, "_audio_duration", lambda *_a: 12.0)
    monkeypatch.setattr(gap_rescue, "_transcribe_window", lambda *_a, **_k: witness)
    monkeypatch.setattr(
        word_vote, "vote",
        lambda segments, _witness: (
            segments,
            {"substitutions": 0, "insertions": 0,
             "lines_changed": 0, "declined": []},
        ),
    )
    result = {"segments": [
        {"start": 0.0, "end": 3.0, "text": "heard line"},
    ]}
    out = asyncio.run(main._maybe_word_vote(
        result, str(audio), "job", "es", live_hint=True,
    ))
    assert out["_independent_asr_words"] == witness
    stats = out["postpass_stats"]["word_vote"]
    assert stats["independent_verifier"] is True
    assert stats["verification_only"] is True
    assert stats["witness_words"] == 10


def test_live_witness_never_rewrites_the_content_it_certifies(
        tmp_path, monkeypatch):
    audio = tmp_path / "mix.wav"
    stem = tmp_path / "stem.wav"
    audio.write_bytes(b"mix")
    stem.write_bytes(b"stem")
    witness = [
        {"word": word, "start": i * 0.3, "end": i * 0.3 + 0.2}
        for i, word in enumerate(
            "Hoy temprano estuve pensando en vos con calma".split()
        )
    ]
    monkeypatch.setenv("LIVE_INDEPENDENT_VERIFY_ENABLED", "1")
    monkeypatch.setenv("WORD_VOTE_ENABLED", "1")
    monkeypatch.setattr(vocal_sep, "separate_vocals", lambda *_a, **_k: str(stem))
    monkeypatch.setattr(pipeline, "_audio_duration", lambda *_a: 12.0)
    monkeypatch.setattr(gap_rescue, "_transcribe_window", lambda *_a, **_k: witness)
    monkeypatch.setattr(
        word_vote, "vote",
        lambda *_a, **_k: (_ for _ in ()).throw(
            AssertionError("verification witness must not mutate live lyrics")
        ),
    )
    result = {"segments": [{
        "start": 0.0, "end": 3.0,
        "text": "Muy temprano estuve pensando en vos con calma",
    }]}
    out = asyncio.run(main._maybe_word_vote(
        result, str(audio), "job", "es", live_hint=True,
    ))
    assert out["segments"][0]["text"].startswith("Muy temprano")


def test_live_witness_applies_only_a_preexisting_catalogue_proposal(
        tmp_path, monkeypatch):
    monkeypatch.setattr(main, "_quality_mutation_authorized", lambda _job_id: True)
    audio = tmp_path / "mix.wav"
    stem = tmp_path / "stem.wav"
    audio.write_bytes(b"mix")
    stem.write_bytes(b"stem")
    witness = [
        {"word": word, "start": i * 0.3, "end": i * 0.3 + 0.2}
        for i, word in enumerate(
            "Hoy temprano estuve pensando en vos con calma".split()
        )
    ]
    monkeypatch.setenv("LIVE_INDEPENDENT_VERIFY_ENABLED", "1")
    monkeypatch.setenv("TRANSCRIPTION_QUALITY_MODE", "enforce")
    monkeypatch.setenv("TRANSCRIPTION_QUALITY_ENFORCE_PERCENT", "100")
    monkeypatch.setattr(vocal_sep, "separate_vocals", lambda *_a, **_k: str(stem))
    monkeypatch.setattr(pipeline, "_audio_duration", lambda *_a: 12.0)
    monkeypatch.setattr(gap_rescue, "_transcribe_window", lambda *_a, **_k: witness)
    result = {"segments": [{
        "start": 0.0, "end": 3.0,
        "text": "Muy temprano estuve pensando en vos con calma",
        "live_lexical_suggestion":
            "Hoy temprano estuve pensando en vos con calma",
    }]}
    out = asyncio.run(main._maybe_word_vote(
        result, str(audio), "job", "es", live_hint=True,
    ))
    assert out["segments"][0]["text"].startswith("Hoy temprano")
    assert out["segments"][0]["live_lexical_verified"] is True


def test_observe_mode_records_verified_proposal_without_mutating(
        tmp_path, monkeypatch):
    audio = tmp_path / "mix.wav"
    stem = tmp_path / "stem.wav"
    audio.write_bytes(b"mix")
    stem.write_bytes(b"stem")
    witness = [
        {"word": word, "start": i * 0.3, "end": i * 0.3 + 0.2}
        for i, word in enumerate(
            "Hoy temprano estuve pensando en vos con calma".split()
        )
    ]
    monkeypatch.setenv("LIVE_INDEPENDENT_VERIFY_ENABLED", "1")
    monkeypatch.setenv("TRANSCRIPTION_QUALITY_MODE", "observe")
    monkeypatch.setattr(vocal_sep, "separate_vocals", lambda *_a, **_k: str(stem))
    monkeypatch.setattr(pipeline, "_audio_duration", lambda *_a: 12.0)
    monkeypatch.setattr(gap_rescue, "_transcribe_window", lambda *_a, **_k: witness)
    result = {"segments": [{
        "start": 0.0, "end": 3.0,
        "text": "Muy temprano estuve pensando en vos con calma",
        "live_lexical_suggestion":
            "Hoy temprano estuve pensando en vos con calma",
    }]}
    out = asyncio.run(main._maybe_word_vote(
        result, str(audio), "job", "es", live_hint=True,
    ))
    assert out["segments"][0]["text"].startswith("Muy temprano")
    assert out["postpass_stats"]["word_vote"]["lines_suggested"] == 1


def test_studio_does_not_pay_for_live_verifier(monkeypatch):
    monkeypatch.setenv("LIVE_INDEPENDENT_VERIFY_ENABLED", "1")
    monkeypatch.delenv("WORD_VOTE_ENABLED", raising=False)
    result = {"segments": [{"start": 0, "end": 1, "text": "line"}]}
    out = asyncio.run(main._maybe_word_vote(
        result, "/missing.wav", "job", "es", live_hint=False,
    ))
    assert out is result


def test_live_verifier_rejects_unknown_duration_before_asr(tmp_path, monkeypatch):
    audio = tmp_path / "mix.wav"
    stem = tmp_path / "stem.wav"
    audio.write_bytes(b"mix")
    stem.write_bytes(b"stem")
    monkeypatch.setenv("LIVE_INDEPENDENT_VERIFY_ENABLED", "1")
    monkeypatch.setattr(vocal_sep, "separate_vocals", lambda *_a, **_k: str(stem))
    monkeypatch.setattr(pipeline, "_audio_duration", lambda *_a: None)
    monkeypatch.setattr(
        gap_rescue, "_transcribe_window",
        lambda *_a, **_k: (_ for _ in ()).throw(
            AssertionError("invalid duration must decline before ASR")
        ),
    )
    out = asyncio.run(main._maybe_word_vote(
        {"segments": [{"start": 0, "end": 1, "text": "line"}]},
        str(audio), "job", "es", live_hint=True,
    ))
    assert out["postpass_stats"]["word_vote"]["declined"] == [
        "duration_unavailable",
    ]


def test_repeated_live_vocalization_is_not_filtered_as_training_credit(
        tmp_path, monkeypatch):
    audio = tmp_path / "mix.wav"
    stem = tmp_path / "stem.wav"
    audio.write_bytes(b"mix")
    stem.write_bytes(b"stem")
    witness = [
        {"word": "no", "start": i * 0.7, "end": i * 0.7 + 0.5}
        for i in range(10)
    ]
    monkeypatch.setenv("LIVE_INDEPENDENT_VERIFY_ENABLED", "1")
    monkeypatch.setattr(vocal_sep, "separate_vocals", lambda *_a, **_k: str(stem))
    monkeypatch.setattr(pipeline, "_audio_duration", lambda *_a: 12.0)
    monkeypatch.setattr(gap_rescue, "_transcribe_window", lambda *_a, **_k: witness)
    out = asyncio.run(main._maybe_word_vote(
        {"segments": [{"start": 0, "end": 8, "text": "no no no"}]},
        str(audio), "job", "es", live_hint=True,
    ))
    assert len(out["_independent_asr_words"]) == 10
    assert out["postpass_stats"]["word_vote"]["witness_words_filtered"] == 0


def test_poor_stem_witness_falls_back_to_better_blind_mix(
        tmp_path, monkeypatch):
    monkeypatch.setattr(main, "_quality_mutation_authorized", lambda _job_id: True)
    audio = tmp_path / "mix.wav"
    stem = tmp_path / "stem.wav"
    audio.write_bytes(b"mix")
    stem.write_bytes(b"stem")
    good = [
        {"word": word, "start": i * 0.35, "end": i * 0.35 + 0.25}
        for i, word in enumerate(
            "Hoy temprano estuve pensando en vos con calma".split()
        )
    ]
    bad = [
        {"word": f"junk{i}", "start": 8 + i * 0.3,
         "end": 8 + i * 0.3 + 0.2}
        for i in range(10)
    ]
    monkeypatch.setenv("LIVE_INDEPENDENT_VERIFY_ENABLED", "1")
    monkeypatch.setenv("LIVE_INDEPENDENT_MIX_FALLBACK_ENABLED", "1")
    monkeypatch.setenv("TRANSCRIPTION_QUALITY_MODE", "enforce")
    monkeypatch.setenv("TRANSCRIPTION_QUALITY_ENFORCE_PERCENT", "100")
    monkeypatch.setattr(vocal_sep, "separate_vocals", lambda *_a, **_k: str(stem))
    monkeypatch.setattr(pipeline, "_audio_duration", lambda *_a: 12.0)

    def transcribe(path, *_args, **_kwargs):
        return good if path == str(audio) else bad

    monkeypatch.setattr(gap_rescue, "_transcribe_window", transcribe)
    result = {"segments": [{
        "start": 0.0, "end": 3.0,
        "text": "Muy temprano estuve pensando en vos con calma",
        "live_lexical_suggestion":
            "Hoy temprano estuve pensando en vos con calma",
    }]}
    out = asyncio.run(main._maybe_word_vote(
        result, str(audio), "job", "es", live_hint=True,
    ))
    assert out["segments"][0]["text"].startswith("Hoy temprano")
    stats = out["postpass_stats"]["word_vote"]
    assert stats["witness_source"] == "mix"
    assert stats["provider_attempts"] == 2
    assert stats["audio_seconds_billed"] == 24.0
