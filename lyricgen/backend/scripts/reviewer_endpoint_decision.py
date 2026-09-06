"""Freeze two review-only experiments before reading development comparators."""
import argparse
import json
from pathlib import Path
import statistics
import subprocess
from reviewer_endpoint_options import options
from reviewer_singing_activity import run_activity
from reviewer_ctc_frames import inspect
from reviewer_shadow_audio import file_sha, private_write


def run(root,out):
    out.mkdir(mode=0o700,parents=True,exist_ok=True)
    source=json.loads((root/'integral-v2/input.json').read_text())
    audio=root/'audio/e926daf14d7a-mix.wav'
    assert file_sha(audio)==source['audio_sha256']
    rows=[]
    for i in [6,33,40]:
        cached=json.loads((root/f'text-frames-v2/model-line-{i}.json').read_text())
        ctc, old=cached['ctc'],cached['singing_activity']
        w=old['window']
        # Two extra seconds on either side; same frame lattice (140 hops).
        wide={'start':w['start']-2,'end':min(source['duration_seconds'],w['end']+2)}
        wide['offset_seconds']=wide['start']
        path=out/f'activity-wide-{i}.json'
        if path.exists(): new=json.loads(path.read_text())
        else:
            new=run_activity(root/'veracity-vendor',audio,wide)
            private_write(path,new)
        deltas=[abs(f['probability']-new['frames'][j+140]['probability'])
            for j,f in enumerate(old['frames']) if j+140<len(new['frames'])
            and j>=57 and j<len(old['frames'])-57]
        boundary_deltas=[abs(f['probability']-new['frames'][j+140]['probability'])
            for j,f in enumerate(old['frames']) if j+140<len(new['frames'])
            and (j<57 or j>=len(old['frames'])-57)]
        seg=source['segments'][i]
        limit=source['segments'][i+1]['start'] if i+1<len(source['segments']) else source['duration_seconds']
        rows.append({'line_index':i,'baseline_end':seg['end'],
            'clock_check':{'same_decoded_audio_sha256':new['audio_sha256']==ctc['audio_sha256'],
                'cross_signal_transfer':False,'frame_lattice_shift':140,
                'interior_probability_delta_median':statistics.median(deltas),
                'boundary_probability_delta_max':max(boundary_deltas),
                'perceptual_latency_verified':False},
            'options':options(ctc,new,seg['end'],limit)})
    frozen={'rows':rows,'commit':subprocess.check_output(['git','rev-parse','HEAD'],text=True).strip(),
        'human_targets_seen_by_inference':False,'approach_count':2,'provider_calls':0}
    path=out/'predictions-frozen.json'
    if not path.exists():private_write(path,frozen)
    else: frozen=json.loads(path.read_text())
    # Deliberately only after immutable prediction artifact exists.
    targets={r['line_index']:r for r in json.loads((root/'integral-v2/evaluation.json').read_text())['rows']}
    result=[]
    for row in frozen['rows']:
        target=targets[row['line_index']]['human_end']
        error=abs(row['baseline_end']-target)
        result.append({'line_index':row['line_index'],'human_end':target,'baseline_error':error,
            'clean_gold':False,'comparator':'auto_trim_contaminated_development',
            'methods':{m:[{**v,'absolute_error':abs(v['end']-target),
                'improves':abs(v['end']-target)<error,'worsens':abs(v['end']-target)>error,
                'within_150ms':abs(v['end']-target)<=.15} for v in row['options'][m]]
                for m in ['A_ctc_path_peaks','B_activity_fall_ctc']}})
    if not (out/'evaluation.json').exists():private_write(out/'evaluation.json',
        {'prediction_sha256':file_sha(path),'rows':result,'automatic_changes':0})
    print(json.dumps(result),flush=True)
    control_path=out/'luciano-control.json'
    if not control_path.exists():
        control=json.loads((root/'sustain-occurrence-v1.json').read_text())
        song=next(j for j in json.loads((root/'snapshot.json').read_text())['jobs'] if j['job_id']=='497451e63958')
        i=control['line_index'];seg=song['segments'][i]
        audio=root/'audio/497451e63958-mix.wav'
        assert file_sha(audio)==song['audio_sha256']
        window=control['occurrence_bounded_alignment']['window']
        ctc=inspect(audio,seg['text'],window)
        activity=run_activity(root/'veracity-vendor',audio,
            {'start':window['start']-2,'end':window['end']+2,'offset_seconds':window['start']-2})
        result={'job_id':song['job_id'],'line_index':i,'ctc':ctc,'activity':activity,
            'options':options(ctc,activity,seg['end'],song['segments'][i+1]['start']),
            'known_defect':False,'human_precision_measured':False,'document_modified':False}
        private_write(control_path,result)
        print(json.dumps({'control_options':result['options']}),flush=True)


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--root',type=Path,required=True);p.add_argument('--output',type=Path,required=True)
    a=p.parse_args();run(a.root,a.output)
