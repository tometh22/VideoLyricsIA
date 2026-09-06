from copy import deepcopy
import pytest
from scripts import watch_reviewer_campaign_budget as monitor


def records(seconds,cost):
    return [{'request':{'provider':p,'window':{'start':0.,'end':seconds}},'cost':cost}
            for p in ('openai','google')]


def data():
    return {'songs':[{'source':{'job_id':'a'},'duration_seconds':24.,'status':'partial',
        'windows':[{'start':0.,'end':24.}],
        'missing_audio_windows':[{'family':f,'window':{'start':0.,'end':24.}}
            for f in monitor.FAMILIES]}]}


def test_projection_normalizes_audio_seconds_not_short_call_count(monkeypatch):
    monkeypatch.setattr(monitor,'cached_receipts',lambda *a,**k:{'records':records(2,.002)+records(24,.024)})
    monkeypatch.setattr(monitor,'usage_estimate',lambda r:r[0]['cost'])
    result=monitor.project(data(),[],{'priced_observed_usage_usd':1.,
        'unsettled_reservations_usd':.2,'attempts':4},approved_usd=20)
    assert result['usd_per_submitted_second']=={'openai':.001,'google':.001}
    assert result['projected_remaining_usd']==pytest.approx(48*.001*1.2)
    assert result['remaining_authorized_usd']==18.8
    assert not result['exceeds_budget']


def test_projection_or_accounting_breach_holds(monkeypatch):
    monkeypatch.setattr(monitor,'cached_receipts',lambda *a,**k:{'records':records(24,12)})
    monkeypatch.setattr(monitor,'usage_estimate',lambda r:r[0]['cost'])
    totals={'priced_observed_usage_usd':1.,'unsettled_reservations_usd':0.,'attempts':2}
    assert monitor.project(data(),[],totals,approved_usd=20)['exceeds_budget']
    monkeypatch.setattr(monitor,'cached_receipts',lambda *a,**k:{'records':[]})
    totals['priced_observed_usage_usd']=21
    result=monitor.project(data(),[],totals,approved_usd=20)
    assert result['exceeds_budget'] and result['projected_remaining_usd'] is None


def test_monitor_refuses_non20_authority(tmp_path,monkeypatch):
    folder=tmp_path/'campaign-300';folder.mkdir();(folder/'manifest.json').write_text('{}')
    monkeypatch.setattr(monitor,'authorization',lambda *a:{'approved_usd':65})
    with pytest.raises(ValueError,match='exact_user_authorized_usd20'):
        monitor.tick(tmp_path,tmp_path/'authority')


def test_breach_writes_durable_hold_without_cancelling_inflight(tmp_path,monkeypatch):
    folder=tmp_path/'campaign-300';folder.mkdir();(folder/'manifest.json').write_text('{}')
    monkeypatch.setattr(monitor,'authorization',lambda *a:{'approved_usd':20,'max_attempts':9004})
    calls=[]
    class Ledger:
        db=type('DB',(),{'close':lambda self:calls.append('close')})()
        def __init__(self,*a,**k):pass
        def totals(self):return {}
        def hold_after_attempts(self,n):calls.append(('hold',n))
    monkeypatch.setattr(monitor,'SpendLedger',Ledger)
    monkeypatch.setattr(monitor,'request_index',lambda *a,**k:[])
    monkeypatch.setattr(monitor,'project',lambda *a,**k:{'exceeds_budget':True,'attempts':100})
    result=monitor.tick(tmp_path,tmp_path/'authority')
    assert calls==[('hold',100),'close']
    assert result['new_reservations_held']
    assert (folder/'budget-projection-hold.json').exists()


def test_runner_lifetime_guard_stops_before_song_provider_work(tmp_path,monkeypatch):
    from scripts import run_reviewer_campaign as runner
    (tmp_path/'snapshot.json').write_text('{"jobs":[{"job_id":"a"}]}')
    (tmp_path/'sample.json').write_text('{}')
    (tmp_path/'import-reconciled.json').write_text('{"rows":[]}')
    m=data();m.update(execution_order=['a'],first_ten=['a'])
    m['songs'][0].update(job_id='a',candidate_available=False,backed_changes=0)
    monkeypatch.setattr(runner,'create_manifest',lambda *a:m)
    monkeypatch.setattr(runner,'authorization',lambda *a:{'approved_usd':20,'max_attempts':9004})
    monkeypatch.setattr(runner,'request_index',lambda *a:[])
    monkeypatch.setattr(runner.subprocess,'check_output',lambda *a,**k:'commit')
    monkeypatch.setattr(runner,'process_song',lambda *a,**k:pytest.fail('must not run provider work'))
    monkeypatch.setattr(monitor,'project',lambda *a,**k:{'exceeds_budget':True,'attempts':0})
    runner.run(tmp_path,tmp_path/'snapshot.json',authorization_path=tmp_path/'authority')
    assert (tmp_path/'campaign-300/budget-projection-hold.json').exists()
    from reviewer_campaign import SpendLedger
    ledger=SpendLedger(tmp_path/'campaign-300/spend.sqlite',approved_usd=20,max_attempts=9004)
    import sqlite3
    with pytest.raises(sqlite3.IntegrityError,match='first_ten_inspection_hold'):
        ledger.reserve('new','openai',24)


def test_local_only_preserves_authority_and_hold_without_projection_or_calls(tmp_path,monkeypatch):
    import json
    import sqlite3
    from reviewer_campaign import SpendLedger
    from scripts import run_reviewer_campaign as runner
    folder=tmp_path/'campaign-300';folder.mkdir()
    ledger=SpendLedger(folder/'spend.sqlite',approved_usd=20,max_attempts=9004)
    ledger.hold_after_attempts(0);ledger.db.close()
    (tmp_path/'snapshot.json').write_text('{"jobs":[{"job_id":"a"}]}')
    (tmp_path/'sample.json').write_text('{}');(tmp_path/'import-reconciled.json').write_text('{"rows":[]}')
    m=data();m.update(execution_order=['a'],first_ten=['a'],campaign_id='c',roster_sha256='r',method_sha256='m')
    m['songs'][0].update(job_id='a',candidate_available=False,backed_changes=0,first_ten=True)
    auth={'approved_usd':20,'max_attempts':9004}
    monkeypatch.setattr(runner,'create_manifest',lambda *a:m)
    monkeypatch.setattr(runner,'authorization',lambda *a:auth)
    monkeypatch.setattr(runner,'request_index',lambda *a:[])
    monkeypatch.setattr(runner.subprocess,'check_output',lambda *a,**k:'commit')
    visited=[]
    monkeypatch.setattr(runner,'process_song',lambda *a,**k:visited.append(k['paid_allowed']))
    monkeypatch.setattr(monitor,'project',lambda *a,**k:pytest.fail('local work does not require budget projection'))
    runner.run(tmp_path,tmp_path/'snapshot.json',authorization_path=tmp_path/'authority',local_only=True)
    assert visited==[False]
    assert json.loads((folder/'manifest.json').read_text())['authorization']==auth
    ledger=SpendLedger(folder/'spend.sqlite',approved_usd=20,max_attempts=9004)
    with pytest.raises(sqlite3.IntegrityError):ledger.reserve('new','openai',24)
