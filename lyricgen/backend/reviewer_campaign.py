"""Durable exact-roster review accounting. No product/database writes.

One local owner may run this ledger. An OS lock prevents competing runners;
attempt reservations survive crashes, so unknown completion is never retried.
"""
from collections import Counter
from contextlib import contextmanager
import fcntl
import json
import math
import os
from pathlib import Path
import sqlite3
from reviewer_integral import windows, union_seconds
from reviewer_shadow import source_binding, validate_snapshot
from shadow_reference_import import digest

FAMILIES = ['openai/whisper-1', 'google/gemini-2.5-flash-audio']
METHOD = {'version':'campaign-full-review-v1','window_seconds':24,'overlap_seconds':6,
          'providers':['openai','google'],'clock':'original_mix_decoded',
          'timing_policy':'baseline_preserved_doubts_localized',
          'tokenization_policy':'hold_for_editorial_review','auto_apply':False}


def atomic_json(path, value):
    path=Path(path);path.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
    temp=path.with_suffix(path.suffix+'.tmp')
    with temp.open('w') as f:
        os.chmod(temp,0o600);json.dump(value,f,ensure_ascii=False,sort_keys=True)
        f.flush();os.fsync(f.fileno())
    os.replace(temp,path)


@contextmanager
def owner_lock(folder):
    folder=Path(folder);folder.mkdir(mode=0o700,parents=True,exist_ok=True)
    with (folder/'runner.lock').open('a') as f:
        try: fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError: raise RuntimeError('another_campaign_runner_active') from None
        yield


def create_manifest(snapshot, sample, audio_root, *, expected_count=300):
    jobs=snapshot['jobs']; ids=[j['job_id'] for j in jobs]
    if len(ids)!=expected_count or len(set(ids))!=expected_count:
        raise ValueError('exact_unique_campaign_roster_required')
    if digest(jobs)!=snapshot['snapshot_sha256']:
        raise ValueError('snapshot_integrity_failed')
    splits={s['job_id']:s['split'] for s in sample['songs']}
    # Fixed from already-accessible development songs, not outcome-selected.
    first=['e926daf14d7a','05bc6835be6b','0b1ba41ea743','497451e63958','b9f7e218a071',
           'a3ce7330f68b','e6e18373f5b6','db054d3e388b','f517564b42d8','36e84c260878']
    rows=[]
    for j in jobs:
        problem=None
        try:
            validate_snapshot(j)
            duration=float(j['duration_seconds'])
            plan=windows(duration)
        except (ValueError, KeyError, TypeError) as exc:
            duration=0.;plan=[];problem='invalid_snapshot:'+type(exc).__name__
        rows.append({'job_id':j['job_id'],'ordinal':j['ordinal'],'artist':j['artist'],'title':j['title'],
            'source':source_binding(j),'duration_seconds':duration,'source_updated_at':j['updated_at'],
            'snapshot_status':j['status'],'approved_at':j.get('approved_at'),
            'protected_lines':sum(bool(s.get('locked') or s.get('operator_locked')) for s in j['segments']),
            'split':splits.get(j['job_id'],'frozen_application_only'),
            'evaluation_use':'development' if j['job_id'] in first else 'frozen_method_no_tuning',
            'audio_path':str((Path(audio_root)/f"{j['job_id']}-mix.wav").resolve()),
            'windows':plan,'first_ten':j['job_id'] in first,'status':'blocked' if problem else 'pending',
            'coverage_seconds':{f:0 for f in FAMILIES},'reconciliation_complete':False,
            'candidate_available':False,'backed_changes':0,'blocker':problem})
    ordered=[i for i in first if i in ids]+[i for i in ids if i not in first]
    return {'schema':'reviewer-campaign-ledger-v1','campaign_id':snapshot['campaign_id'],
        'snapshot_sha256':snapshot['snapshot_sha256'],'snapshot_captured_at':snapshot['captured_at'],
        'roster_sha256':digest(ids),'method':METHOD,'method_sha256':digest(METHOD),
        'first_ten':first,'execution_order':ordered,'songs':rows,
        'authorization':{'approved_usd':0,'max_attempts':0,'status':'not_confirmed'},
        'counts':{'complete':0,'partial':0,'pending':expected_count,'blocked':0}}


def update_status(row, receipts, *, reconciliation_complete=False, candidate=None, blocker=None):
    receipts=[r for r in receipts if r.get('source')==row['source']
        and r.get('clock')=='original_mix_decoded'
        and isinstance(r.get('start'),(int,float)) and isinstance(r.get('end'),(int,float))
        and 0 <= r['start'] < r['end'] <= row['duration_seconds']
        and len(r.get('evidence_sha256',''))==64]
    coverage={f:union_seconds([(r['start'],r['end']) for r in receipts
        if r['family']==f and r['tool_status']=='ok' and r['received_audio']]) for f in FAMILIES}
    full=row['duration_seconds']>0 and all(abs(v-row['duration_seconds'])<1e-4 for v in coverage.values())
    if candidate is not None and candidate.get('source') != row['source']:
        raise ValueError('candidate_source_mismatch')
    row.update(coverage_seconds=coverage,reconciliation_complete=reconciliation_complete,
        candidate_available=candidate is not None,backed_changes=len((candidate or {}).get('changes',[])),blocker=blocker)
    row['status']=('complete' if full and reconciliation_complete and candidate is not None and not blocker else
        'blocked' if blocker else 'partial' if any(coverage.values()) else 'pending')


def counters(manifest):
    rows=manifest['songs'];counts=Counter(r['status'] for r in rows)
    return {**{k:counts[k] for k in ['complete','partial','pending','blocked']},
        'total':len(rows),'candidates_available':sum(r['candidate_available'] for r in rows),
        'full_review_candidates_available':sum(r['candidate_available'] and r['status']=='complete' for r in rows),
        'songs_with_backed_changes':sum(r['backed_changes']>0 for r in rows),
        'complete_without_backed_changes':sum(r['status']=='complete' and not r['backed_changes'] for r in rows)}


class SpendLedger:
    def __init__(self,path,*,approved_usd=0,max_attempts=0):
        self.db=sqlite3.connect(path)
        self.db.execute('CREATE TABLE IF NOT EXISTS attempts (id TEXT PRIMARY KEY, reserve REAL, status TEXT, result_path TEXT)')
        self.db.execute('CREATE TABLE IF NOT EXISTS usage_accounting (id TEXT PRIMARY KEY, priced_usage REAL, usage_json TEXT)')
        self.limit=approved_usd; self.cap=max_attempts

    def reserve(self,identity,provider,duration):
        if provider not in {'openai','google'} or not 0 < duration <= 24:
            raise ValueError('unsupported_billing_request')
        # Conservative configured published rates incl maximum Gemini output.
        from reviewer_shadow_audio import BLIND_PROMPT
        # Byte count bounds text tokenization conservatively; 32 audio tokens/s
        # reserves above observed25, rather than treating one sample as a cap.
        bound=math.ceil(duration)*.006/60 if provider=='openai' else (
            24*32/1e6+len(BLIND_PROMPT.encode('utf-8'))*.30/1e6+4096*2.5/1e6)
        self.db.execute('BEGIN IMMEDIATE')
        try:
            old=self.db.execute('SELECT status,result_path FROM attempts WHERE id=?',(identity,)).fetchone()
            if old:return False,old[0]
            count,total=self.db.execute('SELECT count(*),coalesce(sum(coalesce(u.priced_usage,a.reserve)),0) FROM attempts a LEFT JOIN usage_accounting u ON a.id=u.id').fetchone()
            if count>=self.cap or total+bound>self.limit:return False,'budget_authorization_required'
            self.db.execute('INSERT INTO attempts VALUES (?,?,?,NULL)',(identity,bound,'reserved_unknown_completion'))
            return True,None
        finally:self.db.commit()

    def finish(self,identity,status,path,*,request=None):
        self.db.execute('UPDATE attempts SET status=?,result_path=? WHERE id=?',(status,str(path),identity))
        # Returned usage priced at verified tariffs, NOT an invoice. Unknown
        # completion/missing usage keeps its entire pre-call reservation.
        if request and request.get('received_audio') is True:
            priced=None
            if request.get('provider')=='openai':
                window=request['window'];priced=math.ceil(window['end']-window['start'])*.006/60
            elif request.get('provider')=='google':
                usage=request.get('usage') or {}
                prompt=usage.get('prompt_token_count');output=usage.get('candidates_token_count')
                thoughts=usage.get('thoughts_token_count') or 0
                if all(isinstance(x,int) and x>=0 for x in (prompt,output,thoughts)):
                    # Charge ALL input tokens at the higher audio rate. This
                    # safely over-reserves text and avoids missing modality data.
                    priced=(prompt+(output+thoughts)*2.5)/1e6
            if priced is not None:
                self.db.execute('INSERT OR REPLACE INTO usage_accounting VALUES (?,?,?)',
                    (identity,priced,json.dumps(request.get('usage'))))
        self.db.commit()

    def totals(self):
        n,v,priced,unknown=self.db.execute('SELECT count(*),coalesce(sum(coalesce(u.priced_usage,a.reserve)),0),coalesce(sum(u.priced_usage),0),coalesce(sum(CASE WHEN u.id IS NULL THEN a.reserve ELSE 0 END),0) FROM attempts a LEFT JOIN usage_accounting u ON a.id=u.id').fetchone()
        return {'attempts':n,'reserved_upper_bound_usd':v,'priced_observed_usage_usd':priced,
            'unsettled_reservations_usd':unknown,'remaining_authorized_usd':max(0.,self.limit-v),'billed_usd':None}
