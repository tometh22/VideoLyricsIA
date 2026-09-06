"""Cached text repair plus a bounded new singing-activity/CTC experiment."""
import argparse
import json
from pathlib import Path
import subprocess
import unicodedata
from reviewer_shadow import review_window, tokens
from reviewer_correspondence import correspond
from reviewer_candidate import build_candidate
from reviewer_phrase_alignment import align_phrase
from reviewer_shadow_audio import private_write
from reviewer_singing_activity import run_activity
from reviewer_ctc_frames import inspect


def text_run(root, output):
    source=json.loads((root/'integral-v2/input.json').read_text())
    previous=json.loads((root/'integral-v2/predictions-frozen.json').read_text())
    listening=json.loads((root/'integral-v2/listening.json').read_text())
    commit=subprocess.check_output(['git','rev-parse','HEAD'],text=True).strip()
    output.mkdir(mode=0o700,parents=True,exist_ok=True)
    decisions=[]
    for r in previous['lines']:
        for p in r['text_candidates']:
            if not p['eligible']: continue
            w=next(w for w in listening['windows'] if w['window']==p['window'])
            decisions.append(review_window(source,{**p['window'],'line_index':r['line_index']},
                evidence=[{**q,'kind':'minimal_text_patch_request'} for q in w['requests']],commit=commit))
    # Preserve the earlier supported development patch; no new paid calls.
    old=json.loads((root/'text-edit-replay-v1/report.json').read_text())
    for case in old['cases']:
        decisions.append(review_window(source,case['window'],
            evidence=[{**q,'kind':'minimal_text_patch_request'} for q in case['requests']],commit=commit))
    candidate=build_candidate(source,decisions)
    candidate['realignments']=[]
    for change in candidate['changes']:
        if change['field']!='text': continue
        i=change['line_index']
        context=next(d['window'] for d in decisions if d['window']['line_index']==i)
        baseline=source['segments'][i]
        # Independent recognition kept its larger context; localization uses
        # the baseline occurrence neighborhood, not the next phrase/repetition.
        start=max(0.,baseline['start']-1.)
        end=source['segments'][i+1]['start'] if i+1<len(source['segments']) else min(start+24.,source['duration_seconds'])
        w={'start':start,'end':min(end,start+24.),'offset_seconds':start}
        candidate['realignments'].append({'line_index':i,'recognition_window':context,
            'display_timing_changed':False,**align_phrase(root/'audio/e926daf14d7a-mix.wav',change['after'],w)})
    candidate['acoustic_inspection_coverage_seconds']=listening['coverage_seconds']
    candidate['inspection_is_not_certification']=True
    candidate['implementation_commit']=commit
    private_write(output/'candidate.json',candidate)
    audit=[]
    for r in previous['lines']:
        if r['phrase_recognized']: continue
        row=r['baseline']; options=[]
        for w in listening['windows']:
            if w['window']['start']>=row['end'] or w['window']['end']<=row['start']: continue
            whisper=next(q for q in w['requests'] if q['provider']=='openai')
            google=next(q for q in w['requests'] if q['provider']=='google')
            mapping=correspond(row,whisper,w['window'])
            for c in mapping.get('candidates',[]):
                heard=tokens(' '.join(e.get('text','') for e in google.get('response',{}).get('events',[])))
                seq=c['heard_tokens']
                c['also_in_gemini_hypothesis']=any(heard[j:j+len(seq)]==seq for j in range(len(heard)-len(seq)+1))
                c['window']=w['window']
                options.append(c)
        options.sort(key=lambda x:(x['edit_cost'],x['length_difference'],not x['window_contains_baseline'],-x['similarity']))
        best=options[0] if options else None
        classification=('unresolved_no_anchor' if not best else 'formatting_only' if best['formatting_only'] else
            'window_truncation_possible' if not best['window_contains_baseline'] else
            'two_family_lexical_hypothesis' if best['also_in_gemini_hypothesis'] else 'recognition_disagreement')
        if best and any(op[0]=='delete' for op in best['operations']):
            classification='partial_correspondence_not_omission_proof'
        # Competing distinct locations remain visible; never certify by score.
        conflicts=bool(best and any(abs(c['start']-best['start'])>1 and c['similarity']==best['similarity'] for c in options))
        if conflicts: classification='occurrence_conflict'
        if best and not conflicts:
            fold=lambda seq: [''.join(c for c in unicodedata.normalize('NFD',t) if not unicodedata.combining(c)) for t in seq]
            if tokens(row['text']) != best['heard_tokens'] and fold(tokens(row['text'])) == fold(best['heard_tokens']):
                classification='orthographic_diacritic_difference_not_lexical_error'
        audit.append({'line_index':r['line_index'],'baseline_text':row['text'],'classification':classification,
            'correspondences':options[:3],'human_error_confirmed':False,'similarity_is_certification':False})
    private_write(output/'correspondences.json',{'lines':audit,'provider_calls':0,'implementation_commit':commit})
    print(json.dumps({'candidate_changes':len(candidate['changes']),'audited_nonexact_lines':len(audit)}))


def model_run(root,output):
    output.mkdir(mode=0o700,parents=True,exist_ok=True)
    prior=json.loads((root/'integral-v2/predictions-frozen.json').read_text())
    for i in [6,33,40]:
        row=prior['lines'][i]
        window=row['ctc']['window']
        audio=root/'audio/e926daf14d7a-mix.wav'
        result={'line_index':i,'baseline_end':row['baseline']['end'],
            'ctc':inspect(audio,row['baseline']['text'],window),
            'singing_activity':run_activity(root/'veracity-vendor',audio,window),
            'timing_repair_selected':False,'stem_comparison':'blocked_clock_correspondence_unverified'}
        private_write(output/f'model-line-{i}.json',result)
        print(json.dumps({'line':i,'activity_frames':len(result['singing_activity']['frames'])}),flush=True)
    old=json.loads((root/'sustain-occurrence-v1.json').read_text())
    window=old['occurrence_bounded_alignment']['window']
    audio=root/'audio/497451e63958-mix.wav'
    private_write(output/'luciano-activity.json',run_activity(root/'veracity-vendor',audio,window))


if __name__=='__main__':
    p=argparse.ArgumentParser()
    p.add_argument('phase',choices=['text','model'])
    p.add_argument('--root',required=True,type=Path)
    p.add_argument('--output',required=True,type=Path)
    a=p.parse_args()
    (text_run if a.phase=='text' else model_run)(a.root,a.output)
