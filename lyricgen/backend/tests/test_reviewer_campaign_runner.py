from copy import deepcopy
import json
from types import SimpleNamespace

import pytest

from scripts import run_reviewer_campaign as runner


def test_recovery_union_covers_window_but_gaps_do_not():
    w={'start':0.,'end':24.}
    assert runner.covered([{'family':'g','start':0.,'end':13.},
                           {'family':'g','start':11.,'end':24.}],'g',w)
    assert not runner.covered([{'family':'g','start':0.,'end':11.},
                               {'family':'g','start':13.,'end':24.}],'g',w)


def test_empty_baseline_cannot_be_a_completed_candidate():
    with pytest.raises(ValueError,match='empty_transcription_baseline'):
        runner.process_song(None,None,None,None,{'job_id':'empty','segments':[]},
                            None,None,None,None,paid_allowed=False)


def test_cached_reconciliation_does_not_preserve_transient_blocker(tmp_path, monkeypatch):
    from reviewer_shadow import source_binding
    from shadow_reference_import import digest
    song={'job_id':'cached','audio_sha256':'a'*64,'audio_revision':1,
          'segments_revision':0,'segments_sha256':'b'*64,'duration_seconds':6.,
          'segments':[{'text':'Una frase','start':1.,'end':4.}]}
    source=source_binding(song)
    receipts=[{'source':source,'clock':'original_mix_decoded','start':0.,'end':6.,
               'evidence_sha256':str(i)*64,'family':family,'tool_status':'ok','received_audio':True}
              for i,family in enumerate(('openai/whisper-1','google/gemini-2.5-flash-audio'))]
    cached={'records':[{}],'receipts':receipts}
    key=digest({'source':source,'method':'method','selector_revision':runner.SELECTOR_REVISION,
                'reference':None,'receipts':[r['evidence_sha256'] for r in receipts]})
    row={'job_id':'cached','source':source,'duration_seconds':6.,'windows':[],
         'reconciliation_key':key,'reconciliation_complete':True,'status':'blocked',
         'blocker':'audio_tool_invalid_response','failure_code':'IntegrityError'}
    folder=tmp_path/'cached';folder.mkdir()
    candidate={'source':source,'changes':[],'residual_qc':{}}
    (folder/'candidate.json').write_text(json.dumps(candidate))
    (folder/'review.json').write_text('{}')
    monkeypatch.setattr(runner,'cached_receipts',lambda *a,**k:cached)
    monkeypatch.setattr(runner,'usage_estimate',lambda *a:0.)
    monkeypatch.setattr(runner,'reconcile',lambda *a,**k:(deepcopy(candidate),{}))
    monkeypatch.setattr(runner,'prepare_batch_candidate',lambda *a,**k:{})
    runner.process_song(tmp_path,tmp_path,{'method_sha256':'method'},row,song,[],[],None,
                        'commit',paid_allowed=False)
    assert row['status']=='complete' and row['blocker'] is None
    assert 'failure_code' not in row


def manifest():
    return {'campaign_id':'campaign','roster_sha256':'roster','method_sha256':'method',
        'snapshot_sha256':'snapshot','first_ten':['one'],'execution_order':['one','two'],
        'authorization':{'approved_usd':20},
        'songs':[{'job_id':j,'source':{'job_id':j,'segments_revision':0},
            'duration_seconds':10.,'protected_lines':0,'snapshot_status':'lyrics_ready',
            'first_ten':j=='one','status':'complete','candidate_available':True,
            'backed_changes':0,'reconciliation_complete':True,
            'observed_usage_requests':2,'usage_estimate_complete':True,
            'observed_usage_cost_usd':.01} for j in ('one','two')]}


def test_per_song_refresh_preserves_unaffected_results():
    old=manifest();old['songs'][1]['reconciliation_key']='keep'
    fresh=deepcopy(old);fresh['snapshot_sha256']='changed'
    fresh['songs'][0]['source']['segments_revision']=1
    fresh['songs'][0].update(status='pending',candidate_available=False)
    new=runner.refresh_manifest(old,fresh)
    assert new['songs'][0]['invalidated_previous_source']['segments_revision']==0
    assert new['songs'][0]['status']=='pending'
    assert new['songs'][1]['reconciliation_key']=='keep'
    assert new['snapshot_sha256']=='changed'


@pytest.mark.parametrize('key',['campaign_id','roster_sha256','method_sha256'])
def test_frozen_identity_change_fails(key):
    fresh=manifest();fresh[key]='changed'
    with pytest.raises(ValueError):runner.refresh_manifest(manifest(),fresh)


def test_first_ten_stale_success_never_authorizes_expansion(tmp_path):
    m=manifest();path=tmp_path/'check.json'
    check=runner.first_ten_checkpoint(m,SimpleNamespace(totals=lambda:{}))
    path.write_text(json.dumps(check));assert runner.expansion_allowed(path,m)
    m['songs'][0]['source']['segments_revision']=1
    assert not runner.expansion_allowed(path,m)
    m=manifest();m['songs'][0]['status']='blocked'
    assert not runner.expansion_allowed(path,m)
    path.write_text('{broken');assert not runner.expansion_allowed(path,m)


def test_authorization_is_explicit_and_read_only(tmp_path):
    path=tmp_path/'missing.json'
    assert runner.authorization(None,manifest())['approved_usd']==0
    with pytest.raises(FileNotFoundError):runner.authorization(path,manifest())
    assert not path.exists()
    auth={k:manifest()[k] for k in ('campaign_id','roster_sha256','method_sha256')}
    auth.update(human_approval_reference='human-message-id',approved_usd=1,max_attempts=10)
    path.write_text(json.dumps(auth));assert runner.authorization(path,manifest())==auth
    for patch in ({'approved_usd':True},{'approved_usd':float('nan')},
                  {'max_attempts':True},{'roster_sha256':'foreign'},
                  {'human_approval_reference':''}):
        path.write_text(json.dumps({**auth,**patch}))
        with pytest.raises(ValueError):runner.authorization(path,manifest())


def test_complete_first_ten_does_not_expand_over_budget():
    m=manifest();m['songs'][1]['windows']=[{}]*10000
    check=runner.first_ten_checkpoint(m,SimpleNamespace(totals=lambda:{'reserved_upper_bound_usd':1}))
    assert check['all_ten_complete']
    assert not check['automatic_expansion_allowed']
    assert check['projected_remaining_usd_with_20pct_reserve']==120


def test_decoder_duration_checked_before_spending(tmp_path,monkeypatch):
    audio=tmp_path/'audio.wav';audio.write_bytes(b'fake')
    song={'audio_sha256':'hash','duration_seconds':10.}
    monkeypatch.setattr(runner,'file_sha',lambda _: 'hash')
    monkeypatch.setattr(runner.subprocess,'run',lambda *a,**k:SimpleNamespace(stdout='out_time_us=9500000\n'))
    with pytest.raises(ValueError,match='decoded_audio_duration_mismatch'):
        runner.verify_audio(audio,song)
    monkeypatch.setattr(runner.subprocess,'run',lambda *a,**k:SimpleNamespace(stdout='out_time_us=10000000\n'))
    assert runner.verify_audio(audio,song)==10.


def test_last_window_rounding_does_not_repurchase():
    assert runner.covered([{'family':'f','start':18.,'end':23.1234567}],
                          'f',{'start':18.,'end':23.123457})
    assert not runner.covered([{'family':'f','start':18.,'end':23.12}],
                              'f',{'start':18.,'end':23.123457})


def test_unknown_attempt_reused_across_document_revision():
    song={'job_id':'one','audio_sha256':'h','audio_revision':1,'segments_revision':3}
    window={'start':0.,'end':10.}
    request={'provider':'google','view':'mix','source':{**song,'segments_revision':0},
        'window':window,'tool_status':'unknown_completion'}
    assert runner.previous_unknown([{'request':request}],song,'google',window)
    assert not runner.previous_unknown([{'request':request}],{**song,'audio_revision':2},'google',window)


def test_individual_song_failure_continues_wave(tmp_path,monkeypatch):
    (tmp_path/'snapshot.json').write_text(json.dumps({'jobs':[{'job_id':'one'},{'job_id':'two'}]}))
    (tmp_path/'sample.json').write_text('{}')
    (tmp_path/'import-reconciled.json').write_text('{"rows":[]}')
    monkeypatch.setattr(runner,'create_manifest',lambda *a:manifest())
    monkeypatch.setattr(runner,'request_index',lambda *a:[])
    monkeypatch.setattr(runner.subprocess,'check_output',lambda *a,**k:'commit')
    visited=[]
    def process(*args,**kwargs):
        visited.append(args[4]['job_id'])
        assert kwargs['paid_allowed'] is False
        if args[4]['job_id']=='one':raise RuntimeError('provider secret must not leak')
    monkeypatch.setattr(runner,'process_song',process)
    runner.run(tmp_path,tmp_path/'snapshot.json')
    result=json.loads((tmp_path/'campaign-300/manifest.json').read_text())
    assert visited==['one','two']
    assert result['songs'][0]['status']=='blocked'
    assert 'secret' not in json.dumps(result)
    assert result['spend']['attempts']==0
    assert not json.loads((tmp_path/'campaign-300/first-ten-check.json').read_text())['automatic_expansion_allowed']
