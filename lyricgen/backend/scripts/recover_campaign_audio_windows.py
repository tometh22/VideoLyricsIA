"""One bounded subdivision of a known failed response; never unknown retries.

Same model/prompt as v2. Two overlapping half-windows replace only a known
failed24s window, not successful audio. Outcomes and extra cost remain explicit.
"""
import argparse
import json
from pathlib import Path

from reviewer_campaign import SpendLedger, atomic_json, owner_lock
from reviewer_shadow import source_binding
from reviewer_shadow_audio import BlindAudioTools, extract_clip
from shadow_reference_import import digest
from scripts.run_reviewer_campaign import authorization, verify_audio


def recover(root,snapshot,job_id,failed_path):
    folder=root/'campaign-300'
    with owner_lock(folder):
        manifest=json.loads((folder/'manifest.json').read_text())
        auth=authorization(folder/'authorization.json',manifest)
        ledger=SpendLedger(folder/'spend.sqlite',approved_usd=auth['approved_usd'],max_attempts=auth['max_attempts'])
        song=next(s for s in snapshot['jobs'] if s['job_id']==job_id)
        failed=json.loads(failed_path.read_text())
        if (failed.get('tool_status')!='invalid_response' or failed.get('received_audio') is not True
            or failed.get('provider')!='google' or failed.get('source')!=source_binding(song)
            or failed.get('prompt_version')!='blind-vocal-events-shadow-v2-bounded-schema'):
            raise ValueError('known_failed_v2_audio_response_required')
        parent=failed['window'];mid=(parent['start']+parent['end'])/2
        if not 18 < parent['end']-parent['start'] <= 24:
            raise ValueError('one_subdivision_only_no_recursive_recovery')
        parts=[{'start':parent['start'],'end':mid+1,'offset_seconds':parent['start']},
               {'start':mid-1,'end':parent['end'],'offset_seconds':mid-1}]
        audio=root/'audio'/f'{job_id}-mix.wav';verify_audio(audio,song)
        out=folder/job_id/'bounded-recovery'/digest(parent);out.mkdir(parents=True,mode=0o700,exist_ok=True)
        listener=BlindAudioTools(out/'requests');results=[]
        for window in parts:
            identity=digest({'recovery_of':digest(failed),'window':window,'attempt':1})
            clip=out/(digest(window)+'.wav')
            if not clip.exists():extract_clip(audio,window,clip)
            reserved,reason=ledger.reserve(identity,'google',window['end']-window['start'])
            if not reserved:
                results.append({'window':window,'status':'not_repeated','reason':reason});continue
            request=listener.listen(clip,provider='google',view='mix',source=source_binding(song),window=window)
            ledger.finish(identity,request['tool_status'],out/'requests',request=request)
            results.append({'window':window,'status':request['tool_status'],
                'error_type':request.get('error_type'),'latency_seconds':request.get('latency_seconds')})
        report={'job_id':job_id,'parent':parent,'failed_evidence_sha256':digest(failed),
            'hypothesis':'shorter_context_avoids_repetition_output_overflow',
            'only_changed_component':'window_duration','automatic_apply_allowed':False,
            'subdivision_rounds':1,'results':results,'spend':ledger.totals()}
        atomic_json(out/'report.json',report);print(json.dumps(report))


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--root',type=Path,required=True)
    p.add_argument('--snapshot',type=Path,required=True);p.add_argument('--job',required=True)
    p.add_argument('--failed',type=Path,required=True);a=p.parse_args()
    recover(a.root,json.loads(a.snapshot.read_text()),a.job,a.failed)
