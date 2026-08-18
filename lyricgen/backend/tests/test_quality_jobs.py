from datetime import datetime, timedelta, timezone
import json
import inspect
import uuid

import quality_jobs
import line_evidence
import structural_hybrid
import transcription_quality as tq
from auth import create_user
from database import Job


def test_attested_asr_context_connects_persisted_words_by_independent_family(
        monkeypatch):
    monkeypatch.setenv(
        "QUALITY_CONTENT_FINGERPRINT_HMAC_KEY",
        "quality-test-key-0123456789-ABCDEF",
    )
    monkeypatch.setenv("QUALITY_CONTENT_FINGERPRINT_HMAC_KEY_ID", "test-v1")
    base = {
        "start": 1.0, "end": 2.0, "text": "private",
        "words": [{"word": "private", "start": 1.0, "end": 1.8}],
    }
    first = line_evidence.annotate_provider_evidence(
        [base], source="whisperx_primary", provider="provider-a",
        model="model-a", correlated_family="family-a",
    )[0]
    second = line_evidence.annotate_provider_evidence(
        [{**base, "start": 3.0, "end": 4.0,
          "words": [{"word": "witness", "start": 3.0, "end": 3.8}]}],
        source="whisper_api_secondary", provider="provider-b", model="model-b",
        correlated_family="family-b",
    )[0]
    reference = line_evidence.annotate_provider_evidence(
        [base], source="operator_reference", reference_text="private",
        correlated_family="poison-family",
    )[0]

    context = quality_jobs._attested_asr_context([first, second, reference])

    assert context["_primary_asr_family"] == "family-a"
    assert context["_independent_asr_family"] == "family-b"
    assert [item["word"] for item in context["_asr_words"]] == ["private"]
    assert [item["word"] for item in context["_independent_asr_words"]] == [
        "witness",
    ]
    assert "poison-family" not in context.values()

    # Editable segment fields are not part of provider recognition evidence.
    first["words"][0]["word"] = "forged-visible-word"
    unchanged = quality_jobs._attested_asr_context([first])
    assert [item["word"] for item in unchanged["_asr_words"]] == ["private"]

    # The snapshot words and lineage family are cryptographically bound.
    first["provider_evidence"]["words"][0]["word"] = "forged-snapshot-word"
    assert quality_jobs._attested_asr_context([first])["_asr_words"] == []
    second["content_provenance"]["lineage"]["correlated_family"] = "forged-family"
    assert quality_jobs._attested_asr_context([second])["_asr_words"] == []


def test_failure_callback_persists_only_error_type_not_provider_message(
        monkeypatch, caplog):
    secret = "PRIVATE_LYRIC_IN_PROVIDER_EXCEPTION"
    captured = {}

    class FakeJob:
        id = "quality:test"
        retries_left = 0
        args = ("job-id",)
        kwargs = {
            "expected_revision": 2,
            "expected_segments_hash": "hash",
            "analysis_attempt_id": "attempt",
        }

    monkeypatch.setattr(quality_jobs, "_snapshot", lambda _job_id: {
        "segments": [{"start": 1.0, "end": 2.0, "text": "line"}],
        "quality": {"metrics": {}, "unsafe_windows": []},
    })
    monkeypatch.setattr(
        quality_jobs, "_persist_if_current",
        lambda _job_id, _revision, _hash, quality, **_kwargs:
        captured.setdefault("quality", quality) is quality,
    )

    quality_jobs.transcription_quality_failure_callback(
        FakeJob(), None, RuntimeError, RuntimeError(secret), None,
    )

    encoded = json.dumps(captured["quality"], ensure_ascii=False)
    assert secret not in encoded
    assert secret not in caplog.text
    assert "provider_failure:RuntimeError" in encoded


def test_quality_observability_never_persists_or_logs_raw_audio_hashes(
        monkeypatch):
    monkeypatch.setenv(
        "QUALITY_CONTENT_FINGERPRINT_HMAC_KEY",
        "quality-test-key-0123456789-ABCDEF",
    )
    monkeypatch.setenv("QUALITY_CONTENT_FINGERPRINT_HMAC_KEY_ID", "test-v1")
    source_hash, stem_hash = "a" * 64, "b" * 64

    safe = quality_jobs._safe_audio_identity_metrics(source_hash, stem_hash)
    encoded = json.dumps(safe)

    assert source_hash not in encoded
    assert stem_hash not in encoded
    assert safe["audio_fingerprint"].startswith("hmac-sha256:v1:test-v1:")
    assert safe["stem_fingerprint"].startswith("hmac-sha256:v1:test-v1:")
    persist_source = inspect.getsource(quality_jobs._persist_if_current)
    assert "expected_hash[:" not in persist_source
    assert "audio_sha256[:" not in persist_source


def test_pending_reconciler_filters_and_streams_all_pending_rows():
    source = inspect.getsource(quality_jobs.reconcile_stale_pending_quality_jobs)
    assert '"analysis_pending"' in source
    assert ".order_by(pending_since.asc(), Job.job_id.asc())" in source
    assert ".yield_per(page_size)" in source
    assert ".limit(" not in source
    assert "content_hash[:" not in source


def test_pending_marker_staleness_distinguishes_recent_and_orphaned_jobs():
    now = datetime(2026, 8, 16, tzinfo=timezone.utc)
    recent = {
        "analysis_pending": True,
        "analysis_enqueued_at": (now - timedelta(seconds=30)).isoformat(),
    }
    stale = {
        "analysis_pending": True,
        "analysis_enqueued_at": (now - timedelta(minutes=20)).isoformat(),
    }
    assert quality_jobs._pending_marker_is_stale(
        recent, now=now, max_age_s=900,
    ) is False
    assert quality_jobs._pending_marker_is_stale(
        stale, now=now, max_age_s=900,
    ) is True
    assert quality_jobs._pending_marker_is_stale({
        "analysis_pending": True, "analysis_enqueued_at": "invalid",
    }, now=now) is True


def test_persisted_acoustic_diagnostics_strip_raw_lyrics_and_words():
    secret = "private lyric sentinel"
    sanitized = quality_jobs._sanitize_analytical_evidence({
        "events": [{
            "id": "ae1", "start": 1.0, "end": 2.0,
            "text": secret, "words": [{"word": secret}],
        }],
        "phonetic_candidates": [{"texts": [secret, secret]}],
    })
    encoded = json.dumps(sanitized)
    assert secret not in encoded
    assert sanitized["events"][0]["text_present"] is True
    assert sanitized["events"][0]["word_count"] == 1
    assert sanitized["phonetic_candidates"][0]["text_count"] == 2


def test_analytical_sanitizer_is_recursive_allow_list_not_key_blacklist(
    monkeypatch,
):
    monkeypatch.setenv(
        "QUALITY_CONTENT_FINGERPRINT_HMAC_KEY",
        "quality-test-key-0123456789-ABCDEF",
    )
    monkeypatch.setenv("QUALITY_CONTENT_FINGERPRINT_HMAC_KEY_ID", "quality-v1")
    secret = "LETRA_PRIVADA_REAL_UOH_UOH"
    raw_audio_hash = "a" * 64
    poisoned = {
        "lyrics": secret,
        "transcript": secret,
        "content_candidates": [secret],
        "provider_payload": {"utterance": secret},
        "stem_sha256": raw_audio_hash,
        "events": [{
            "start": 1.0, "end": 2.0,
            "provider_payload": {"utterance": secret},
            "reason": secret,
        }],
    }
    sanitized = quality_jobs._sanitize_analytical_evidence(poisoned)
    encoded = json.dumps(sanitized, ensure_ascii=False)
    assert secret not in encoded
    assert "lyrics" not in sanitized
    assert "transcript" not in sanitized
    assert "content_candidates" not in sanitized
    assert "provider_payload" not in sanitized
    assert sanitized["events"][0]["reason"] == "redacted"
    assert "stem_sha256" not in sanitized
    assert raw_audio_hash not in encoded
    assert sanitized["stem_fingerprint"].startswith(
        "hmac-sha256:v1:quality-v1:"
    )


def test_quality_windows_are_prioritized_by_risk_then_duration():
    windows = [
        {"id": "early-low", "start": 1, "end": 30,
         "reasons": ["uncovered_asr"]},
        {"id": "late-critical", "start": 60, "end": 64,
         "reasons": ["live_structural_disagreement"]},
        {"id": "early-critical", "start": 40, "end": 42,
         "reasons": ["live_structural_disagreement"]},
    ]
    ordered = quality_jobs._prioritize_windows(windows)
    assert [item["id"] for item in ordered] == [
        "late-critical", "early-critical", "early-low",
    ]


def test_unprocessed_quality_windows_fail_closed():
    quality = tq.evaluate(
        [{"start": 1.0, "end": 2.0, "text": "line"}],
        {"audio_coverage": 1.0, "text_mismatches": 0,
         "voiced_gap_s": 0, "uncovered_seconds": 0},
        retry_stats={"attempted": True, "windows_skipped": 2},
    )
    assert quality["decision"] == "review_required"
    assert any(
        reason["code"] == "quality_windows_unprocessed"
        for reason in quality["reasons"]
    )


def test_truncated_quality_windows_fail_closed():
    quality = tq.evaluate(
        [{"start": 1.0, "end": 2.0, "text": "line"}],
        {"audio_coverage": 1.0, "text_mismatches": 0,
         "voiced_gap_s": 0, "uncovered_seconds": 0},
        retry_stats={"attempted": True, "windows_truncated": 1},
    )
    assert quality["decision"] == "review_required"
    assert any(
        reason["code"] == "quality_windows_truncated"
        for reason in quality["reasons"]
    )


def test_failure_callback_does_not_publish_before_rq_retry(monkeypatch):
    class FakeJob:
        id = "quality:test"
        retries_left = 1
        args = ("job-id",)
        kwargs = {"expected_revision": 0, "expected_segments_hash": "hash"}

    snapshot = __import__("unittest.mock").mock.Mock()
    monkeypatch.setattr(quality_jobs, "_snapshot", snapshot)
    quality_jobs.transcription_quality_failure_callback(
        FakeJob(), None, RuntimeError, RuntimeError("transient"), None,
    )
    snapshot.assert_not_called()


def test_quality_job_rejects_worker_with_different_runtime_identity(monkeypatch):
    monkeypatch.setattr(
        "queue_jobs._transcription_quality_runtime_token",
        lambda: "new-runtime-token",
    )
    snapshot = __import__("unittest.mock").mock.Mock()
    monkeypatch.setattr(quality_jobs, "_snapshot", snapshot)
    result = quality_jobs.run_transcription_quality_job(
        "job-id", expected_revision=0, expected_segments_hash="a" * 64,
        quality_runtime_token="old-runtime-token",
    )
    assert result == {
        "status": "discarded", "reason": "runtime_identity_mismatch",
    }
    snapshot.assert_not_called()


def test_quality_persist_discards_same_hash_from_older_audio_revision(db):
    tenant = f"quality_occ_{uuid.uuid4().hex[:8]}"
    user = create_user(
        db, f"quality_occ_{uuid.uuid4().hex[:8]}", "testpass12345", None,
        tenant_id=tenant,
    )
    job_id = uuid.uuid4().hex[:12]
    segments = [{"start": 1.0, "end": 2.0, "text": "line"}]
    content_hash = tq.segments_hash(segments)
    db.add(Job(
        job_id=job_id, user_id=user.id, tenant_id=tenant,
        artist="Artist", song_title="Song", filename="song.wav",
        style="oscuro", status="transcribed_pending", current_step="editing",
        delivery_profile="youtube", segments_json=segments,
        segments_revision=2, input_audio_sha256="a" * 64,
        audio_revision=8, active_quality_attempt_id="attempt-current",
        transcription_quality={"analysis_status": "pending"},
    ))
    db.commit()

    candidate = tq.evaluate(segments, None)
    persisted = quality_jobs._persist_if_current(
        job_id, 2, content_hash, candidate,
        expected_audio_revision=7,
        expected_audio_sha256="a" * 64,
        analysis_attempt_id="attempt-current",
    )
    assert persisted is False
    db.expire_all()
    row = db.query(Job).filter(Job.job_id == job_id).one()
    assert row.transcription_quality == {"analysis_status": "pending"}
    assert row.active_quality_attempt_id == "attempt-current"

    persisted = quality_jobs._persist_if_current(
        job_id, 2, content_hash, candidate,
        expected_audio_revision=8,
        expected_audio_sha256="a" * 64,
        analysis_attempt_id="attempt-current",
    )
    assert persisted is True
    db.expire_all()
    row = db.query(Job).filter(Job.job_id == job_id).one()
    assert row.transcription_quality["audio_revision"] == 8
    assert row.active_quality_attempt_id is None


def test_ctc_mapping_cannot_resolve_content_without_independent_attestation(
        monkeypatch):
    stem_hash = "a" * 64
    mix_hash = "b" * 64
    segments = [
        {"start": 60.85, "end": 63.77, "text": "Real, uoh uoh"},
        {"start": 63.77, "end": 67.04, "text": "Real, uoh uoh"},
    ]
    windows = [{
        "id": "refrain", "start": 60.0, "end": 68.0,
        "reasons": ["live_structural_disagreement"],
    }]
    diagnostic = [{
        "window": [57.0, 71.0],
        "evidence": {"content_mapping": {
            "accepted": True, "phonetic_verified": True,
            "selected_candidate_id": "gemini-1",
            "phonetic_evidence": {
                "accepted": True, "schema": "ctc-phonetic-evidence-v1",
                "calibration_id": "gold-v1", "evidence_sha256": "e" * 64,
                "model_identity": {"model_revision": "f" * 40},
            },
            "strong_unassigned_events": 0,
            "events": [dict(item) for item in segments],
        }},
    }]
    unresolved, resolved = quality_jobs._confirmed_windows(
        segments, windows, diagnostic,
        expected_stem_sha256=stem_hash, expected_mix_sha256=mix_hash,
    )
    assert [item["id"] for item in unresolved] == ["refrain"]
    assert resolved == []

    mapping = diagnostic[0]["evidence"]["content_mapping"]
    monkeypatch.setenv(
        "QUALITY_CONTENT_ATTESTATION_KEY",
        "quality-test-key-0123456789-ABCDEF",
    )
    for event in mapping["events"]:
        event["content_source"] = "gemini_audio"
    attested = structural_hybrid._attest_independent_content(
        mapping, [{
            "source": "slowed_stem_whisper", "family": "openai_whisper",
            "events": [dict(item) for item in segments],
        }], context={
            "window": [57.0, 71.0],
            "stem_sha256": stem_hash, "mix_sha256": mix_hash,
        },
    )
    mapping["independent_content_verified"] = True
    mapping["independent_content_evidence_sha256"] = attested["evidence_sha256"]
    mapping["independent_content_attestation"] = attested["attestation"]
    unresolved, resolved = quality_jobs._confirmed_windows(
        segments, windows, diagnostic,
        expected_stem_sha256=stem_hash, expected_mix_sha256=mix_hash,
    )
    assert unresolved == []
    assert [item["id"] for item in resolved] == ["refrain"]

    original_signature = mapping["independent_content_attestation"][
        "signature_hmac_sha256"
    ]
    mapping["independent_content_attestation"]["signature_hmac_sha256"] = "0" * 64
    unresolved, resolved = quality_jobs._confirmed_windows(
        segments, windows, diagnostic,
        expected_stem_sha256=stem_hash, expected_mix_sha256=mix_hash,
    )
    assert [item["id"] for item in unresolved] == ["refrain"]
    assert resolved == []
    mapping["independent_content_attestation"][
        "signature_hmac_sha256"
    ] = original_signature

    diagnostic[0]["evidence"]["content_mapping"]["events"][1]["text"] = "No"
    unresolved, resolved = quality_jobs._confirmed_windows(
        segments, windows, diagnostic,
        expected_stem_sha256=stem_hash, expected_mix_sha256=mix_hash,
    )
    assert [item["id"] for item in unresolved] == ["refrain"]
    assert resolved == []


def test_independent_attestation_is_bound_to_candidate_window_and_audio(monkeypatch):
    monkeypatch.setenv(
        "QUALITY_CONTENT_ATTESTATION_KEY",
        "quality-test-key-0123456789-ABCDEF",
    )
    events = [{
        "start": 60.0, "end": 63.0, "text": "Real uoh uoh",
        "content_source": "gemini_audio",
    }]
    mapping = {"selected_candidate_id": "gemini-1", "events": events}
    attested = structural_hybrid._attest_independent_content(
        mapping, [{
            "family": "openai_whisper", "events": [dict(events[0])],
        }], context={
            "window": [57.0, 71.0],
            "stem_sha256": "a" * 64, "mix_sha256": "b" * 64,
        },
    )
    mapping.update({
        "independent_content_attestation": attested["attestation"],
        "independent_content_evidence_sha256": attested["evidence_sha256"],
    })
    valid = lambda **overrides: quality_jobs._valid_independent_content_attestation(
        mapping,
        expected_window=overrides.get("window", [57.0, 71.0]),
        expected_stem_sha256=overrides.get("stem", "a" * 64),
        expected_mix_sha256=overrides.get("mix", "b" * 64),
    )
    assert valid()
    assert not valid(window=[58.0, 71.0])
    assert not valid(stem="c" * 64)
    assert not valid(mix="d" * 64)
    mapping["selected_candidate_id"] = "other-candidate"
    assert not valid()


def test_independent_attestation_rejects_weak_hmac_secret(monkeypatch):
    monkeypatch.setenv("QUALITY_CONTENT_ATTESTATION_KEY", "weak-test-key")
    mapping = {
        "selected_candidate_id": "gemini-1",
        "events": [{
            "start": 1.0, "end": 2.0, "text": "private",
            "content_source": "gemini_audio",
        }],
    }
    result = structural_hybrid._attest_independent_content(
        mapping,
        [{"family": "openai_whisper", "events": [dict(mapping["events"][0])]}],
    )
    assert result == {
        "verified": False, "reason": "attestation_key_unavailable",
    }
