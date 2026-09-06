"""Capture exact staging campaign in a read-only transaction, no remote files."""
import argparse
import base64
import json
from pathlib import Path
import subprocess
import shlex
from reviewer_shadow_audio import private_write

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--output',type=Path,required=True)
    p.add_argument('--identity',required=True);a=p.parse_args()
    source=Path(__file__).with_name('export_reviewer_shadow_snapshot.py').read_bytes()
    code="import base64;exec(compile(base64.b64decode(%r),'readonly_snapshot','exec'))" % base64.b64encode(source).decode()
    result=subprocess.run(['railway','ssh','-e','staging','-s','api','-i',a.identity,
        '--',shlex.join(['python','-c',code,'--campaign','ba3318bdfffe'])],capture_output=True,timeout=90)
    if result.returncode:
        raise RuntimeError('staging_snapshot_transport_failed_exit_'+str(result.returncode))
    payload=json.loads(result.stdout)
    if len(payload['jobs'])!=300 or len({j['job_id'] for j in payload['jobs']})!=300:
        raise ValueError('campaign_not_exactly_300')
    private_write(a.output,payload)
    print(json.dumps({'campaign_id':payload['campaign_id'],'count':len(payload['jobs']),
        'captured_at':payload['captured_at'],'snapshot_sha256':payload['snapshot_sha256']}))
