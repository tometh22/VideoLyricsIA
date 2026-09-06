"""Exact300, resumable isolated review. Zero paid calls without budget authority.

Default cache-only. One owner/concurrency1, zero automatic retries, durable
reservation before any provider request. Unknown completions are not repurchased.
No database writes, suggestions publication, approvals, merges or deployments.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import replace
import json
import math
from pathlib import Path
import subprocess
import time

from reviewer_acoustic_cache import cached_receipts, request_index
from reviewer_batch_bridge import prepare_batch_candidate
from reviewer_campaign import (SpendLedger, atomic_json, counters, create_manifest,
    owner_lock, update_status)
from reviewer_campaign_reconcile import reconcile, SELECTOR_REVISION
from reviewer_phrase_alignment import align_phrase
from reviewer_shadow import ShadowPolicy, source_binding
from reviewer_shadow_audio import BlindAudioTools, extract_clip, file_sha
from shadow_reference_import import digest


def authorization(path, manifest):
    if path is None:
        return {'approved_usd':0,'max_attempts':0,'status':'not_confirmed'}
    auth=json.loads(Path(path).read_text())
    if (auth.get('campaign_id')!=manifest['campaign_id']
        or auth.get('roster_sha256')!=manifest['roster_sha256']
        or auth.get('method_sha256')!=manifest['method_sha256']
        or not auth.get('human_approval_reference')
        or type(auth.get('approved_usd')) not in (int,float)
        or not math.isfinite(auth.get('approved_usd',0))
        or not 0 < auth.get('approved_usd',0) <= 65
        or type(auth.get('max_attempts')) is not int
        or not 0 < auth.get('max_attempts',0) <= 9004):
        raise ValueError('explicit_campaign_budget_authorization_required')
    return auth


def refresh_manifest(previous, fresh):
    """Revisions invalidate only their own products; blind audio remains reusable."""
    if previous is None:
        return fresh
    if any(previous[k]!=fresh[k] for k in ('campaign_id','roster_sha256','method_sha256')):
        raise ValueError('campaign_roster_or_frozen_method_changed_new_run_required')
    old={r['job_id']:r for r in previous['songs']}
    for i,row in enumerate(fresh['songs']):
        prior=old.get(row['job_id'])
        if prior and all(prior.get(k)==row.get(k) for k in
                ('source','duration_seconds','protected_lines','snapshot_status')):
            fresh['songs'][i]={**row,**prior}
        elif prior:
            row['invalidated_previous_source']=prior.get('source')
    return fresh


def covered(receipts, family, window):
    # Cached provider clips may differ by sub-millisecond metadata rounding.
    return any(r['family']==family and r['start']<=window['start']+1e-6
               and r['end']+1e-6>=window['end'] for r in receipts)


def expansion_binding(manifest):
    first=set(manifest['first_ten'])
    return digest({'campaign':manifest['campaign_id'],'roster':manifest['roster_sha256'],
        'method':manifest['method_sha256'],'selector_revision':SELECTOR_REVISION,'first_ten':[
            {k:r.get(k) for k in ('job_id','source','duration_seconds','protected_lines','snapshot_status')}
            for r in manifest['songs'] if r['job_id'] in first]})


def expansion_allowed(path, manifest):
    if not path.exists():return False
    try:check=json.loads(path.read_text())
    except (ValueError,OSError):return False
    rows={r['job_id']:r for r in manifest['songs']}
    return (check.get('binding')==expansion_binding(manifest)
        and check.get('automatic_expansion_allowed') is True
        and all(rows.get(j,{}).get('status')=='complete' for j in manifest['first_ten']))


def verify_audio(audio, song):
    if not audio.exists():raise ValueError('source_audio_not_downloaded')
    if file_sha(audio)!=song['audio_sha256']:raise ValueError('source_audio_sha256_mismatch')
    result=subprocess.run(['ffmpeg','-nostdin','-v','error','-i',str(audio),'-map','0:a:0',
        '-progress','pipe:1','-f','null','-'],capture_output=True,text=True,check=True,timeout=180)
    times=[int(v.split('=',1)[1])/1e6 for v in result.stdout.splitlines()
           if v.startswith('out_time_us=') and v.split('=',1)[1].lstrip('-').isdigit()]
    if not times or abs(max(times)-song['duration_seconds'])>.001:
        raise ValueError('decoded_audio_duration_mismatch')
    return max(times)


def previous_unknown(index,song,provider,window):
    for entry in index:
        request=entry.get('request',{});source=request.get('source',{})
        if (request.get('provider')==provider
            and all(source.get(k)==song.get(k) for k in ('job_id','audio_sha256','audio_revision'))
            and request.get('view')=='mix'
            and request.get('tool_status')=='unknown_completion'
            and all(abs(request.get('window',{}).get(k,-100)-window[k])<1e-6 for k in ('start','end'))):
            return True
    return False


def first_ten_checkpoint(manifest,ledger):
    rows={r['job_id']:r for r in manifest['songs']}
    ready=all(rows.get(j,{}).get('status')=='complete' for j in manifest['first_ten'])
    samples=[rows.get(j,{}) for j in manifest['first_ten']]
    attempts=sum(r.get('observed_usage_requests',0) for r in samples)
    known=all(r.get('usage_estimate_complete') for r in samples) and attempts>0
    cost=sum(r.get('observed_usage_cost_usd',0) for r in samples)
    remaining=sum(len(r.get('missing_audio_windows',[])) if 'missing_audio_windows' in r
        else len(r.get('windows',[]))*2 for r in manifest['songs'] if r['job_id'] not in manifest['first_ten'])
    projection=remaining*cost/attempts*1.2 if known else None
    totals=ledger.totals()
    balance=manifest.get('authorization',{}).get('approved_usd',0)-totals.get('reserved_upper_bound_usd',0)
    affordable=projection is not None and projection<=balance
    return {'all_ten_complete':ready,'counts':{j:rows.get(j,{}).get('status') for j in manifest['first_ten']},
        'binding':expansion_binding(manifest),'method_sha256':manifest['method_sha256'],
        'spend':totals,'automatic_expansion_allowed':ready and affordable,
        'projected_remaining_usd_with_20pct_reserve':projection,'remaining_budget_usd':balance,
        'projection_is_invoice':False,'budget_projection_passed':affordable,
        'systematic_error_check':'all_ten_complete_no_tool_or_reconciliation_blocker'}


def usage_estimate(records):
    costs=[]
    for record in records:
        request=record['request'];usage=request.get('usage') or {}
        if request['provider']=='openai':
            seconds=usage.get('seconds')
            if not isinstance(seconds,(int,float)) or seconds<0:return None
            costs.append(seconds*.006/60)
        else:
            details=usage.get('prompt_tokens_details');output=usage.get('candidates_token_count')
            if not isinstance(details,list) or not isinstance(output,(int,float)):return None
            amount=0.
            for item in details:
                if item.get('modality') not in ('TEXT','AUDIO'):return None
                amount+=item['token_count']*(.3 if item['modality']=='TEXT' else 1)/1e6
            costs.append(amount+(output+(usage.get('thoughts_token_count') or 0))*2.5/1e6)
    return sum(costs)


def run(root, snapshot_path, *, authorization_path=None, max_songs=300):
    out=root/'campaign-300';started=time.monotonic()
    with owner_lock(out):
        snapshot=json.loads(snapshot_path.read_text())
        sample=json.loads((root/'sample.json').read_text())
        target=out/'manifest.json'
        fresh=create_manifest(snapshot,sample,root/'audio')
        manifest=refresh_manifest(json.loads(target.read_text()) if target.exists() else None,fresh)
        auth=authorization(authorization_path,manifest);manifest['authorization']=auth
        ledger=SpendLedger(out/'spend.sqlite',approved_usd=auth['approved_usd'],max_attempts=auth['max_attempts'])
        index=request_index(root);jobs={j['job_id']:j for j in snapshot['jobs']}
        rows={r['job_id']:r for r in manifest['songs']}
        refs=json.loads((root/'import-reconciled.json').read_text())['rows']
        commit=subprocess.check_output(['git','rev-parse','HEAD'],text=True).strip()
        first_check=out/'first-ten-check.json'
        for job_id in manifest['execution_order'][:max_songs]:
            song=jobs[job_id];row=rows[job_id]
            try:
                process_song(root,out,manifest,row,song,refs,index,ledger,commit,
                    paid_allowed=auth['approved_usd']>0 and
                    (row['first_ten'] or expansion_allowed(first_check,manifest)))
            except Exception as exc:
                # Failure details stay per song; credentials / raw provider messages do not leak.
                row.update(status='blocked',blocker='song_execution_failed:'+type(exc).__name__,
                    candidate_available=False,reconciliation_complete=False,backed_changes=0,
                    operationally_published=False)
                row['failure_code']=str(exc) if isinstance(exc,ValueError) and str(exc) in {
                    'source_audio_not_downloaded','source_audio_sha256_mismatch',
                    'decoded_audio_duration_mismatch','empty_transcription_baseline'} else type(exc).__name__
            index=request_index(root)
            manifest['counts']=counters(manifest);manifest['spend']=ledger.totals()
            manifest['run_latency_seconds']=round(time.monotonic()-started,3)
            atomic_json(target,manifest)
            print(json.dumps({'job_id':job_id,'status':row['status'],'counts':manifest['counts'],
                'spend':manifest['spend']}),flush=True)
            if job_id==manifest['first_ten'][-1]:
                check=first_ten_checkpoint(manifest,ledger)
                atomic_json(out/'first-ten-status.json',check)
                # Replace stale success with explicit failure, not existence-based authority.
                atomic_json(first_check,check)
        print(json.dumps({'counts':manifest['counts'],'spend':manifest['spend'],
            'latency_seconds':manifest['run_latency_seconds'],'manifest':str(target)}))


def process_song(root,out,manifest,row,song,refs,index,ledger,commit,*,paid_allowed):
    job_id=song['job_id']
    if not song['segments'] or not any(s.get('text','').strip() for s in song['segments']):
        # This repair/adoption method requires existing caption occurrences.
        # An empty full-length JSON document must never count as a candidate.
        raise ValueError('empty_transcription_baseline')
    cached=cached_receipts(song,index=index)
    folder=out/job_id;folder.mkdir(mode=0o700,exist_ok=True)
    errors=[]
    if paid_allowed:
        audio=Path(row['audio_path'])
        if row['windows']:
            row['decoded_duration_seconds']=verify_audio(audio,song)
            tool=BlindAudioTools(folder/'requests',policy=replace(ShadowPolicy(),
                max_calls_per_song=len(row['windows'])*2))
            for w in row['windows']:
                pending=[]
                for provider,family in [('openai','openai/whisper-1'),('google','google/gemini-2.5-flash-audio')]:
                    if covered(cached['receipts'],family,w):
                        continue
                    if previous_unknown(index,song,provider,w):
                        errors.append('unknown_completion_not_repeated');continue
                    identity=digest({'audio':{k:song[k] for k in ('job_id','audio_sha256','audio_revision')},
                        'window':w,'provider':provider,'method':manifest['method_sha256']})
                    clip=folder/(digest(w)+'.wav')
                    if not clip.exists():
                        extract_clip(audio,w,clip)
                    reserved,reason=ledger.reserve(identity,provider,w['end']-w['start'])
                    retrying=False
                    if not reserved and reason=='invalid_response':
                        # Exactly one new request after a KNOWN malformed result.
                        # Unknown completion and transport errors never enter here.
                        identity=digest({'retry_of':identity,'retry_number':1})
                        reserved,reason=ledger.reserve(identity,provider,w['end']-w['start'])
                        retrying=reserved
                    if not reserved:
                        errors.append(reason);continue
                    listener=(BlindAudioTools(folder/'retry-1'/'requests',policy=replace(ShadowPolicy(),
                        max_calls_per_song=1)) if retrying else tool)
                    pending.append((identity,listener,clip,provider))
                # One song / two independent provider calls maximum. ALL
                # reservations commit on the owner thread before any call starts.
                with ThreadPoolExecutor(max_workers=2) as pool:
                    futures={pool.submit(listener.listen,clip,provider=provider,view='mix',
                        source=source_binding(song),window=w):identity
                        for identity,listener,clip,provider in pending}
                    for future in as_completed(futures):
                        request=future.result();identity=futures[future]
                        ledger.finish(identity,request['tool_status'],folder/'requests',request=request)
                        if request['tool_status']!='ok':errors.append('audio_tool_'+request['tool_status'])
            index=request_index(root);cached=cached_receipts(song,index=index)
    reference=next((r for r in refs if r.get('matched_job_id')==job_id
        and r.get('association')=='unique_metadata_candidate' and r.get('availability')=='present'),None)
    usage=usage_estimate(cached['records'])
    row.update(observed_usage_requests=len(cached['records']),usage_estimate_complete=usage is not None,
        observed_usage_cost_usd=usage or 0,usage_is_invoice=False)
    reconciliation_key=digest({'source':source_binding(song),'method':manifest['method_sha256'],
        'selector_revision':SELECTOR_REVISION,
        'reference':reference,'receipts':[r['evidence_sha256'] for r in cached['receipts']]})
    if (not errors and row.get('reconciliation_key')==reconciliation_key
        and (folder/'candidate.json').exists() and (folder/'review.json').exists()
        and row.get('reconciliation_complete')):
        old_candidate=json.loads((folder/'candidate.json').read_text())
        if old_candidate.get('source')==source_binding(song):return
    candidate=None;review=None
    if cached['records']:
        candidate,review=reconcile(song,cached,commit=commit,external_reference=reference)
        candidate['realignments']=[]
        for change in candidate['changes']:
            if change['field']!='text':continue
            i=change['line_index'];line=song['segments'][i]
            start=max(0.,line['start']-1);end=min(start+24,song['duration_seconds'],
                song['segments'][i+1]['start'] if i+1<len(song['segments']) else song['duration_seconds'])
            alignment_path=folder/(digest([source_binding(song),change])+'-alignment.json')
            if alignment_path.exists():
                alignment=json.loads(alignment_path.read_text())
            else:
                alignment=align_phrase(row['audio_path'],change['after'],{'start':start,'end':end,'offset_seconds':start})
                atomic_json(alignment_path,alignment)
            candidate['realignments'].append({'line_index':i,'display_timing_changed':False,**alignment})
        atomic_json(folder/'review.json',review);atomic_json(folder/'candidate.json',candidate)
    update_status(row,cached['receipts'],candidate=candidate,
        reconciliation_complete=review is not None,
        blocker=';'.join(sorted(set(errors))) if errors else None)
    row['cached_requests']=len(cached['records'])
    row['reconciliation_key']=reconciliation_key
    row['selector_revision']=SELECTOR_REVISION
    row['missing_audio_windows']=[{'window':w,'family':f} for w in row['windows']
        for f in ('openai/whisper-1','google/gemini-2.5-flash-audio')
        if not covered(cached['receipts'],f,w)]
    row['operationally_published']=False
    if row['status']=='complete':
        prepared=prepare_batch_candidate(song,candidate,review,original_segments=song.get('original_segments'))
        atomic_json(folder/'prepared-product-bridge.json',prepared)


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--root',type=Path,required=True)
    p.add_argument('--snapshot',type=Path,required=True);p.add_argument('--authorization',type=Path)
    p.add_argument('--max-songs',type=int,choices=[10,300],default=300)
    args=p.parse_args();run(args.root,args.snapshot,authorization_path=args.authorization,max_songs=args.max_songs)
