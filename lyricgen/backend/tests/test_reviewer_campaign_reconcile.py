from copy import deepcopy
from reviewer_campaign_reconcile import human_protected, reconcile
from reviewer_shadow import source_binding
from shadow_reference_import import digest


def song():
    rows=[{'text':'Quiero cantar contigo','start':1.,'end':4.}]
    return {'job_id':'sample','audio_sha256':'a'*64,'audio_revision':1,
        'segments_revision':0,'segments':rows,'segments_sha256':digest(rows),
        'duration_seconds':6.,'original_segments':deepcopy(rows)}


def cached(s):
    rows=[]
    for provider,family in [('openai','openai/whisper-1'),('google','google/gemini-2.5-flash-audio')]:
        words=[{'word':w,'start':i+1.,'end':i+1.7} for i,w in enumerate(['Quiero','vivir','contigo'])]
        response={'text':'Quiero vivir contigo','words':words} if provider=='openai' else {
            'events':[{'text':'Quiero vivir contigo','start':1.,'end':3.7,'kind':'sung'}]}
        request={'provider':provider,'family':family,'view':'mix','tool_status':'ok',
            'received_audio':True,'conditioning_texts':[],'source':source_binding(s),
            'window':{'start':0.,'end':6.,'offset_seconds':0.},'response':response}
        annotations=[{'text':w['word'],'local_start':w['start'],'local_end':w['end'],
            'global_start':w['start'],'global_end':w['end']} for w in words] if provider=='openai' else []
        rows.append({'request':request,'annotations':annotations,'invalid_annotations':[],
            'original_source':source_binding(s),'evidence_sha256':'a'*64})
    return {'records':rows,'receipts':[],'excluded':[]}


def test_all_rows_reconciled_without_flags_no_truth_claim():
    s=song();before=deepcopy(s)
    c,r=reconcile(s,cached(s),commit='a'*40)
    assert s==before
    assert len(r['line_diagnostics'])==1
    assert r['line_diagnostics'][0]['baseline_is_correct'] is None
    assert r['line_diagnostics'][0]['association']['candidates']
    assert r['line_diagnostics'][0]['endpoint_selector']=='not_reached_no_endpoint_candidate'
    assert c['segments'][0]['end']==4.


def test_missing_listening_explicit_not_certified():
    c,r=reconcile(song(),{'records':[],'receipts':[],'excluded':[]},commit='a'*40)
    assert r['line_diagnostics'][0]['phrase_status']=='not_examined'
    assert c['changes']==[]
    assert not c['residual_qc']['complete_audio_coverage_verified']


def test_unlocked_human_timing_and_structural_changes_protected():
    s=song();s['segments'][0]['end']=4.2
    assert human_protected(s,0)
    s=song();s['original_segments'].append({'text':'x'})
    assert human_protected(s,0)


def test_original_absent_not_assumed_safe_after_revision_zero():
    s=song();s.pop('original_segments');s['segments_revision']=3
    assert human_protected(s,0)
