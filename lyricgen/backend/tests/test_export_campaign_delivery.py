import json

import pytest

from scripts.export_campaign_delivery import export


def test_delivery_freezes_only_complete_current_candidates(tmp_path, monkeypatch):
    from scripts import export_campaign_delivery as module
    folder=tmp_path/'campaign-300';(folder/'one').mkdir(parents=True)
    source={'job_id':'one','segments_revision':1}
    candidate={'source':source,'changes':[],
               'residual_qc':{'complete_audio_coverage_verified':True}}
    path=folder/'one'/'candidate.json';path.write_text(json.dumps(candidate))
    campaign={'campaign_id':'c','snapshot_sha256':'s','counts':{'complete':1},'songs':[
        {'job_id':'one','source':source,'status':'complete'},
        {'job_id':'two','status':'partial'},{'job_id':'three','status':'blocked'}]}
    (folder/'manifest.json').write_text(json.dumps(campaign))
    monkeypatch.setattr(module.subprocess,'run',lambda *a,**k:None)
    delivery=tmp_path/'delivery';export(tmp_path,delivery)
    manifest=json.loads((delivery/'manifest.json').read_text())
    assert len(manifest['songs'])==3 and len(manifest['candidates'])==1
    assert not manifest['operationally_published']
    path.write_text('{}')
    assert json.loads((delivery/'candidates'/'one.json').read_text())==candidate
    with pytest.raises(ValueError,match='already_exists'):
        export(tmp_path,delivery)
