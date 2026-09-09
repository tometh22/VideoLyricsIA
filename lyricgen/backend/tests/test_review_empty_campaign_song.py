from copy import deepcopy
import json

import pytest

from reviewer_integral import windows
from reviewer_shadow import source_binding
from scripts.review_empty_campaign_song import JOB_ID, NoRetryLedger, diagnose, target_song
from scripts import review_empty_campaign_song as module
from shadow_reference_import import digest


def song():
    return {"job_id": JOB_ID, "ordinal": 113, "audio_sha256": "a"*64, "audio_revision": 1,
            "segments_revision": 0, "segments_sha256": digest([]), "segments": [],
            "original_segments": [], "duration_seconds": 149.28, "status": "transcribed_pending"}


def caches(kind="sung", whisper_text="hola", gemini_text="hola", *, full=True):
    window={"start":0.,"end":24.,"offset_seconds":0.}
    def record(provider,text,kind=None):
        return {"request":{"provider":provider,"window":window,"usage":{}},
                "evidence_sha256":("b" if provider=="openai" else "c")*64,
                "annotations":[{"text":text,"kind":kind,"global_start":1.,"global_end":2.,
                                "local_start":1.,"local_end":2.}], "invalid_annotations":[]}
    return {"records":[record("openai",whisper_text),record("google",gemini_text,kind)],
            "receipts":[{"family":f,"start":0.,"end":149.28 if full else 24.}
                        for f in ("openai/whisper-1","google/gemini-2.5-flash-audio")], "excluded":[]}


def test_agreement_only_diagnostic_not_fake_candidate():
    baseline=song()
    result=diagnose(baseline,caches())
    assert result["acoustic_review_complete"]
    assert not result["complete_candidate"]
    assert result["baseline"]==[]==baseline["segments"]
    suggestion=result["offline_insertion_hypotheses"][0]
    assert suggestion["current_segments"]==[]
    assert suggestion["status"]=="offline_insertion_hypothesis_not_adoptable"
    assert suggestion["same_occurrence_certified"] is False
    assert result["blocker"]=="empty_baseline_insertion_hypotheses_require_occurrence_alignment_and_bridge"


def test_vocalization_does_not_become_lexical_insertion():
    result=diagnose(song(),caches(kind="vocalization",whisper_text="oh",gemini_text="oh"))
    assert result["offline_insertion_hypotheses"]==[]
    assert result["blocker"]=="empty_baseline_vocalization_or_speech_editorial_review_required"


def test_no_agreement_preserves_contradictory_words():
    result=diagnose(song(),caches(whisper_text="otra frase"))
    assert result["offline_insertion_hypotheses"]==[]
    assert result["word_evidence"][0]["words"][0]["text"]=="otra frase"
    assert result["blocker"]=="empty_baseline_no_cross_family_lexical_support"


def test_partial_coverage_remains_incomplete_even_with_agreement():
    result=diagnose(song(),caches(full=False))
    assert not result["acoustic_review_complete"]
    assert result["blocker"]=="empty_baseline_acoustic_review_incomplete"


def test_other_occurrence_not_matched_by_text_alone():
    cache=caches()
    cache["records"][0]["annotations"][0].update(global_start=10.,global_end=11.)
    assert diagnose(song(),cache)["offline_insertion_hypotheses"]==[]


def test_unknown_and_malformed_attempts_not_retried():
    class Ledger:
        def reserve(self,identity,*args):return False,identity
    ledger=NoRetryLedger(Ledger())
    assert ledger.reserve("invalid_response")== (False,"empty_diagnostic_known_invalid_not_retried")
    assert ledger.reserve("reserved_unknown_completion")== (False,"reserved_unknown_completion")


def fixture():
    jobs=[{**song(),"job_id":f"{i:012d}","ordinal":i+1} for i in range(300)]
    jobs[112]=song()
    snapshot={"jobs":jobs,"campaign_id":"campaign","snapshot_sha256":digest(jobs)}
    manifest={"campaign_id":"campaign","songs":[{"job_id":j["job_id"],"source":source_binding(j),"windows":windows(j["duration_seconds"])} for j in jobs]}
    return snapshot,manifest


def test_only_exact_empty_unapproved_target():
    snapshot,manifest=fixture()
    baseline=deepcopy(manifest)
    result,row=target_song(snapshot,manifest)
    assert result["job_id"]==JOB_ID
    assert manifest==baseline
    assert len(row["windows"])==8
    snapshot["jobs"][112]["approved_at"]="human approval"
    snapshot["snapshot_sha256"]=digest(snapshot["jobs"])
    with pytest.raises(ValueError,match="exact_unapproved_empty_target_required"):
        target_song(snapshot,manifest)


def test_source_change_rejected_and_other_song_roster_rejected():
    snapshot,manifest=fixture()
    manifest["songs"][112]["source"]["segments_revision"]=1
    with pytest.raises(ValueError,match="source_mismatch"):
        target_song(snapshot,manifest)
    snapshot,manifest=fixture()
    manifest["songs"][0]["job_id"]="different"
    with pytest.raises(ValueError,match="roster_mismatch"):
        target_song(snapshot,manifest)


def test_cache_only_run_no_calls_never_marks_empty_complete(tmp_path,monkeypatch):
    snapshot,manifest=fixture()
    for row in manifest["songs"]:
        row.update(status="pending",duration_seconds=149.28,candidate_available=False,
                   backed_changes=0,reconciliation_complete=False)
    before=deepcopy(manifest)
    folder=tmp_path/"campaign-300"
    folder.mkdir()
    (folder/"manifest.json").write_text(json.dumps(manifest))
    path=tmp_path/"snapshot.json"
    path.write_text(json.dumps(snapshot))
    cache=caches()
    for receipt in cache["receipts"]:
        receipt.update(source=source_binding(song()),clock="original_mix_decoded",
                       evidence_sha256="e"*64,tool_status="ok",received_audio=True)
    monkeypatch.setattr(module,"request_index",lambda *a,**k: [])
    monkeypatch.setattr(module,"cached_receipts",lambda *a,**k: cache)
    def forbidden(*a,**k):raise AssertionError("no inference or ledger allowed")
    monkeypatch.setattr(module,"execute_request_batches",forbidden)
    monkeypatch.setattr(module,"SpendLedger",forbidden)
    result=module.run(tmp_path,path)
    after=json.loads((folder/"manifest.json").read_text())
    assert result["new_attempts"]==0
    assert after["songs"][112]["status"]=="blocked"
    assert after["songs"][112]["candidate_available"] is False
    assert all(old==new for i,(old,new) in enumerate(zip(before["songs"],after["songs"])) if i!=112)
