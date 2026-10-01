from copy import deepcopy

import pytest

from reviewer_campaign_product import CAMPAIGN
from reviewer_shadow import source_binding
from scripts.publish_reviewer_campaign import validate_bundle, publish_bundle
from shadow_reference_import import digest


def bundle():
    songs=[{'job_id':f'{i:012d}','audio_sha256':'a'*64,'audio_revision':1,'segments_revision':0,
            'segments':[],'segments_sha256':digest([])} for i in range(300)]
    return {'schema':'reviewer-campaign-release-bundle-v1','campaign_id':CAMPAIGN,
        'songs':songs,'artifacts':{},'manifest':{'campaign_id':CAMPAIGN,
            'execution_order':[s['job_id'] for s in songs],
            'songs':[{'job_id':s['job_id'],'source':source_binding(s),'status':'pending'} for s in songs]}}


def test_exact_300_source_bound_roster_order_required():
    value=bundle();before=deepcopy(value)
    assert len(validate_bundle(value)[0])==300
    assert value==before
    value['manifest']['execution_order'][0]=value['manifest']['execution_order'][1]
    with pytest.raises(ValueError,match='300_roster'):validate_bundle(value)


def test_wrong_revision_or_campaign_rejected():
    value=bundle();value['songs'][0]['segments_revision']=1
    with pytest.raises(ValueError,match='source_binding'):validate_bundle(value)
    value=bundle();value['campaign_id']='different'
    with pytest.raises(ValueError,match='exact_campaign'):validate_bundle(value)


def test_never_publication_outside_staging(monkeypatch):
    monkeypatch.setenv('ENVIRONMENT','production')
    with pytest.raises(ValueError,match='staging_environment_required'):
        publish_bundle(bundle(),execute=True)
