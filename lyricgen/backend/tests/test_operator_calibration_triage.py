"""The operational review queue must remain blind, private and non-authorizing."""
from copy import deepcopy
import json

from machine_evidence import build_machine_evidence, finalize_machine_evidence
from operator_calibration_triage import build_triage


SECRET = "0123456789abcdefghijklmnopqrstuvwxyzABCD"


def _record(job_id="job00000001", audio_hash="a" * 64):
    original = [
        {"id": "a", "start": 1.0, "end": 2.0, "text": "frase repetida"},
        {"id": "b", "start": 4.0, "end": 5.0, "text": "frase repetida"},
        {"id": "c", "start": 7.0, "end": 8.0, "text": "texto original"},
        {"id": "d", "start": 10.0, "end": 11.0, "text": "cierre original"},
        {"id": "e", "start": 13.0, "end": 14.0, "text": "sin cambios"},
    ]
    edited = deepcopy(original)
    edited[0]["end"] = 2.7
    edited[2]["text"] = "texto escuchado"
    edited[3]["text"] = "cierre escuchado"
    edited[3]["start"] = 10.4
    evidence = finalize_machine_evidence(
        build_machine_evidence({"segments": original}),
        original_segments=original,
        quality={"decision": "review_required", "timing_source": "ctc_align"},
        audio_sha256=audio_hash,
        audio_revision=0,
    )
    return {
        "job_id": job_id,
        "operator_id": 62,
        "artist": "Artista repetido",
        "audio_sha256": audio_hash,
        "audio_revision": 0,
        "current_revision": 1,
        "input_r2_key": f"private/{job_id}.wav",
        "timing_source": "ctc_align",
        "is_live": False,
        "original_segments": original,
        "current_segments": edited,
        "edited_segments": edited,
        "machine_evidence": evidence,
        "original_version": {
            "id": "initial", "revision": 0, "reason": "transcription",
            "segments": original,
            "provenance": {"schema": "machine-transcription-lineage-v1"},
        },
        "edited_version": {
            "id": "agus-save", "revision": 1, "reason": "autosave",
            "segments": edited, "created_by": 62,
        },
    }


def test_queue_has_changed_windows_and_controls_without_lyric_text():
    result = build_triage([_record()], secret=SECRET)
    assert result["summary"]["jobs_eligible"] == 1
    assert result["summary"]["tasks_by_type"] == {
        "content_review": 1,
        "joint_diagnosis": 1,
        "timing_review": 1,
        "unchanged_control": 2,
    }
    timing = next(row for row in result["queue"] if row["task_type"] == "timing_review")
    assert timing["repeated_phrase"] is True
    assert timing["sample_role"] == "difficult"
    assert timing["clip_start_s"] == 0.0
    assert result["summary"]["blind_gold_labels"] == 0
    assert all(row["automatic_apply_allowed"] is False for row in result["queue"])
    serialized = json.dumps(result, ensure_ascii=False)
    assert "frase repetida" not in serialized
    assert "texto escuchado" not in serialized


def test_stale_audio_and_unverified_occurrence_are_excluded():
    stale = _record()
    stale["audio_sha256"] = "b" * 64
    unbound = _record(job_id="job00000002", audio_hash="c" * 64)
    unbound["edited_segments"][1]["id"] = "other"
    unbound["edited_version"]["segments"] = unbound["edited_segments"]
    result = build_triage([stale, unbound], secret=SECRET)
    assert result["queue"] == []
    assert result["summary"]["jobs_excluded"] == {
        "audio_snapshot_unverified": 1,
        "structure_or_occurrence_unverified": 1,
    }


def test_superseded_operator_revision_cannot_seed_review_queue():
    stale = _record()
    stale["current_revision"] = 2
    result = build_triage([stale], secret=SECRET)
    assert result["queue"] == []
    assert result["summary"]["jobs_excluded"] == {
        "operator_checkpoint_unverified": 1,
    }


def test_artist_and_recording_groups_do_not_cross_splits():
    first = _record()
    second = _record(job_id="job00000002", audio_hash="b" * 64)
    second["artist"] = "ÁRTISTA REPETIDO"
    result = build_triage([first, second], secret=SECRET)
    assert len({row["split"] for row in result["queue"]}) == 1
    assert len(result["summary"]["songs_by_split"]) == 1
