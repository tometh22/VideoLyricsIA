from copy import deepcopy
from reviewer_correction_history import summarize


def test_precision_noise_ignored_but_one_persisted_unit_preserved():
    row = {'_id':0, 'start':1., 'end':2., 'text':'hola'}
    versions = [{'revision':0,'segments':[row]}]
    versions.append({'revision':1,'reason':'manual','created_by':2,
                     'segments':[{**row,'end':2.00001}]})
    versions.append({'revision':2,'reason':'manual','created_by':2,
                     'segments':[{**row,'end':2.0001}]})
    report = summarize(versions)
    assert len(report['events']) == 1
    assert report['events'][0]['delta_seconds']['end'] == .0001
    assert not report['clean_gold']


def test_structural_and_translation_not_same_occurrence_endpoints():
    a = {'_id':0,'start':1.,'end':2.,'text':'hola'}
    b = {**a,'start':3.,'end':4.}
    versions = [{'revision':0,'segments':[a]},
                {'revision':1,'reason':'manual','segments':[b]},
                {'revision':2,'reason':'autosave','segments':[b,{**a,'_id':1}]}]
    report=summarize(deepcopy(versions))
    assert report['counts']=={'translated_or_reassociated_interval':1,'split_merge_or_unverified_association':1}


def test_historical_approval_clamp_is_not_human_endpoint_gold():
    a=[{'_id':18,'start':90.2229,'end':95.23,'text':'Primera','locked':True},
       {'_id':19,'start':95.24,'end':97.42,'text':'Siguiente'}]
    b=deepcopy(a); b[0]['end']=95.19
    result=summarize([{'revision':62,'segments':a}, {'revision':63,'reason':'approve','segments':b}])
    assert result['events'][0]['category']=='approval_clamp_signature_excluded'
    assert result['events'][0]['delta_seconds']['end']==-.04
