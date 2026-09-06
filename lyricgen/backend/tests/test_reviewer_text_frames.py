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
    args=dict(job=SimpleNamespace(job_id='j',tenant_id='t',input_audio_sha256='a'*64,audio_revision=1),
        user_id=2,checkpoint='draft',from_revision=4,to_revision=5)
    monkeypatch.delenv('REVIEWER_TIMING_CAPTURE_ENABLED',raising=False)
    assert timing_capture(old,new,**args) is None
    monkeypatch.setenv('REVIEWER_TIMING_CAPTURE_ENABLED','1')
    evidence=timing_capture(old,new,**args)
    assert evidence['changed'][0]['end_delta']==.7 or abs(evidence['changed'][0]['end_delta']-.7)<1e-9
    assert evidence['audio_sha256']=='a'*64 and not evidence['clean_gold']
    assert evidence['changed'][0]['association']=='position_only_unverified'
    assert timing_capture(old,new,**{**args,'checkpoint':'reviewer_candidate'}) is None
