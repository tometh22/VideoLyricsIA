import json
import sqlite3

import pytest

from reviewer_campaign import SpendLedger
from reviewer_integral import windows
from reviewer_shadow import source_binding
from scripts import complete_google_campaign_audio as module
from scripts.run_reviewer_campaign import ProviderCircuitOpen
from shadow_reference_import import digest


def fixture(tmp_path):
    jobs=[{'job_id':f'{i:012d}','audio_sha256':'a'*64,'audio_revision':1,'segments_revision':0,
           'segments_sha256':digest([]),'segments':[],'duration_seconds':24.} for i in range(300)]
    jobs[0]['job_id']='a34129dd111b'
    manifest={'campaign_id':'campaign','roster_sha256':'roster','method_sha256':'method',
        'execution_order':[s['job_id'] for s in jobs],
        'songs':[{'job_id':s['job_id'],'source':source_binding(s),'audio_path':'unused',
                  'duration_seconds':24.,'windows':windows(24.)} for s in jobs]}
    folder=tmp_path/'campaign-300';folder.mkdir()
    (folder/'manifest.json').write_text(json.dumps(manifest))
    snapshot=tmp_path/'snapshot.json'
    snapshot.write_text(json.dumps({'campaign_id':'campaign','jobs':jobs,'snapshot_sha256':digest(jobs)}))
    auth=folder/'authorization.json'
    auth.write_text(json.dumps({k:manifest[k] for k in ('campaign_id','roster_sha256','method_sha256')}|{
        'human_approval_reference':'USD20 approved','approved_usd':20,'max_attempts':100}))
    return jobs,manifest,snapshot,auth


def test_google_specs_exact_identity_unknown_skipped(tmp_path,monkeypatch):
    jobs,manifest,_,_=fixture(tmp_path);song=jobs[0];row=manifest['songs'][0]
    monkeypatch.setattr(module,'extract_clip',lambda a,w,p:p.write_bytes(b'fixture'))
    specs=list(module.google_specs(tmp_path,manifest,row,song,{'receipts':[]},[],[]))
    assert len(specs)==1 and specs[0]['provider']=='google'
    assert specs[0]['policy'].max_calls_per_song==2
    assert specs[0]['identity']==digest({'audio':{k:song[k] for k in ('job_id','audio_sha256','audio_revision')},
        'window':row['windows'][0],'provider':'google','method':'method'})
    index=[{'request':{'provider':'google','source':source_binding(song),'view':'mix',
                       'tool_status':'unknown_completion','window':row['windows'][0]}}]
    errors=[]
    assert list(module.google_specs(tmp_path,manifest,row,song,{'receipts':[]},index,errors))==[]
    assert errors==['unknown_completion_not_repeated']


def setup_runtime(tmp_path,monkeypatch):
    jobs,manifest,snapshot,auth=fixture(tmp_path)
    monkeypatch.setattr(module,'request_index',lambda *a,**k:[])
    monkeypatch.setattr(module,'project',lambda *a,**k:{'exceeds_budget':False})
    monkeypatch.setattr(module,'verify_audio',lambda *a:24.)
    monkeypatch.setattr(module,'extract_clip',lambda a,w,p:p.write_bytes(b'fixture'))
    heard=set()
    def cached(song,**kwargs):
        complete=song['job_id']!=jobs[0]['job_id'] or song['job_id'] in heard
        return {'receipts':[{'family':module.FAMILY,'start':0.,'end':24.}] if complete else []}
    monkeypatch.setattr(module,'cached_receipts',cached)
    return jobs,manifest,snapshot,auth,heard


def test_openai_hold_untouched_empty113_google_allowed_without_full_review(tmp_path,monkeypatch):
    jobs,manifest,snapshot,auth,heard=setup_runtime(tmp_path,monkeypatch)
    hold=tmp_path/'campaign-300/provider-circuit-hold.json'
    hold.write_text('{"status":"open","provider":"openai"}')
    original=hold.read_bytes()
    manifest_bytes=(tmp_path/'campaign-300/manifest.json').read_bytes()
    providers=[]
    def execute(specs,ledger,source,errors,**kwargs):
        providers.extend(s['provider'] for s in specs)
        heard.add(source['job_id'])
    monkeypatch.setattr(module,'execute_request_batches',execute)
    result=module.run(tmp_path,snapshot,auth,execute=True)
    assert providers==['google']
    assert result['counts']['google_coverage_complete']==300
    assert result['full_reviews_completed']==0 and result['openai_calls']==0
    assert result['songs'][0]['candidate_generated'] is False
    assert hold.read_bytes()==original
    assert (tmp_path/'campaign-300/manifest.json').read_bytes()==manifest_bytes


def test_google_429_stops_without_visiting_other299(tmp_path,monkeypatch):
    _,_,snapshot,auth,_=setup_runtime(tmp_path,monkeypatch)
    def execute(*args,**kwargs):
        args[1].hold_after_attempts(0)
        raise ProviderCircuitOpen([{'provider':'google','http_status':429}],0)
    monkeypatch.setattr(module,'execute_request_batches',execute)
    result=module.run(tmp_path,snapshot,auth,execute=True)
    assert result['stop_reason']=='google_http_429_circuit_open'
    assert result['songs_remaining_unvisited']==299
    assert json.loads((tmp_path/'campaign-300/google-provider-circuit-hold.json').read_text())['status']=='open'


def test_sqlite_hold_not_lifted(tmp_path,monkeypatch):
    _,_,snapshot,auth,_=setup_runtime(tmp_path,monkeypatch)
    path=tmp_path/'campaign-300/spend.sqlite'
    ledger=SpendLedger(path,approved_usd=20,max_attempts=100)
    ledger.hold_after_attempts(0);ledger.db.close()
    def execute(specs,ledger,*args,**kwargs):
        spec=next(iter(specs))
        ledger.reserve(spec['identity'],'google',24.)
    monkeypatch.setattr(module,'execute_request_batches',execute)
    result=module.run(tmp_path,snapshot,auth,execute=True)
    assert result['stop_reason']=='existing_sqlite_spend_hold'
    db=sqlite3.connect(path)
    assert db.execute('SELECT maximum FROM phase_limit').fetchone()==(0,)
    assert db.execute('SELECT count(*) FROM attempts').fetchone()==(0,)
    db.close()


@pytest.mark.parametrize('concurrency',[1,8,True])
def test_only_bounded_google_concurrency(tmp_path,concurrency):
    with pytest.raises(ValueError,match='google_concurrency'):
        module.run(tmp_path,None,None,concurrency=concurrency)
