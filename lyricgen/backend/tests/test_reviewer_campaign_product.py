from copy import deepcopy
from types import SimpleNamespace

import pytest

from reviewer_campaign_product import CAMPAIGN, KEY, campaign_payload, live_source, prepare_status, publish_song, status_for_job
from reviewer_assist_scope import display_enabled, inference_enabled, publication_enabled
from tests.test_reviewer_batch_bridge import fixture


def objects():
    song,candidate,review=fixture()
    job=SimpleNamespace(job_id=song['job_id'],campaign_id=CAMPAIGN,tenant_id='tenant',
        input_audio_sha256=song['audio_sha256'],audio_revision=song['audio_revision'],
        status='transcribed_pending',approved_at=None,transcription_quality={'existing':'preserve'})
    doc=SimpleNamespace(job_id=job.job_id,tenant_id=job.tenant_id,revision=song['segments_revision'],
        current_segments=deepcopy(song['segments']),approved_at=None,quality_proposal=None)
    row={'job_id':job.job_id,'source':live_source(job,doc),'status':'complete','coverage_seconds':{}}
    return song,candidate,review,job,doc,row


def enabled(monkeypatch):
    monkeypatch.setenv('REVIEWER_ASSIST_ENABLED','1')
    monkeypatch.setenv('REVIEWER_ASSIST_CAMPAIGN_ID',CAMPAIGN)
    monkeypatch.setenv('REVIEWER_ASSIST_PUBLISH_ENABLED','1')


def test_read_publish_inference_separate_and_scoped(monkeypatch):
    enabled(monkeypatch)
    monkeypatch.delenv('REVIEWER_ASSIST_INFERENCE_ENABLED',raising=False)
    assert display_enabled(CAMPAIGN) and publication_enabled(CAMPAIGN)
    assert not inference_enabled(CAMPAIGN)
    assert not display_enabled('other') and not publication_enabled(None)
    import reviewer_assist_runtime as runtime
    monkeypatch.setattr(runtime,'BlindAudioTools',lambda *a,**k:pytest.fail('no model calls'))
    result=runtime.run_snapshot('song',{'campaign_id':CAMPAIGN},None,None)
    assert result[1]['provider_calls']==0


def test_missing_campaign_scope_fails_closed(monkeypatch):
    monkeypatch.delenv('REVIEWER_ASSIST_CAMPAIGN_ID', raising=False)
    for name in ('REVIEWER_ASSIST_ENABLED','REVIEWER_ASSIST_PUBLISH_ENABLED','REVIEWER_ASSIST_INFERENCE_ENABLED'):
        monkeypatch.setenv(name,'1')
    for campaign in (None,CAMPAIGN,'other'):
        assert not display_enabled(campaign)
        assert not publication_enabled(campaign)
        assert not inference_enabled(campaign)


def test_summary_select_omits_heavy_document_evidence(monkeypatch):
    from sqlalchemy.orm import Session
    enabled(monkeypatch)
    _,_,_,job,doc,_=objects()
    session=Session()
    sql=[]
    class Query:
        def __init__(self, model):self.query=session.query(model)
        def options(self,*args):self.query=self.query.options(*args);return self
        def filter(self,*args):self.query=self.query.filter(*args);return self
        def all(self):sql.append(str(self.query.statement));return [doc]
    campaign_payload(SimpleNamespace(query=Query),CAMPAIGN,[(None,job)])
    assert len(sql)==1
    assert 'current_segments' in sql[0] and 'revision' in sql[0]
    assert 'machine_evidence' not in sql[0] and 'quality_proposal' not in sql[0]
    session.close()


def test_all_roster_counts_and_stale_candidate_hidden(monkeypatch):
    enabled(monkeypatch)
    _,candidate,_,job,doc,row=objects()
    job.transcription_quality[KEY]=prepare_status(row,candidate=candidate,registered=True)
    before=deepcopy(doc.current_segments)
    payload,statuses=campaign_payload(None,CAMPAIGN,[(None,job)],{job.job_id:doc})
    assert payload['counters']['complete']==1 and payload['candidate_count']==1
    doc.revision+=1
    payload,statuses=campaign_payload(None,CAMPAIGN,[(None,job)],{job.job_id:doc})
    assert payload['counters']['stale']==1 and payload['candidate_count']==0
    assert statuses[job.job_id]['blocker']=='source_changed'
    assert doc.current_segments==before


def fake_db(job,doc):
    class Query:
        def __init__(self,obj):self.obj=obj
        def filter(self,*a):return self
        def populate_existing(self):return self
        def with_for_update(self):return self
        def first(self):return self.obj
        def one(self):return self.obj
    class DB:
        def query(self,model):return Query(job if model.__name__=='Job' else doc)
    return DB()


def test_stale_publication_writes_status_only_no_registry(monkeypatch):
    enabled(monkeypatch)
    song,candidate,review,job,doc,row=objects();doc.revision+=1
    before=deepcopy(doc.__dict__)
    monkeypatch.setattr('reviewer_candidate_registry.register_candidate',lambda *a,**k:pytest.fail('stale R2 write'))
    result=publish_song(fake_db(job,doc),SimpleNamespace(id=CAMPAIGN,tenant_id='tenant'),song,row,
        {'candidate':candidate,'review':review},execute=True)
    assert result['status']=='stale'
    assert job.transcription_quality['existing']=='preserve'
    assert job.transcription_quality[KEY]['candidate_available'] is False
    assert doc.__dict__==before


def test_complete_publication_requires_full_evidence_before_r2(monkeypatch):
    enabled(monkeypatch)
    song,candidate,review,job,doc,row=objects()
    review['audio_evidence']=review['audio_evidence'][:1]
    monkeypatch.setattr('reviewer_candidate_registry.register_candidate',lambda *a,**k:pytest.fail('unverified R2 write'))
    with pytest.raises(ValueError,match='coverage'):
        publish_song(fake_db(job,doc),SimpleNamespace(id=CAMPAIGN,tenant_id='tenant'),song,row,
            {'candidate':candidate,'review':review},execute=True)


def test_publish_keeps_candidate_viewable_when_native_proposal_conflicts(monkeypatch):
    enabled(monkeypatch)
    song,candidate,review,job,doc,row=objects();doc.quality_proposal={'native':'unchanged'}
    before=deepcopy(doc.__dict__)
    monkeypatch.setattr('reviewer_candidate_registry.register_candidate',lambda *a,**k:{'registered':True})
    monkeypatch.setattr('reviewer_batch_bridge.publish_batch_candidate',lambda *a,**k:{'published':False,'reason':'existing_proposal_preserved'})
    result=publish_song(fake_db(job,doc),SimpleNamespace(id=CAMPAIGN,tenant_id='tenant'),song,row,
        {'candidate':candidate,'review':review},execute=True)
    first=deepcopy(job.transcription_quality[KEY])
    publish_song(fake_db(job,doc),SimpleNamespace(id=CAMPAIGN,tenant_id='tenant'),song,row,
        {'candidate':candidate,'review':review},execute=True)
    assert job.transcription_quality[KEY]==first
    assert result['candidate_available'] is True
    assert status_for_job(job,doc)['blocker']=='existing_proposal_preserved'
    assert doc.__dict__==before


def test_approval_after_publication_cannot_be_overwritten(monkeypatch):
    from editor import apply_quality_proposal
    _,_,_,job,doc,_=objects()
    doc.quality_proposal={'reviewer_assist':{'campaign_id':CAMPAIGN}}
    job.approved_at='already approved'
    before=deepcopy(doc.__dict__)
    with pytest.raises(RuntimeError,match='approved_song_preserved'):
        apply_quality_proposal(fake_db(job,doc),job,doc,1,proposal_id='old',base_revision=doc.revision,
            window_ids=['old'],idempotency_key='test')
    assert doc.__dict__==before


def test_capture_outside_campaign_default_off(monkeypatch):
    from reviewer_timing_capture import timing_capture
    enabled(monkeypatch);monkeypatch.setenv('REVIEWER_TIMING_CAPTURE_ENABLED','1')
    _,_,_,job,_,_=objects();job.campaign_id='other'
    assert timing_capture([{'start':0,'end':1}],[{'start':0,'end':2}],job=job,user_id=1,
        checkpoint='manual',from_revision=1,to_revision=2) is None


def test_apply_checks_live_campaign_not_only_proposal_metadata(monkeypatch):
    from editor import apply_quality_proposal, QualityProposalsDisabled
    enabled(monkeypatch)
    _,_,_,job,doc,_=objects()
    doc.quality_proposal={'reviewer_assist':{'campaign_id':CAMPAIGN}}
    job.campaign_id='outside'
    before=deepcopy(doc.__dict__)
    with pytest.raises(QualityProposalsDisabled,match='reviewer_campaign_out_of_scope'):
        apply_quality_proposal(fake_db(job,doc),job,doc,1,proposal_id='old',base_revision=doc.revision,
            window_ids=['old'],idempotency_key='test')
    assert doc.__dict__==before
