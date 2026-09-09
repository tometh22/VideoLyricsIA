from copy import deepcopy
import json
from pathlib import Path
import sqlite3
import threading

import pytest

from reviewer_campaign import SpendLedger
from reviewer_shadow import ShadowPolicy
from scripts import run_reviewer_campaign as runner


def test_429_drains_batch_persists_hold_and_never_consumes_next(tmp_path):
    ledger=SpendLedger(tmp_path/'spend.sqlite',approved_usd=20,max_attempts=100)
    generated=[]
    barrier=threading.Barrier(4)
    def specifications():
        for i in range(8):
            generated.append(i)
            yield {'identity':str(i),'provider':'openai','window':{'start':0.,'end':24.},
                   'clip':Path(str(i)),'folder':tmp_path,'policy':ShadowPolicy()}
    class Listener:
        def __init__(self,*a,**k):pass
        def listen(self,clip,**kwargs):
            barrier.wait(timeout=5)
            return {'tool_status':'tool_error' if str(clip)=='0' else 'ok',
                    'http_status':429 if str(clip)=='0' else None,
                    'provider':'openai','model':'whisper-1'}
    errors=[]
    with pytest.raises(runner.ProviderCircuitOpen) as caught:
        runner.execute_request_batches(specifications(),ledger,{},errors,concurrency=4,listener_factory=Listener)
    assert generated==[0,1,2,3]
    assert ledger.db.execute('SELECT count(*) FROM attempts WHERE status IN (?,?)',('ok','tool_error')).fetchone()[0]==4
    assert caught.value.receipt['in_flight_batch_drained'] is True
    assert caught.value.receipt['attempts']==4
    with pytest.raises(sqlite3.IntegrityError,match='first_ten_inspection_hold'):
        ledger.reserve('must-not-start','openai',24.)
    ledger.db.close()


def test_provider_circuit_stops_run_preserves_unvisited_rows(tmp_path,monkeypatch):
    jobs=[{'job_id':name} for name in ('one','two','three')]
    (tmp_path/'snapshot.json').write_text(json.dumps({'jobs':jobs}))
    (tmp_path/'sample.json').write_text('{}')
    (tmp_path/'import-reconciled.json').write_text('{"rows":[]}')
    initial={'campaign_id':'campaign','roster_sha256':'roster','method_sha256':'method',
        'first_ten':['one'],'execution_order':['one','two','three'],
        'songs':[{'job_id':j['job_id'],'source':{'job_id':j['job_id']},'status':'pending',
            'duration_seconds':10.,'candidate_available':False,'backed_changes':0,'first_ten':True,
            'reconciliation_complete':False} for j in jobs]}
    monkeypatch.setattr(runner,'create_manifest',lambda *a:deepcopy(initial))
    monkeypatch.setattr(runner,'request_index',lambda *a:[])
    monkeypatch.setattr(runner,'cached_receipts',lambda *a,**k:{'receipts':[]})
    monkeypatch.setattr(runner.subprocess,'check_output',lambda *a,**k:'commit')
    visited=[]
    def process(*args,**kwargs):
        visited.append(args[4]['job_id'])
        raise runner.ProviderCircuitOpen([{'provider':'openai','http_status':429}],0)
    monkeypatch.setattr(runner,'process_song',process)
    runner.run(tmp_path,tmp_path/'snapshot.json')
    result=json.loads((tmp_path/'campaign-300/manifest.json').read_text())
    assert visited==['one']
    assert result['songs'][1:]==initial['songs'][1:]
    assert result['songs'][0]['blocker']=='provider_http_429_circuit_open'
    assert result['counts']['pending']==2
    assert json.loads((tmp_path/'campaign-300/provider-circuit-hold.json').read_text())['status']=='open'
