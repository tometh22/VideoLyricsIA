from copy import deepcopy

import pytest

from reviewer_campaign import FAMILIES, SpendLedger, counters, update_status, owner_lock


def row():
    return {'source':{'job_id':'one'},'duration_seconds':10.,'status':'pending',
            'candidate_available':False,'backed_changes':0}


def receipts():
    return [{'source':{'job_id':'one'},'family':f,'tool_status':'ok','received_audio':True,
             'clock':'original_mix_decoded','start':a,'end':b,'evidence_sha256':'a'*64}
            for f in FAMILIES for a,b in [(0.,6.),(5.,10.)]]


def test_full_document_is_not_full_review():
    r=row();c={'source':r['source'],'changes':[]}
    update_status(r,[],candidate=c,reconciliation_complete=True)
    assert r['status']=='pending'
    update_status(r,receipts()[:-1],candidate=c,reconciliation_complete=True)
    assert r['status']=='partial'
    update_status(r,receipts(),candidate=c,reconciliation_complete=True)
    assert r['status']=='complete'
    assert counters({'songs':[r]})['complete_without_backed_changes']==1


@pytest.mark.parametrize('mutation',[{'source':{}},{'clock':'stem'},{'tool_status':'tool_error'},
                                   {'received_audio':False},{'end':11.},{'evidence_sha256':''}])
def test_invalid_evidence_does_not_complete(mutation):
    r=row();e=receipts();e[-1].update(mutation)
    update_status(r,e,reconciliation_complete=True,candidate={'source':r['source']})
    assert r['status']=='partial'


def test_foreign_candidate_rejected():
    with pytest.raises(ValueError,match='candidate_source'):
        update_status(row(),receipts(),candidate={'source':{}})


def test_zero_budget_never_reserves_and_unknown_completion_survives_restart(tmp_path):
    path=tmp_path/'ledger.sqlite'
    ledger=SpendLedger(path)
    assert ledger.reserve('one','openai',24)==(False,'budget_authorization_required')
    ledger.db.close()
    ledger=SpendLedger(path,approved_usd=1,max_attempts=1)
    assert ledger.reserve('one','google',24)==(True,None)
    ledger.db.close()
    restarted=SpendLedger(path,approved_usd=1,max_attempts=1)
    assert restarted.reserve('one','google',24)==(False,'reserved_unknown_completion')
    assert restarted.reserve('two','google',24)==(False,'budget_authorization_required')
    assert restarted.totals()['attempts']==1


def test_owner_lock_is_exclusive(tmp_path):
    with owner_lock(tmp_path):
        with pytest.raises(RuntimeError,match='another_campaign_runner'):
            with owner_lock(tmp_path):
                pass


def test_unsupported_billing_rejected(tmp_path):
    ledger=SpendLedger(tmp_path/'ledger.sqlite',approved_usd=1,max_attempts=10)
    for provider,duration in [('new-provider',24),('google',25),('openai',float('nan'))]:
        with pytest.raises(ValueError):
            ledger.reserve('x',provider,duration)


def test_inflight_reserved_then_priced_usage_releases_only_known_difference(tmp_path):
    ledger=SpendLedger(tmp_path/'ledger.sqlite',approved_usd=.02,max_attempts=3)
    assert ledger.reserve('first','google',24)[0]
    assert not ledger.reserve('second','google',24)[0]
    ledger.finish('first','ok','result',request={'provider':'google','received_audio':True,
        'usage':{'prompt_token_count':748,'candidates_token_count':250,'thoughts_token_count':0}})
    assert ledger.totals()['priced_observed_usage_usd']==pytest.approx(.001373)
    assert ledger.totals()['unsettled_reservations_usd']==0
    assert ledger.reserve('second','google',24)[0]
    ledger.finish('second','tool_error','result',request={'provider':'google','received_audio':False})
    assert ledger.totals()['unsettled_reservations_usd']>0
    assert not ledger.reserve('third','google',24)[0]
