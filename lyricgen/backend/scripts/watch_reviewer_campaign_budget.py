"""Operational cost-only monitor; no lyric inspection, inference, or product writes.

A hold affects NEW spend-ledger reservations only. In-flight calls finish normally.
This command never releases a hold, increases authority, or interprets usage as an invoice.
"""
import argparse
import json
from pathlib import Path
import time

from reviewer_acoustic_cache import cached_receipts, request_index
from reviewer_campaign import SpendLedger, atomic_json, FAMILIES
from scripts.run_reviewer_campaign import authorization, usage_estimate


def project(manifest, index, totals, *, approved_usd):
    samples={p:{'seconds':0.,'usd':0.,'requests':0,'unpriced_requests':0}
             for p in ('openai','google')}
    remaining={p:0. for p in samples}
    family_provider=dict(zip(FAMILIES, ('openai','google')))
    for row in manifest['songs']:
        # Only provenance/duration are passed; no current lyrics or human decisions.
        song={**row['source'],'duration_seconds':row['duration_seconds']}
        cached=cached_receipts(song,index=index)
        for record in cached['records']:
            request=record['request'];p=request['provider'];window=request['window']
            duration=window['end']-window['start']
            cost=usage_estimate([record])
            if cost is None:
                samples[p]['unpriced_requests']+=1
                continue
            samples[p]['seconds']+=duration
            samples[p]['usd']+=cost
            samples[p]['requests']+=1
        # Persisted missing-window plan is conservative during in-flight work:
        # completed calls not checkpointed yet may be counted again in projection.
        missing=row.get('missing_audio_windows')
        if missing is None:
            missing=[{'window':w,'family':f} for w in row.get('windows',[]) for f in FAMILIES]
        for item in missing:
            w=item['window'];remaining[family_provider[item['family']]]+=w['end']-w['start']
    known=all(v['seconds']>0 for v in samples.values())
    rates={p:v['usd']/v['seconds'] if v['seconds'] else None for p,v in samples.items()}
    estimate=sum(remaining[p]*rates[p] for p in samples)*1.2 if known else None
    accounted=totals['priced_observed_usage_usd']+totals['unsettled_reservations_usd']
    balance=approved_usd-accounted
    breach=accounted>approved_usd or (estimate is not None and estimate>balance)
    return {'schema':'reviewer-continuous-budget-v1','approved_usd':approved_usd,
        'accounted_usd':accounted,'remaining_authorized_usd':balance,
        'samples_by_provider':samples,'usd_per_submitted_second':rates,
        'remaining_submitted_seconds':remaining,'retry_reserve_fraction':.2,
        'projected_remaining_usd':estimate,'exceeds_budget':breach,
        'billed_usd':None,'attempts':totals['attempts'],
        'counts':manifest.get('counts',{}),
        'resolved_songs':sum(r['status'] in ('complete','blocked') for r in manifest['songs'])}


def tick(root, authority_path):
    folder=root/'campaign-300'
    manifest=json.loads((folder/'manifest.json').read_text())
    auth=authorization(authority_path,manifest)
    if auth['approved_usd']!=20:
        raise ValueError('monitor_requires_exact_user_authorized_usd20')
    ledger=SpendLedger(folder/'spend.sqlite',approved_usd=20,max_attempts=auth['max_attempts'])
    try:
        result=project(manifest,request_index(root,max_files=25000),ledger.totals(),approved_usd=20)
        if result['exceeds_budget']:
            # hold_after_attempts serializes writes via SQLite. Already-reserved
            # calls are intentionally not cancelled or counted as failed.
            ledger.hold_after_attempts(result['attempts'])
            result['new_reservations_held']=True
            result['hold_reason']='projected_remaining_exceeds_authorized_balance'
            result['created_at_epoch']=time.time()
            atomic_json(folder/'budget-projection-hold.json',result)
        atomic_json(folder/'budget-projection-current.json',result)
        return result
    finally:
        ledger.db.close()


def watch(root, authority_path, *, timeout_seconds=3600, interval_seconds=10):
    if not 0<timeout_seconds<=3600 or interval_seconds!=10:
        raise ValueError('bounded_monitor_10s_interval_60min_max')
    start=time.monotonic();last_resolved=None
    while True:
        result=tick(root,authority_path)
        resolved=result['resolved_songs']
        if result['exceeds_budget']:
            print(json.dumps({'event':'budget_hold',**result}),flush=True);return result
        if last_resolved is None:
            last_resolved=resolved
        if resolved>=300:
            print(json.dumps({'event':'done',**result}),flush=True);return result
        if resolved-last_resolved>=10:
            print(json.dumps({'event':'progress',**result}),flush=True);last_resolved=resolved
        remaining=timeout_seconds-(time.monotonic()-start)
        if remaining<=0:
            print(json.dumps({'event':'timeout',**result}),flush=True);return result
        time.sleep(min(interval_seconds,remaining))


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--root',type=Path,required=True)
    parser.add_argument('--authorization',type=Path,required=True)
    parser.add_argument('--timeout-seconds',type=float,default=3600)
    args=parser.parse_args()
    watch(args.root,args.authorization,timeout_seconds=args.timeout_seconds)
