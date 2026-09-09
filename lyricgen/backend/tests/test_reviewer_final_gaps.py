from copy import deepcopy
import json
import sqlite3
from types import SimpleNamespace

import pytest

from scripts import recover_campaign_final_gaps as final
from reviewer_shadow import source_binding
from shadow_reference_import import digest


def song(duration=30.):
    segments = [{"start": 1., "end": 4., "text": "Control", "locked": True}]
    return {"job_id": "song", "audio_sha256": "a" * 64, "audio_revision": 1,
        "segments_revision": 3, "segments": segments, "segments_sha256": digest(segments),
        "duration_seconds": duration}


def record(s, start, end, status="ok", *, prompt=final.V2):
    request = {"provider": "google", "model": "gemini-2.5-flash", "family": final.FAMILY,
        "source": source_binding(s), "clip_sha256": digest([start, end]), "view": "mix",
        "conditioning_texts": [], "window": {"start": start, "end": end, "offset_seconds": start},
        "prompt_version": prompt, "tool_status": status, "received_audio": status in {"ok", "invalid_response"},
        "response": {"events": [], "editorial_ambiguity": False, "ambiguity_reason": "none", "reverb": "absent"}}
    return {"request": request, "cache_path": "/synthetic/requests/result.json", "evidence_sha256": digest(request)}


def fixture():
    s = song()
    return s, [record(s, 0., 10.), record(s, 15., 30.), record(s, 8., 18., "invalid_response")]


def test_short_known_gap_has_bounded_context_and_preserves_snapshot():
    s, index = fixture(); before = deepcopy(s)
    result = final.plan_final_gaps(s, index)
    assert result["blockers"] == []
    assert result["clips"][0]["gap"] == {"start": 10., "end": 15.}
    assert result["clips"][0]["window"] == {"start": 9.5, "end": 15.5, "offset_seconds": 9.5}
    assert s == before


def test_known_first_ten_style_final_4613ms_gap():
    s = song(34.613)
    index = [record(s, 0., 24.), record(s, 18., 30.), record(s, 18., 34.613, "invalid_response")]
    result = final.plan_final_gaps(s, index)
    assert result["clips"][0]["window"]["end"] == 34.613
    index.append(record(s, 29.5, 34.613))
    assert final.plan_final_gaps(s, index)["clips"] == []


@pytest.mark.parametrize("status", ["unknown_completion", "tool_error", "reserved_unknown_completion"])
def test_unknown_or_tool_error_overlap_is_not_bypassed(status):
    s, index = fixture()
    index.append(record(s, 9., 11., status))
    result = final.plan_final_gaps(s, index)
    assert result["clips"] == []
    assert result["blockers"] == ["unknown_or_tool_error_overlaps_final_clip"]


def test_context_overlap_also_blocks_unknown():
    s, index = fixture()
    index.append(record(s, 9.6, 9.9, "unknown_completion"))
    assert final.plan_final_gaps(s, index)["clips"] == []


def test_long_failed_parent_needs_existing_subdivision():
    s, index = fixture(); index[-1] = record(s, 0., 24., "invalid_response")
    assert final.plan_final_gaps(s, index)["clips"] == []
    assert len(final.plan_final_gaps(s, index, subdivided=[digest(index[-1]["request"])])["clips"]) == 1


def test_v1_failure_or_wrong_audio_cannot_authorize_new_window():
    s, index = fixture()
    index[-1]["request"]["prompt_version"] = "blind-vocal-events-shadow-v1"
    assert final.plan_final_gaps(s, index)["clips"] == []
    index[-1]["request"]["prompt_version"] = final.V2
    index[-1]["request"]["source"]["audio_sha256"] = "b" * 64
    assert final.plan_final_gaps(s, index)["clips"] == []


def test_gap_over_six_seconds_is_partitioned_inside_same_final_round():
    s, index = fixture(); index[1] = record(s, 17., 30.)
    result = final.plan_final_gaps(s, index)
    assert result["blockers"] == []
    assert [c["gap"] for c in result["clips"]] == [{"start": 10., "end": 16.}, {"start": 16., "end": 17.}]


def test_more_than_four_gaps_rejected_as_whole_plan():
    s = song(30.)
    index = [record(s, float(start), float(start + 1)) for start in range(0, 30, 5)]
    result = final.plan_final_gaps(s, index)
    assert result["clips"] == []
    assert result["blockers"] == ["final_round_gap_count_or_total_exceeded"]


def test_crash_before_attempt_marker_blocks_overlapping_ledger_reservation():
    s, index = fixture(); window = {"start": 0., "end": 24., "offset_seconds": 0.}
    manifest = {"songs": [{"source": source_binding(s), "windows": [window]}], "method_sha256": "frozen"}
    identity = digest({"audio": final.audio_identity(s), "window": window, "provider": "google", "method": "frozen"})
    ledger = SimpleNamespace(db=SimpleNamespace(execute=lambda *a: [(identity, "reserved_unknown_completion")]))
    assert final.reservation_windows(ledger, manifest, index, s) == [window]
    ledger.db.execute = lambda *a: [("unattributed", "reserved_unknown_completion")]
    with pytest.raises(ValueError, match="unattributed_unknown"):
        final.reservation_windows(ledger, manifest, index, s)


def setup_execution(tmp_path, monkeypatch):
    s, index = fixture(); folder = tmp_path / "campaign-300"; folder.mkdir()
    manifest = {"campaign_id": "test", "roster_sha256": "roster", "method_sha256": "method",
        "songs": [{"job_id": s["job_id"], "source": source_binding(s), "windows": []}]}
    (folder / "manifest.json").write_text(json.dumps(manifest))
    auth = {k: manifest[k] for k in ("campaign_id", "roster_sha256", "method_sha256")}
    auth.update(approved_usd=20, max_attempts=100, human_approval_reference="synthetic-test")
    (folder / "authorization.json").write_text(json.dumps(auth))
    monkeypatch.setattr(final, "verify_audio", lambda *a: None)
    monkeypatch.setattr(final, "request_index", lambda *a, **k: index)
    monkeypatch.setattr(final, "project", lambda *a, **k: {"exceeds_budget": False})
    monkeypatch.setattr(final, "extract_clip", lambda audio, window, output: output.write_bytes(b"test-only"))
    calls = []
    class Listener:
        def __init__(self, *a, **k): pass
        def listen(self, clip, **kwargs):
            calls.append(kwargs)
            entry = record(s, kwargs["window"]["start"], kwargs["window"]["end"])
            index.append(entry)
            result = entry["request"]
            result["usage"] = {"prompt_token_count": 100, "candidates_token_count": 100}
            return result
    monkeypatch.setattr(final, "BlindAudioTools", Listener)
    return s, folder, calls


def test_execute_and_resume_use_one_lifetime_call_only(monkeypatch, tmp_path):
    s, folder, calls = setup_execution(tmp_path, monkeypatch)
    assert final.recover(tmp_path, {"jobs": [s]}, "song")["executed"] is False
    assert calls == []
    result = final.recover(tmp_path, {"jobs": [s]}, "song", execute=True)
    assert len(calls) == 1 and result["clips"][0]["status"] == "ok"
    final.recover(tmp_path, {"jobs": [s]}, "song", execute=True)
    assert len(calls) == 1
    assert not (folder / "song" / "candidate.json").exists()


def test_budget_projection_blocks_before_provider_call(monkeypatch, tmp_path):
    s, folder, calls = setup_execution(tmp_path, monkeypatch)
    monkeypatch.setattr(final, "project", lambda *a, **k: {"exceeds_budget": True})
    result = final.recover(tmp_path, {"jobs": [s]}, "song", execute=True)
    assert result["blockers"] == ["projected_budget_exceeded"] and calls == []
    ledger = final.SpendLedger(folder / "spend.sqlite", approved_usd=20, max_attempts=100)
    try:
        with pytest.raises(sqlite3.IntegrityError, match="inspection_hold"):
            ledger.reserve("later-call", "google", 6.)
    finally:
        ledger.db.close()


def test_exact_twenty_dollar_authority_required(monkeypatch, tmp_path):
    s, folder, calls = setup_execution(tmp_path, monkeypatch)
    path = folder / "authorization.json"; value = json.loads(path.read_text())
    value["approved_usd"] = 21; path.write_text(json.dumps(value))
    with pytest.raises(ValueError, match="exact_authorized_usd20"):
        final.recover(tmp_path, {"jobs": [s]}, "song", execute=True)
    assert calls == []


def test_twelve_second_gap_uses_two_frozen_clips_despite_context_overlap(monkeypatch, tmp_path):
    s, _, calls = setup_execution(tmp_path, monkeypatch)
    index = final.request_index(tmp_path)
    index[:] = [record(s, 0., 10.), record(s, 22., 30.), record(s, 8., 24., "invalid_response")]
    result = final.recover(tmp_path, {"jobs": [s]}, "song", execute=True)
    assert len(calls) == 2
    assert [r["status"] for r in result["clips"]] == ["ok", "ok"]
    final.recover(tmp_path, {"jobs": [s]}, "song", execute=True)
    assert len(calls) == 2


def test_four_call_lifetime_cap_with_subdivision_evidence(monkeypatch, tmp_path):
    s, folder, calls = setup_execution(tmp_path, monkeypatch)
    s["duration_seconds"] = 24.
    index = final.request_index(tmp_path)
    index[:] = [record(s, 0., 24., "invalid_response")]
    parent_hash = digest(index[0]["request"])
    report = folder / "song" / "bounded-recovery" / "parent" / "report.json"
    report.parent.mkdir(parents=True)
    report.write_text(json.dumps({"job_id": "song", "subdivision_rounds": 1,
        "failed_evidence_sha256": parent_hash}))
    result = final.recover(tmp_path, {"jobs": [s]}, "song", execute=True)
    assert len(calls) == 4
    assert result["final_round_receipt"]["calls_lifetime_upper_bound"] == 4
    final.recover(tmp_path, {"jobs": [s]}, "song", execute=True)
    assert len(calls) == 4


def test_another_final_song_unknown_is_not_misattributed():
    s, index = fixture()
    other = {"audio": {**final.audio_identity(s), "job_id": "other"},
        "clips": [{"identity": "other-final", "window": {"start": 0., "end": 10.}}]}
    ledger = SimpleNamespace(db=SimpleNamespace(execute=lambda *a: [("other-final", "unknown_completion")]))
    assert final.reservation_windows(ledger, {"songs": [], "method_sha256": "x"}, index, s,
                                    other_final_plans=[other]) == []


@pytest.mark.parametrize("status", ["tool_error", "unknown_completion", "reserved_unknown_completion"])
def test_openai_uncertainty_does_not_veto_google_or_settle_old_reservation(tmp_path, monkeypatch, status):
    s, folder, calls = setup_execution(tmp_path, monkeypatch)
    window = {"start": 0., "end": 24., "offset_seconds": 0.}
    path = folder / "manifest.json"
    manifest = json.loads(path.read_text())
    manifest["songs"][0]["windows"] = [window]
    path.write_text(json.dumps(manifest))
    request = record(s, 0., 24., status)["request"]
    request.update(provider="openai", model="whisper-1", family="openai/whisper-1",
                   prompt_version="no-prompt-v1", http_status=429 if status == "tool_error" else None,
                   received_audio=False)
    final.request_index(tmp_path).append({"request": request, "evidence_sha256": digest(request),
                                         "cache_path": "/synthetic/openai.json"})
    identity = digest({"audio": final.audio_identity(s), "window": window,
                       "provider": "openai", "method": "method"})
    ledger = final.SpendLedger(folder / "spend.sqlite", approved_usd=20, max_attempts=100)
    ledger.reserve(identity, "openai", 24.)
    ledger.finish(identity, status, "/synthetic/original", request=request)
    old = ledger.db.execute("SELECT * FROM attempts WHERE id=?", (identity,)).fetchone()
    ledger.db.close()
    result = final.recover(tmp_path, {"jobs": [s]}, "song", execute=True)
    assert result["clips"][0]["status"] == "ok"
    assert len(calls) == 1 and calls[0]["provider"] == "google"
    receipts = final.cached_receipts(s, index=final.request_index(tmp_path))["receipts"]
    assert {r["family"] for r in receipts} == {final.FAMILY}
    assert not (folder / "song" / "candidate.json").exists()
    db = sqlite3.connect(folder / "spend.sqlite")
    assert db.execute("SELECT * FROM attempts WHERE id=?", (identity,)).fetchone() == old
    assert db.execute("SELECT count(*) FROM attempts").fetchone() == (2,)
    assert db.execute("SELECT count(*) FROM usage_accounting WHERE id=?", (identity,)).fetchone() == (0,)
    db.close()


@pytest.mark.parametrize("kind", ["canonical", "invalid_retry", "quota_retry"])
def test_openai_reservation_identity_attribution_is_provider_specific(kind):
    s, index = fixture(); window = {"start": 0., "end": 24., "offset_seconds": 0.}
    manifest = {"songs": [{"source": source_binding(s), "windows": [window]}], "method_sha256": "frozen"}
    identity = digest({"audio": final.audio_identity(s), "window": window, "provider": "openai", "method": "frozen"})
    if kind == "invalid_retry": identity = digest({"retry_of": identity, "retry_number": 1})
    if kind == "quota_retry": identity = digest({"quota_retry_of": identity, "quota_retry_number": 1})
    ledger = SimpleNamespace(db=SimpleNamespace(execute=lambda *a: [(identity, "reserved_unknown_completion")]))
    assert final.reservation_windows(ledger, manifest, index, s) == []
    assert len(final.plan_final_gaps(s, index, unknown_windows=[])["clips"]) == 1


@pytest.mark.parametrize("provider", ["google", "openai"])
def test_subdivision_reservation_retains_provider(provider):
    s, index = fixture()
    failed = record(s, 0., 24., "invalid_response")["request"]
    failed["provider"] = provider
    index.append({"request": failed})
    window = {"start": 0., "end": 13., "offset_seconds": 0.}
    identity = digest({"recovery_of": digest(failed), "window": window, "attempt": 1})
    ledger = SimpleNamespace(db=SimpleNamespace(execute=lambda *a: [(identity, "unknown_completion")]))
    result = final.reservation_windows(ledger, {"songs": [], "method_sha256": "m"}, index, s)
    assert result == ([window] if provider == "google" else [])


def test_unknown_provider_remains_conservative():
    s, index = fixture()
    unknown = record(s, 9., 11., "unknown_completion")
    unknown["request"].pop("provider")
    index.append(unknown)
    assert final.plan_final_gaps(s, index)["clips"] == []
