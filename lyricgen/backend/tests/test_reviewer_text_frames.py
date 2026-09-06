from types import SimpleNamespace
import numpy as np
from reviewer_shadow import select_endpoint
from reviewer_correspondence import correspond
from reviewer_ctc_frames import last_token_profile
from reviewer_timing_capture import timing_capture


def candidate(**kw):
    return dict(end_seconds=3., clock_source='acoustic_tool', tool_status='ok',
        target_voice_verified=True, phonetic_end_supported=True,
        clock='original_mix_decoded', source_clock_verified=True,
        cross_signal_timestamp_transfer=False, **kw)


def test_native_mix_does_not_require_stem_sync():
    row={'start':1.,'end':2.}
    assert select_endpoint(row,[candidate()],next_start=4.,duration=5.)['decision']=='propose'
    bad=candidate();bad['cross_signal_timestamp_transfer']=True
    assert select_endpoint(row,[bad],next_start=4.,duration=5.)['decision']=='abstain'


def test_extension_cannot_shorten_and_reduction_has_own_evidence():
    row={'start':1.,'end':3.5}
    assert select_endpoint(row,[candidate(repair_intent='extend')],next_start=4.,duration=5.)['reason']=='extension_would_shorten_baseline'
    assert select_endpoint(row,[candidate()],next_start=4.,duration=5.)['reason']=='reduction_requires_own_evidence'
    assert select_endpoint(row,[candidate(repair_intent='reduce',reduction_evidence_verified=True)],next_start=4.,duration=5.)['decision']=='propose'


def test_sequence_prefers_corresponding_substitution_to_artificial_prefix_deletion():
    request={'tool_status':'ok','response':{'words':[
        {'word':w,'start':i,'end':i+.9} for i,w in enumerate(['posa','sobre','el','delante'])]}}
    result=correspond({'text':'Posa sobre el delantal','start':0.,'end':4.},request,{'start':0,'end':5})
    assert result['candidates'][0]['heard_tokens']==['posa','sobre','el','delante']
    assert not result['certified']


def test_frame_profile_accounts_for_alternative_ctc_paths():
    # 0 blank,1 star,2 grapheme: last grapheme can leave on different frames.
    em=np.log(np.array([[.1,.8,.1],[.1,.1,.8],[.7,.1,.2],[.1,.8,.1],[.8,.1,.1]]))
    profile=last_token_profile(em,[1,2,1],0)
    assert max(x for x in profile if x is not None)==0
    assert sum(x is not None for x in profile)>=2


def test_prospective_capture_default_off_and_machine_apply_excluded(monkeypatch):
    old=[{'start':1.,'end':2.,'text':'hola'}]; new=[{'start':1.,'end':2.7,'text':'hola'}]
    args=dict(job=SimpleNamespace(job_id='j',tenant_id='t',campaign_id='fixture00001',input_audio_sha256='a'*64,audio_revision=1),
        user_id=2,checkpoint='draft',from_revision=4,to_revision=5)
    monkeypatch.delenv('REVIEWER_TIMING_CAPTURE_ENABLED',raising=False)
    assert timing_capture(old,new,**args) is None
    monkeypatch.setenv('REVIEWER_TIMING_CAPTURE_ENABLED','1')
    monkeypatch.setenv('REVIEWER_ASSIST_CAMPAIGN_ID','fixture00001')
    evidence=timing_capture(old,new,**args)
    assert evidence['changed'][0]['end_delta']==.7 or abs(evidence['changed'][0]['end_delta']-.7)<1e-9
    assert evidence['audio_sha256']=='a'*64 and not evidence['clean_gold']
    assert evidence['changed'][0]['association']=='position_only_unverified'
    assert timing_capture(old,new,**{**args,'checkpoint':'reviewer_candidate'}) is None


def test_normal_editor_save_captures_once_without_approval(monkeypatch):
    from tests.test_editor_documents import _users_and_job
    from database import SessionLocal, Job, EditorDocument, AuditLog, BatchCampaign
    import uuid
    from editor import save_document
    first,_,job_id=_users_and_job('prospective_timing_test')
    monkeypatch.setenv('REVIEWER_TIMING_CAPTURE_ENABLED','1')
    campaign_id=uuid.uuid4().hex[:12]
    monkeypatch.setenv('REVIEWER_ASSIST_CAMPAIGN_ID',campaign_id)
    with SessionLocal() as db:
        job=db.query(Job).filter_by(job_id=job_id).one()
        db.add(BatchCampaign(id=campaign_id,tenant_id=job.tenant_id,created_by=first.id,name='Timing fixture'))
        db.flush()
        job.campaign_id=campaign_id
        job.input_audio_sha256='a'*64
        db.flush()
        doc=db.query(EditorDocument).filter_by(job_id=job_id).one()
        revised=[{**r} for r in doc.current_segments]
        revised[0]['end']=.8
        doc,_,changed=save_document(db,job=job,document=doc,user_id=first.id,
            base_revision=doc.revision,segments=revised,reason='draft')
        db.flush()
        assert changed and job.approved_at is None
        evidence=db.query(AuditLog).filter_by(action='lyrics.prospective_timing',user_id=first.id).one()
        assert evidence.detail['changed'][0]['baseline']['end']==1
        assert evidence.detail['changed'][0]['human_submitted']['end']==.8
        assert evidence.detail['audio_sha256']=='a'*64
        save_document(db,job=job,document=doc,user_id=first.id,
            base_revision=doc.revision,segments=revised,reason='draft')
        assert db.query(AuditLog).filter_by(action='lyrics.prospective_timing',user_id=first.id).count()==1


def test_offline_replay_rejects_changed_audio_before_inference(tmp_path,monkeypatch):
    import json
    import pytest
    from scripts import reviewer_text_and_frames as replay
    folder=tmp_path/'integral-v2';folder.mkdir()
    (folder/'input.json').write_text(json.dumps({'audio_sha256':'expected'}))
    (folder/'predictions-frozen.json').write_text(json.dumps({'source':{'audio_sha256':'expected'}}))
    monkeypatch.setattr(replay,'file_sha',lambda _: 'changed')
    for phase in [replay.text_run,replay.model_run]:
        with pytest.raises(ValueError,match='stale_source_audio'):
            phase(tmp_path,tmp_path/'output')
