from dataclasses import asdict
import json

import pytest

from reviewer_campaign import SpendLedger
from reviewer_shadow import ShadowPolicy, source_binding
from reviewer_shadow_audio import file_sha
from scripts import recover_whisper_quota as recovery
from shadow_reference_import import digest


def fixture(tmp_path):
    song = {"job_id":"test12345678", "audio_sha256":"a"*64, "audio_revision":1,
            "segments_revision":0, "segments_sha256":"b"*64, "duration_seconds":24.}
    window = {"start":0., "end":24., "offset_seconds":0.}
    folder=tmp_path/'campaign-300'/song['job_id'];(folder/'requests').mkdir(parents=True)
    clip=folder/(digest(window)+'.wav');clip.write_bytes(b'cached exact clip')
    request={"source":source_binding(song),"window":window,"provider":"openai","model":"whisper-1",
        "family":"openai/whisper-1","view":"mix","conditioning_texts":[],"prompt_version":"no-prompt-v1",
        "tool_status":"tool_error","http_status":429,"received_audio":False,"error_type":"HTTPError",
        "policy":asdict(ShadowPolicy()),"clip_sha256":file_sha(clip)}
    path=folder/'requests'/'failed.json';path.write_text(json.dumps(request))
    case={"identity":"retry-one","original_identity":"original","job_id":song['job_id'],
          "window":window,"failed_path":str(path),"failed_evidence_sha256":file_sha(path)}
    return song,{"windows":[window],"audio_path":"unused"},request,case


@pytest.mark.parametrize('patch',[{'http_status':500},{'tool_status':'unknown_completion'},
    {'tool_status':'ok'},{'received_audio':True},{'provider':'google'}, {'error_type':'ConnectionError'},
    {'family':'another-family'}, {'error_type':'UnknownError'}])
def test_only_known_429_not_unknown_or_other_tools(tmp_path,patch):
    song,row,request,_=fixture(tmp_path)
    assert recovery.known_quota_rejection(request,song,row['windows'])
    assert not recovery.known_quota_rejection({**request,**patch},song,row['windows'])


def test_exact_source_and_window(tmp_path):
    song,row,request,_=fixture(tmp_path)
    assert not recovery.known_quota_rejection(request,{**song,'audio_revision':2},row['windows'])
    assert not recovery.known_quota_rejection(request,{**song,'segments_revision':2},row['windows'])
    assert not recovery.known_quota_rejection(request,song,[{'start':1.,'end':24.,'offset_seconds':1.}])


def test_retry_reserves_again_preserves_original_and_never_repeats(tmp_path):
    song,row,request,case=fixture(tmp_path)
    ledger=SpendLedger(tmp_path/'ledger.sqlite',approved_usd=20,max_attempts=100)
    ledger.reserve('original','openai',24.)
    ledger.finish('original','tool_error',tmp_path,request=request)
    calls=[]
    class Listener:
        def __init__(self,path,*,policy):
            assert path.name=='requests' and path.parent.name=='quota-retry-1'
            assert asdict(policy)==request['policy']
        def listen(self,clip,**kwargs):
            calls.append(kwargs)
            assert kwargs['source']==request['source'] and kwargs['window']==request['window']
            return {**request,'tool_status':'ok','received_audio':True,'http_status':None,'response':{'words':[]}}
    first=recovery.attempt_one(case,song,row,ledger,tmp_path,listener_factory=Listener)
    assert first['usable_success']
    second=recovery.attempt_one(case,song,row,ledger,tmp_path,listener_factory=Listener)
    assert second=={'status':'not_repeated','reason':'ok'}
    assert len(calls)==1
    assert ledger.db.execute('SELECT status FROM attempts WHERE id="original"').fetchone()==('tool_error',)
    assert ledger.totals()['attempts']==2
    assert ledger.totals()['unsettled_reservations_usd']>0
    ledger.db.close()


def test_missing_restoration_is_not_authority(tmp_path):
    manifest={'campaign_id':'c','roster_sha256':'r','method_sha256':'m'}
    with pytest.raises(ValueError,match='restoration_receipt'):
        recovery.restoration_authorization(None,manifest)
    path=tmp_path/'restore.json'
    receipt={'schema':'openai-whisper-credit-restoration-v1','provider':'openai','model':'whisper-1',
             'restoration_confirmed':True,'human_approval_reference':'actual-human-confirmation',**manifest}
    path.write_text(json.dumps(receipt))
    assert recovery.restoration_authorization(path,manifest)==receipt
    path.write_text(json.dumps({**receipt,'restoration_confirmed':False}))
    with pytest.raises(ValueError):recovery.restoration_authorization(path,manifest)


def test_success_coverage_never_repurchased(tmp_path,monkeypatch):
    song,row,request,case=fixture(tmp_path)
    manifest={'songs':[{'job_id':song['job_id'],**row}],'method_sha256':'m','execution_order':[song['job_id']]}
    entry={'request':request,'cache_path':case['failed_path'],'evidence_sha256':case['failed_evidence_sha256']}
    assert len(recovery.recovery_plan(tmp_path,manifest,{song['job_id']:song},[entry]))==1
    monkeypatch.setattr(recovery,'cached_receipts',lambda *a,**k:{'receipts':[{
        'family':'openai/whisper-1','start':0.,'end':24.}]})
    assert recovery.recovery_plan(tmp_path,manifest,{song['job_id']:song},[entry])==[]


@pytest.mark.parametrize('first_success',[False,True])
def test_run_validates_first_before_expansion_and_reholds_on_429(tmp_path,monkeypatch,first_success):
    base,_,_,_=fixture(tmp_path)
    jobs=[{**base,'job_id':f'{i:012d}'} for i in range(300)]
    manifest={'campaign_id':'campaign','roster_sha256':'roster','method_sha256':'method',
        'songs':[{'job_id':s['job_id'],'source':source_binding(s),'audio_path':'unused'} for s in jobs]}
    folder=tmp_path/'campaign-300'
    (folder/'manifest.json').write_text(json.dumps(manifest))
    snapshot=tmp_path/'snapshot.json'
    snapshot.write_text(json.dumps({'jobs':jobs,'snapshot_sha256':digest(jobs)}))
    auth=tmp_path/'authorization.json'
    auth.write_text(json.dumps({k:manifest[k] for k in ('campaign_id','roster_sha256','method_sha256')} | {
        'human_approval_reference':'original USD20 approval','approved_usd':20,'max_attempts':100}))
    restored=tmp_path/'restoration.json'
    restored.write_text(json.dumps({k:manifest[k] for k in ('campaign_id','roster_sha256','method_sha256')} | {
        'schema':'openai-whisper-credit-restoration-v1','provider':'openai','model':'whisper-1',
        'restoration_confirmed':True,'human_approval_reference':'credit restored explicitly'}))
    monkeypatch.setattr(recovery,'request_index',lambda *a,**k:[])
    monkeypatch.setattr(recovery,'recovery_plan',lambda *a:[{'job_id':jobs[i]['job_id']} for i in range(2)])
    monkeypatch.setattr(recovery,'verify_audio',lambda *a:24.)
    monkeypatch.setattr(recovery,'project',lambda *a,**k:{'exceeds_budget':False})
    calls=[]
    def attempt(*args,**kwargs):
        calls.append(args[0]['job_id'])
        if len(calls)==2:
            saved=json.loads((folder/'whisper-quota-recovery.json').read_text())
            assert saved['first_attempt_validated_before_expansion'] is True
        if first_success and len(calls)==1:return {'status':'ok','usable_success':True}
        return {'status':'tool_error','http_status':429,'usable_success':False}
    monkeypatch.setattr(recovery,'attempt_one',attempt)
    before=(folder/'manifest.json').read_bytes()
    report=recovery.run(tmp_path,snapshot,auth,restored,execute=True)
    assert len(calls)==(2 if first_success else 1)
    assert report['stop_reason']=='quota_retry_not_successful_no_expansion'
    assert (folder/'manifest.json').read_bytes()==before
    ledger=SpendLedger(folder/'spend.sqlite',approved_usd=20,max_attempts=100)
    assert ledger.db.execute('SELECT maximum FROM phase_limit').fetchone()==(0,)
    ledger.db.close()
