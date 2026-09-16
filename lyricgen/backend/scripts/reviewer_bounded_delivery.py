"""Two unreviewed development songs, at most eight blind requests each.

No database access. Existing human documents and held-out test songs are not
inputs. A whole candidate does not imply whole-song acoustic inspection.
"""
import argparse
import json
from pathlib import Path
import subprocess
import time

from reviewer_candidate import build_candidate
from reviewer_phrase_alignment import align_phrase
from reviewer_shadow import review_window, source_binding
from reviewer_shadow_audio import BlindAudioTools, extract_clip, file_sha, private_write


def run(root, output):
    jobs = {j['job_id']: j for j in json.loads((root/'snapshot.json').read_text())['jobs']}
    samples = {s['job_id']: s for s in json.loads((root/'sample.json').read_text())['songs']}
    assets = {s['job_id']: s for s in json.loads((root/'assets-private.json').read_text())['jobs']}
    refs = json.loads((root/'import-reconciled.json').read_text())['rows']
    commit = subprocess.check_output(['git', 'rev-parse', 'HEAD'], text=True).strip()
    output.mkdir(parents=True, mode=0o700, exist_ok=True)
    entries = []
    for job_id in ['05bc6835be6b', '0b1ba41ea743']:
        song = jobs[job_id]
        assert song['segments_revision'] == 0
        assert not any(s.get('locked') or s.get('operator_locked') for s in song['segments'])
        audio = root/'audio'/f'{job_id}-mix.wav'
        assert file_sha(audio) == song['audio_sha256']
        folder = output/job_id
        folder.mkdir(mode=0o700, exist_ok=True)
        final = folder/'candidate.json'
        started = time.monotonic()
        tools = BlindAudioTools(folder/'requests')
        decisions, traces = [], []
        for window in samples[job_id]['windows'][:4]:
            assert window['end']-window['start'] <= 24
            clip = folder/f"line-{window['line_index']}.wav"
            if not clip.exists():
                extract_clip(audio, window, clip)
            requests = [tools.listen(clip, provider=p, view='mix',
                source=source_binding(song), window=window) for p in ['openai', 'google']]
            decisions.append(review_window(song, window, commit=commit,
                evidence=[{**r, 'kind':'minimal_text_patch_request'} for r in requests]))
            traces.append({'window':window, 'clip':str(clip.resolve()), 'requests':requests})
            print(json.dumps({'job_id':job_id,'line':window['line_index'],
                'providers':[r['tool_status'] for r in requests]}), flush=True)
        candidate = build_candidate(song, decisions,
            hypotheses=assets[job_id]['machine_evidence']['hypotheses_by_family'],
            external_reference=next((r for r in refs if r.get('matched_job_id') == job_id), None))
        candidate['realignments'] = []
        for change in candidate['changes']:
            if change['field'] != 'text':
                continue
            i = change['line_index']
            start = max(0.,song['segments'][i]['start']-1)
            end = min(start+24, song['segments'][i+1]['start'] if i+1<len(song['segments']) else song['duration_seconds'])
            candidate['realignments'].append({'line_index':i, 'display_timing_changed':False,
                **align_phrase(audio, change['after'], {'start':start,'end':end,'offset_seconds':start})})
        candidate.update(implementation_commit=commit, human_checked=False,
            acoustic_scope='four_sampled_windows_not_full_song', requests=traces,
            provider_calls_this_run=tools.calls, observed_cost_usd=None,
            latency_seconds=round(time.monotonic()-started,3))
        if not final.exists():
            private_write(final,candidate)
        entries.append({'job_id':job_id,'mode':'current_snapshot','label':song['artist']+' · '+song['title'],
            'path':str(final.resolve()),'changes':len(candidate['changes'])})
    for job_id, mode, path in [
        ('e926daf14d7a','historical_development',root/'text-frames-v3/candidate.json'),
        ('e926daf14d7a','current_snapshot',root/'full-candidates-v2/e926daf14d7a-current_snapshot.json'),
        ('497451e63958','current_snapshot',root/'full-candidates-v2/497451e63958-current_snapshot.json'),
        ('b9f7e218a071','current_snapshot',root/'full-candidates-v2/b9f7e218a071-current_snapshot.json')]:
        entries.append({'job_id':job_id,'mode':mode,'path':str(path.resolve())})
    if not (output/'manifest.json').exists():
        private_write(output/'manifest.json',{'candidates':entries,'documents_modified':False})


if __name__ == '__main__':
    p=argparse.ArgumentParser()
    p.add_argument('--root',type=Path,required=True)
    p.add_argument('--output',type=Path,required=True)
    a=p.parse_args()
    run(a.root,a.output)
