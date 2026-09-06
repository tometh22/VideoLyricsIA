from copy import deepcopy
import pytest

from reviewer_excel_audit import audit
from reviewer_excel_audit import _local_reference_hypotheses
from reviewer_candidate import build_candidate
from reviewer_shadow import source_binding
from shadow_reference_import import digest
from shadow_reference_import import availability


def fixture():
    rows = [{'text': 'Un polizonte alta mar', 'start': 1., 'end': 5.}]
    song = {'job_id': 'sample', 'audio_sha256': 'a'*64, 'audio_revision': 1,
            'segments_revision': 0, 'segments': rows, 'segments_sha256': digest(rows),
            'original_segments': deepcopy(rows), 'duration_seconds': 12.}
    ref = {'matched_job_id': 'sample', 'availability': 'present',
           'association': 'unique_metadata_candidate', 'lyrics': 'Un polizonte en alta mar'}
    records = []
    for family in ('openai/whisper-1', 'google/gemini-2.5-flash-audio'):
        records.append({'request': {'family': family, 'view': 'mix', 'tool_status': 'ok',
            'received_audio': True, 'conditioning_texts': [], 'source': source_binding(song),
            'clip_sha256': 'b'*64, 'window': {'start': 0., 'end': 12., 'offset_seconds': 0.},
            'response': {}}, 'evidence_sha256': family[0]*64,
            'annotations': [{'text': ref['lyrics'], 'global_start': 1., 'global_end': 4.9}]})
    return song, ref, {'records': records}


def test_missing_word_from_excel_requires_actual_dual_audio():
    s, r, c = fixture(); before = deepcopy(s)
    result = audit(s, r, c, commit='test')
    assert len(result['decisions']) == 1
    candidate = build_candidate(s, result['decisions'])
    assert candidate['segments'][0]['text'] == r['lyrics']
    assert candidate['segments'][0]['end'] == 5.
    assert s == before
    assert not result['reference_used_as_audio_witness']


@pytest.mark.parametrize('mode', ['missing_family', 'conditioned', 'wrong_audio', 'stem', 'repeated', 'wrong_occurrence', 'ambiguous'])
def test_no_manufactured_support(mode):
    s, r, c = fixture()
    rec = c['records'][1]
    if mode == 'missing_family': c['records'].pop()
    if mode == 'conditioned': rec['request']['conditioning_texts'] = [r['lyrics']]
    if mode == 'wrong_audio': rec['request']['source']['audio_sha256'] = 'c'*64
    if mode == 'stem': rec['request']['view'] = 'stem'
    if mode == 'repeated': rec['annotations'] *= 2
    if mode == 'wrong_occurrence': rec['annotations'][0].update(global_start=8., global_end=10.)
    if mode == 'ambiguous': rec['request']['response']['editorial_ambiguity'] = True
    assert audit(s, r, c, commit='test')['decisions'] == []


@pytest.mark.parametrize('protection', ['locked', 'operator_locked', 'human_edit', 'approved'])
def test_preservation(protection):
    s, r, c = fixture()
    if protection in {'locked', 'operator_locked'}: s['segments'][0][protection] = True
    if protection == 'human_edit': s['segments'][0]['end'] = 5.1
    if protection == 'approved': s['status'] = 'lyrics_approved'
    s['segments_sha256'] = digest(s['segments'])
    assert audit(s, r, c, commit='test')['decisions'] == []


def test_excel_wrong_association_and_format_not_corrections():
    s, r, c = fixture()
    r['matched_job_id'] = 'another'
    assert audit(s, r, c, commit='test')['status'] == 'no_accepted_text_reference'
    r['matched_job_id'] = 'sample'; r['lyrics'] = 'UN POLIZONTE ALTA MAR.'
    a = audit(s, r, c, commit='test')
    assert a['decisions'] == []
    assert a['differences'][0]['status'] == 'format_only_preserved'


def test_two_provider_events_overlapping_neighbor_not_occurrence_proof():
    s, r, c = fixture()
    s['segments'].append({'text': 'Otra frase', 'start': 5., 'end': 8.})
    s['original_segments'] = deepcopy(s['segments']); s['segments_sha256'] = digest(s['segments'])
    r['lyrics'] += '\nOtra frase'
    c['records'][1]['annotations'][0]['global_end'] = 7.
    assert audit(s, r, c, commit='test')['decisions'] == []


def test_pointer_not_a_lyric():
    assert availability('[Musixmatch] https://example.test/lyrics/song') == 'pointer_only'
    assert availability('https://example.test/song') == 'pointer_only'
    assert availability('Un polizonte en alta mar') == 'present'
    assert availability('NO ENCONTRADA') == 'not_found'


def test_hearing_substring_cannot_prove_words_should_be_deleted():
    s, r, c = fixture()
    r['lyrics'] = 'polizonte alta mar'
    assert audit(s, r, c, commit='test')['differences'][0]['status'] == 'deletion_requires_independent_absence_evidence'


def test_acoustic_agreement_cannot_choose_written_accent():
    s, r, c = fixture()
    s['segments'][0]['text'] = 'Si te mandás'
    s['original_segments'] = deepcopy(s['segments']); s['segments_sha256'] = digest(s['segments'])
    r['lyrics'] = 'Si te mandas'
    assert audit(s, r, c, commit='test')['differences'][0]['status'] == 'diacritics_require_orthographic_not_acoustic_decision'


def test_local_reference_anchors_locate_hypothesis_not_certification():
    s, r, c = fixture()
    hypotheses = _local_reference_hypotheses(s, 'Otra frase distinta\nUn polizonte en alta mar\nOtra estrofa')
    assert len(hypotheses) == 1
    assert hypotheses[0]['reference_tokens'] == ['en']
    assert audit(s, r, {'records': []}, commit='test')['decisions'] == []
