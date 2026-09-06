"""All-row reconciliation of actual blind listening; no original mutations.

Sequence similarity locates hypotheses, not truth. Endpoints stay unchanged:
the archived endpoint experiments supplied no defensible operational repair.
"""
from copy import deepcopy

from reviewer_candidate import build_candidate
from reviewer_correspondence import correspond
from reviewer_integral import locate_words
from reviewer_shadow import review_window, source_binding, tokens
from shadow_reference_import import digest

SELECTOR_REVISION='caption-neighbor-ownership-v2-recovered-witnesses'


def human_protected(song, index):
    row=song['segments'][index]
    if row.get('locked') or row.get('operator_locked'):
        return True
    original=song.get('original_segments')
    if original is None:
        return song.get('segments_revision',0)>0
    if len(original)!=len(song['segments']):
        return True
    return any(row.get(k)!=original[index].get(k) for k in ('text','start','end'))


def reconcile(song, cached, *, commit, external_reference=None):
    groups={}
    for record in cached['records']:
        req=record['request'];w=req['window'];key=(w['start'],w['end'])
        # Prefer one immutable receipt per family/window. Preserve all in audit.
        groups.setdefault(key,{})[req['provider']]=record
    decisions=[];diagnostics=[];held=[]
    for i,line in enumerate(song['segments']):
        options=[]
        for (start,end),group in groups.items():
            if not max(start,line['start']) < min(end,line['end']):
                continue
            whisper=group.get('openai')
            if not whisper:
                continue
            request=deepcopy(whisper['request'])
            request['response']['words']=[{'word':a['text'],'start':a['local_start'],
                'end':a['local_end']} for a in whisper['annotations']]
            window={'start':start,'end':end,'offset_seconds':start,'line_index':i}
            exact=locate_words(line['text'],request,window,line)
            association=correspond(line,request,window)
            contains=start<=line['start'] and end>=line['end']
            options.append({'window':window,'group':group,'exact':exact,
                'association':association,'whisper':request,
                'rank':(not contains,exact['status']!='unique_overlapping_occurrence',
                    'google' not in group,abs((start+end-line['start']-line['end'])/2))})
        options.sort(key=lambda x:x['rank'])
        trace={'line_index':i,'protected':human_protected(song,i),
            'baseline':{'start':line['start'],'end':line['end']},
            'windows_examined':[o['window'] for o in options],
            'phrase_status':'not_examined','occurrence_status':'not_localized',
            'endpoint_generation':'abstain_no_validated_canto_boundary_model',
            'endpoint_selector':'not_reached_no_endpoint_candidate',
            'baseline_is_correct':None,'content_decision':'conserved_not_certified'}
        if not options:
            diagnostics.append(trace);continue
        o=options[0];trace.update(phrase_status=o['exact']['status'],
            occurrence_status=o['association']['status'],
            exact=o['exact'],association=o['association'])
        # Keep format differences separate from lexical recognition conflicts.
        if o['exact']['selected']:
            trace['discrepancy_class']='normalized_text_match_not_certification'
        elif o['association'].get('candidates'):
            best=o['association']['candidates'][0]
            trace['discrepancy_class']=('context_truncation_possible' if not best['window_contains_baseline']
                else 'lexical_or_occurrence_hypothesis_requires_audio_evidence')
        else:
            trace['discrepancy_class']='phrase_association_unresolved'
        evidence=[]
        witness_records=list(o['group'].values())
        if 'google' not in o['group']:
            alternatives=[r for r in cached['records'] if r['request']['provider']=='google'
                and r['request']['window']['start']<=line['start']
                and r['request']['window']['end']>=line['end']]
            if alternatives:
                witness_records.append(min(alternatives,key=lambda r:
                    r['request']['window']['end']-r['request']['window']['start']))
        trace['witness_windows']=[{'family':r['request']['family'],
            'window':r['request']['window'],'evidence_sha256':r['evidence_sha256']}
            for r in witness_records]
        for record in witness_records:
            evidence.append({**record['request'],'kind':'minimal_text_patch_request',
                'source':source_binding(song),'original_source':record['original_source'],
                'cached_evidence_sha256':record['evidence_sha256']})
        decision=review_window(song,o['window'],evidence=evidence,commit=commit)
        verdict=decision['content']
        if verdict['decision']=='propose':
            proposed=verdict['text']
            location=locate_words(proposed,o['whisper'],o['window'],line)
            trace['proposed_text']=proposed;trace['proposed_occurrence']=location
            before,after=tokens(line['text']),tokens(proposed)
            tokenization_only=''.join(before)==''.join(after) and before!=after
            # A wider listening window may include neighboring displayed words.
            # Agreement that they were sung does not authorize duplicating them
            # into this caption. Keep the full evidence as an editorial doubt.
            replacement=verdict.get('replacement_tokens',[])
            span=verdict.get('changed_token_range',[-1,-1])
            previous=tokens(song['segments'][i-1]['text']) if i else []
            following=tokens(song['segments'][i+1]['text']) if i+1<len(song['segments']) else []
            duplicates_neighbor=(span[0]==0 and any(replacement[:n]==previous[-n:]
                for n in range(1,min(len(previous),len(replacement))+1)) or (
                span[1]==len(before) and any(replacement[-n:]==following[:n]
                for n in range(1,min(len(following),len(replacement))+1))))
            reason=('human_protection' if trace['protected'] else
                'adjacent_caption_word_ownership_unresolved' if duplicates_neighbor else
                'tokenization_requires_editorial_review' if tokenization_only else
                'proposed_occurrence_not_unique' if not location['selected'] else None)
            if reason:
                trace['content_decision']='held:'+reason
                held.append({'decision':decision,'reason':reason})
            else:
                decisions.append(decision);trace['content_decision']='proposed_changed_span_only'
        else:
            trace['content_decision']=verdict['decision']+':'+str(verdict.get('reason',''))
        diagnostics.append(trace)
    candidate=build_candidate(song,decisions,external_reference=external_reference)
    # Regions heard outside displayed lyrics are questions, not proven omissions.
    outside=[]
    seen=set()
    for record in cached['records']:
        if record['request']['provider']!='google':
            continue
        for a in record['annotations']:
            if a.get('kind')!='sung':
                continue
            overlap=any(max(a['global_start'],s['start'])<min(a['global_end'],s['end']) for s in song['segments'])
            key=(tuple(tokens(a['text'])),round(a['global_start'],1),round(a['global_end'],1))
            if not overlap and key not in seen:
                seen.add(key);outside.append({**a,'evidence_sha256':record['evidence_sha256'],
                    'classification':'possible_uncovered_singing_not_certified_omission'})
    review={'schema':'full-song-review-v1','source':source_binding(song),
        'selector_revision':SELECTOR_REVISION,'implementation_commit':commit,
        'reconciliation_complete':True,'required_families':['openai/whisper-1','google/gemini-2.5-flash-audio'],
        'audio_evidence':cached['receipts'],'line_diagnostics':diagnostics,
        'uncovered_singing_hypotheses':outside,'held_decisions':held,
        'held_decision_ids':[h['decision']['proposal_id'] for h in held],
        'invalid_annotations':[{'evidence_sha256':r['evidence_sha256'],'annotations':r['invalid_annotations']}
            for r in cached['records'] if r['invalid_annotations']],
        'excluded_requests':[{'reason':r['reason'],'evidence_sha256':r.get('evidence_sha256'),
            'tool_status':r.get('request',{}).get('tool_status')} for r in cached['excluded']],
        'original_documents_modified':False,'human_checked':False}
    candidate['campaign_reconciliation_sha256']=digest(review)
    candidate['selector_revision']=SELECTOR_REVISION
    candidate['implementation_commit']=commit
    candidate['residual_qc']['campaign_line_diagnostics']=diagnostics
    candidate['residual_qc']['uncovered_singing_hypotheses']=outside
    candidate['residual_qc']['unresolved_decisions'].extend(
        {'line_index':d['line_index'],'kind':'content','reason':d.get('discrepancy_class','not_examined')}
        for d in diagnostics if d['phrase_status']!='unique_overlapping_occurrence')
    return candidate,review
