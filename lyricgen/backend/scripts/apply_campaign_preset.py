"""Apply a reusable creative preset through the public API; optionally generate.

Examples (from the repository root):
  python3 lyricgen/backend/scripts/apply_campaign_preset.py --campaign ID --railway-environment staging
  python3 lyricgen/backend/scripts/apply_campaign_preset.py --campaign ID --railway-environment staging --apply
  python3 lyricgen/backend/scripts/apply_campaign_preset.py --campaign ID --railway-environment staging --apply --generate --wait
  python3 lyricgen/backend/scripts/apply_campaign_preset.py --campaign ID --railway-environment staging --generate --wait

Without --apply/--generate, only reads and prints the proposal. --apply saves
seven reusable percentage groups in Campañas > Estilo y fondos. --generate
submits only the exact roster saved in the manifest, through native approval,
quota and queue gates. --wait drains a full render window every 30 seconds;
a full human review buffer stops the process and can be resumed later. It
never approves lyrics, approves final renders, publishes, or retries failures.
For a later batch, choose a new --manifest path. --defer-effect bass_pulse
keeps that recipe assigned but does not submit its remaining songs.

Authentication: --api-url plus GENLY_TOKEN or GENLY_USERNAME/GENLY_PASSWORD;
alternatively --railway-environment loads the API hostname and admin password
into memory and logs in normally. Tokens/passwords are never written to disk.
"""
import argparse
from collections import Counter
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import subprocess
import time

import requests

DEFAULT_PRESET = Path(__file__).resolve().parents[1] / 'presets' / 'umg_photo_effects.json'
ACCEPTED_STATES = {'queued', 'processing', 'rendering', 'pending_review', 'done'}
CAPACITY_CODES = {'batch_render_window_full', 'batch_final_review_full'}


def atomic_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n')
    temporary.replace(path)


def groups_for(preset):
    effects = preset['effects']
    if not effects or len({e['id'] for e in effects}) != len(effects):
        raise ValueError('Preset effects must have unique IDs')
    return [{'id': e['id'], 'name': e['name'], 'weight': 100 / len(effects),
             'requirement': 'photo_effect', 'model': '',
             'settings': {**preset['settings'], 'effect': e['effect']}} for e in effects]


def ready_items(head):
    return [i for i in head['items'] if i['status'] == 'lyrics_approved'
            and not i.get('discarded') and i.get('job_id')]


def generation_form(item, job):
    segments = job.get('segments_json') or job.get('segments')
    if not segments:
        raise ValueError('Approved job has no persisted segments')
    data = {'job_id': item['job_id'], 'artist': item.get('artist') or job.get('artist') or '',
            'song_title': item.get('title') or job.get('song_title') or '',
            'segments_json': json.dumps(segments, ensure_ascii=False),
            'base_revision': str(job['segments_revision']),
            'campaign_creative_revision': str(item['assignment']['revision'])}
    for key, value in item['settings'].items():
        if value is not None:
            data[key] = str(value).lower() if isinstance(value, bool) else str(value)
    return data


class APIError(RuntimeError):
    def __init__(self, response):
        self.status = response.status_code
        try:
            self.detail = response.json().get('detail')
        except ValueError:
            self.detail = 'Non-JSON API response'
        self.code = self.detail.get('code') if isinstance(self.detail, dict) else None
        super().__init__(f'HTTP {self.status}: {self.detail}')


class API:
    def __init__(self, args):
        self.session = requests.Session()
        self.base = args.api_url or ''
        token = os.environ.get('GENLY_TOKEN')
        username = os.environ.get('GENLY_USERNAME')
        password = os.environ.get('GENLY_PASSWORD')
        if args.railway_environment:
            result = subprocess.run(['railway', 'variables', '-e', args.railway_environment,
                                     '-s', 'api', '--json'], capture_output=True, text=True, check=True)
            variables = json.loads(result.stdout)
            self.base = self.base or 'https://' + variables['RAILWAY_STATIC_URL']
            username = username or 'admin'
            password = password or variables['ADMIN_PASSWORD']
        if not self.base.startswith('https://'):
            raise ValueError('An HTTPS API URL is required')
        self.base = self.base.rstrip('/')
        if not token:
            if not username or not password:
                raise ValueError('Provide GENLY_TOKEN or normal login credentials')
            login = self.request('/auth/login', method='POST', json={'username': username, 'password': password})
            token = login['token']
        self.session.headers['Authorization'] = 'Bearer ' + token

    def request(self, path, method='GET', **kwargs):
        response = self.session.request(method, self.base + path, timeout=(10, 90), **kwargs)
        if not response.ok:
            raise APIError(response)
        return response.json()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--campaign', required=True)
    parser.add_argument('--preset', type=Path, default=DEFAULT_PRESET)
    parser.add_argument('--api-url')
    parser.add_argument('--railway-environment')
    parser.add_argument('--manifest', type=Path)
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--generate', action='store_true')
    parser.add_argument('--wait', action='store_true')
    parser.add_argument('--defer-effect', action='append', default=[])
    args = parser.parse_args(argv)
    manifest_path = args.manifest or Path('.context') / f'campaign-preset-{args.campaign}.json'
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    owner = manifest_path.with_suffix('.lock').open('w')
    try:
        fcntl.flock(owner, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise RuntimeError('Another process owns this batch; inspect its manifest instead') from None
    # Keep owner alive for the entire workflow. The OS releases the lock
    # on exit, including crashes, so a later resume cannot overlap it.
    preset = json.loads(args.preset.read_text())
    groups = groups_for(preset)
    if set(args.defer_effect) - {g['settings']['effect'] for g in groups}:
        raise ValueError('Deferred effect is not part of this preset')
    api = API(args)
    base = f'/batch/campaigns/{args.campaign}'
    head = api.request(base + '/creative')
    manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else None
    if manifest and (manifest['campaign_id'] != args.campaign or manifest['api_url'] != api.base):
        raise ValueError('Manifest belongs to another campaign or API')
    if not manifest or not manifest.get('applied'):
        ready = ready_items(head)
        print(json.dumps({'preset':preset['name'], 'ready':len(ready), 'current_revision':head['plan']['revision'],
                          'effects':[g['settings']['effect'] for g in groups]}, ensure_ascii=False), flush=True)
        if not args.apply:
            if args.generate:
                raise ValueError('Apply first, or resume an existing applied manifest')
            return 0
        if not ready:
            raise ValueError('No approved songs are ready to generate')
        if any((i.get('assignment') or {}).get('pinned') for i in ready):
            raise ValueError('Selection has pinned exceptions; review before assigning')
        manifest = {'campaign_id':args.campaign, 'api_url':api.base, 'preset':preset,
                    'created_at':datetime.now(timezone.utc).isoformat(), 'applied':False,
                    'items':[{'id':i['id'], 'job_id':i['job_id'], 'title':i['title']} for i in ready],
                    'submitted':[], 'failures':{}, 'before_plan':head['plan']}
        atomic_json(manifest_path, manifest)
        request = {'revision':head['plan']['revision'], 'item_ids':[i['id'] for i in ready],
                   'mode':'percent', 'groups':groups, 'seed':'umg-photo-effects-v1',
                   'replace_exceptions':False, 'pin':False, 'contract':False,
                   'reason':'Aplicar preset UMG foto fija inspirada en la letra con siete efectos equilibrados; balance conjunto AR+CL autorizado.'}
        preview = api.request(base + '/creative/preview', method='POST', json=request)
        if preview['skipped'] or len(preview['changes']) != len(ready):
            raise ValueError('Preview did not include the exact approved roster')
        expected = {g['name']:g for g in groups}
        for change in preview['changes']:
            after = change['after']
            if (after['movement_style'] != 'foto-parallax' or not after['match_lyrics']
                or after['animate_image'] or after['enable_scenes']
                or after['effect'] != expected[change['group']]['settings']['effect']):
                raise ValueError('Preview does not match the requested photo recipe')
        manifest['preview'] = preview
        atomic_json(manifest_path, manifest)
        result = api.request(base + '/creative/apply', method='POST', json={'preview_id':preview['preview_id']})
        manifest.update(applied=True, operation_id=result['operation_id'], revision=result['revision'])
        atomic_json(manifest_path, manifest)
        head = api.request(base + '/creative')
        selected = [i for i in head['items'] if i['id'] in {r['id'] for r in ready}]
        assert len(selected) == len(ready)
        assert all(i['assignment']['revision'] == result['revision'] for i in selected)
        print(json.dumps({'applied':len(selected), 'effects':dict(Counter(i['settings']['effect'] for i in selected)),
                          'operation_id':result['operation_id']}, ensure_ascii=False), flush=True)
    if not args.generate:
        return 0
    target = {i['id']:i for i in manifest['items']}
    while True:
        head = api.request(base + '/creative')
        current = {i['id']:i for i in head['items']}
        manifest['render_failures'] = {
            i['job_id']:i['status'] for i in current.values()
            if i['id'] in target and i['job_id'] in manifest['submitted']
            and i['status'] in {'error','validation_failed','rejected'}
        }
        pending = [current[key] for key in target if target[key]['job_id'] not in manifest['submitted']
                   and target[key]['job_id'] not in manifest['failures']]
        deferred = [i['job_id'] for i in pending if i['settings'].get('effect') in args.defer_effect]
        pending = [i for i in pending if i['job_id'] not in deferred]
        blocked = None
        for item in pending:
            job_id = item['job_id']
            if job_id != target[item['id']]['job_id']:
                raise ValueError('Selected song now points to a different job')
            if item['status'] in ACCEPTED_STATES:
                manifest['submitted'].append(job_id)
                atomic_json(manifest_path, manifest)
                continue
            if item['status'] != 'lyrics_approved':
                manifest['failures'][job_id] = 'Approval/state changed: ' + item['status']
                atomic_json(manifest_path, manifest)
                continue
            assignment = item.get('assignment') or {}
            if assignment.get('revision') != manifest['revision']:
                manifest['failures'][job_id] = 'Creative assignment changed; review before generating'
                atomic_json(manifest_path, manifest)
                continue
            try:
                job = api.request('/status/' + job_id)
                # A concurrent submission must never be repeated.
                if job['status'] in ACCEPTED_STATES:
                    manifest['submitted'].append(job_id)
                else:
                    result = api.request('/generate', method='POST', data=generation_form(item, job))
                    manifest['submitted'].append(job_id)
                atomic_json(manifest_path, manifest)
                print(json.dumps({'submitted':len(manifest['submitted']), 'total':len(target), 'job_id':job_id,
                                  'title':item['title']}, ensure_ascii=False), flush=True)
            except APIError as exc:
                if exc.status == 429 and exc.code in CAPACITY_CODES:
                    blocked = exc.code
                    break
                manifest['failures'][job_id] = str(exc)
                atomic_json(manifest_path, manifest)
                print(json.dumps({'job_id':job_id,'error':str(exc)}, ensure_ascii=False), flush=True)
            except requests.RequestException:
                # Leave this job unrecorded. Resume first inspects its current
                # server state, avoiding a second submission after a timeout.
                atomic_json(manifest_path, manifest)
                raise RuntimeError('Ambiguous transport failure; resume to inspect server status') from None
        outstanding = len(target) - len(manifest['submitted']) - len(manifest['failures'])
        manifest['blocked'] = blocked
        manifest['outstanding'] = outstanding
        manifest['deferred'] = deferred
        atomic_json(manifest_path, manifest)
        print(json.dumps({'submitted':len(manifest['submitted']), 'total':len(target), 'outstanding':outstanding,
                          'failures':len(manifest['failures']), 'render_failures':manifest['render_failures'],
                          'deferred':len(deferred), 'blocked':blocked}, ensure_ascii=False), flush=True)
        if not outstanding:
            return 1 if manifest['failures'] or manifest['render_failures'] else 0
        if not args.wait or blocked != 'batch_render_window_full':
            return 2
        time.sleep(30)


if __name__ == '__main__':
    raise SystemExit(main())
