import json
from pathlib import Path
import pytest
from change_request_parser import parse_change_request
from change_request_proposals import build_proposal, apply_operations


def test_range_is_one_instruction_with_preserved_punctuation():
    rows = parse_change_request('Desde 0:10 hasta 0:14: "¿Dónde estás?"')['instructions']
    assert len(rows) == 1
    assert (rows[0]['kind'], rows[0]['requested_start'], rows[0]['requested_end'], rows[0]['requested_text']) == ('set_interval', 10, 14, '¿Dónde estás?')


def test_metadata_and_unknown_prefix_are_not_dropped():
    rows = parse_change_request('Corregir artista: Charly García // Revisar toda la sincronización // 4:06 "Gonna get YOU"')['instructions']
    assert [r['kind'] for r in rows] == ['set_metadata', 'timing_review', 'replace_text']


def test_apostrophes_are_lyrics_not_quotes():
    row = parse_change_request("3:58 La cola p'acá balanceando")['instructions'][0]
    assert row['kind'] == 'manual_review'
    assert "p'acá" in row['source_excerpt']


def test_single_quoted_replacement_and_mixed_structure_preserve_every_instruction():
    rows = parse_change_request('Cambiar "p\'acá" por "para acá"')['instructions']
    assert rows[0]['current_text'] == "p'acá"
    assert rows[0]['requested_text'] == 'para acá'
    rows = parse_change_request('0:01: Hola mundo\nRevisar frases completas en una sola pantalla // Corregir artista: Charly García // Verificar el final')['instructions']
    assert {r['kind'] for r in rows} == {'layout_context', 'set_metadata', 'manual_review'}


def test_interval_applies_only_exact_unique_phrase_and_clears_words():
    segments = [{'start': 12, 'end': 15, 'text': 'Una frase', 'words': [{'word': 'Una', 'start': 12}]}]
    p = build_proposal(comment='Desde 0:10 hasta 0:14: "Una frase"', segments=segments, base_revision=0, duration_seconds=20)
    op = p['operations'][0]
    assert op['automatic_apply_allowed']
    result, _ = apply_operations(segments, p, [op['id']])
    assert (result[0]['start'], result[0]['end']) == (10, 14)
    assert 'words' not in result[0]
    assert result[0]['alignment_stale']


def test_explicit_text_replacement_auto_applies_only_when_source_phrase_is_unique():
    segment = {'start': 0, 'end': 2, 'text': 'Hola', 'words': [{'word': 'Hola'}]}
    proposal = build_proposal(comment='Cambiar "Hola" por "Chau"', segments=[segment], base_revision=0)
    operation = proposal['operations'][0]
    assert operation['automatic_apply_allowed'] is True
    result, _ = apply_operations([segment], proposal, [operation['id']])
    assert result[0]['text'] == 'Chau' and result[0]['alignment_stale'] is True
    repeated = build_proposal(comment='Cambiar "Hola" por "Chau"',
        segments=[segment, {**segment, 'start': 4, 'end': 6}], base_revision=0)
    # Ambiguous repeated source text stays unresolved and cannot auto-apply.
    assert repeated['operations'][0]['kind'] == 'replace_text'
    assert repeated['operations'][0]['applicable'] is False
    assert repeated['operations'][0]['automatic_apply_allowed'] is False


@pytest.mark.parametrize('extra', [
    {'start': 8, 'end': 11, 'text': 'Vecina'},
    {'start': 16, 'end': 18, 'text': 'Una frase'},
])
def test_overlap_and_repeated_occurrence_abstain(extra):
    p = build_proposal(comment='Desde 0:10 hasta 0:14: Una frase', segments=[extra, {'start': 12, 'end': 15, 'text': 'Una frase'}], base_revision=0, duration_seconds=20)
    assert not p['operations'][0]['applicable']


def test_deletion_crossing_boundary_stays_manual():
    p = build_proposal(comment='Eliminar todo desde 3:32', segments=[{'start': 210, 'end': 214, 'text': 'Final'}], base_revision=0)
    assert p['operations'][0]['reason'] == 'deletion_crosses_segment_or_time_missing'


def test_conflicting_ranges_do_not_apply():
    p = build_proposal(comment='Desde 0:10 hasta 0:14: "Hola" // Desde 0:11 hasta 0:15: "Hola"', segments=[{'start': 12, 'end': 15, 'text': 'Hola'}], base_revision=0, duration_seconds=20)
    assert all(o['status'] == 'conflict' and not o['applicable'] for o in p['operations'])


def test_all_original_requests_preserve_each_clause():
    corpus = json.loads((Path(__file__).parent / 'fixtures/umg_2026_11_08_requests.json').read_text())
    assert len(corpus) == 17
    for case in corpus:
        for clause in case['comment'].split(' // '):
            rows = parse_change_request(clause)['instructions']
            assert rows, case['request_id']
            assert all(r.get('requested_text') != 'hasta' for r in rows)
            assert all(' //' not in (r.get('requested_text') or '') for r in rows)


def test_two_individually_valid_moves_cannot_create_overlap_together():
    segments = [{'start':0.,'end':1.,'text':'Primera'}, {'start':9.,'end':10.,'text':'Segunda'}]
    p = build_proposal(comment='Desde 0:04 hasta 0:06: "Primera" // Desde 0:05 hasta 0:07: "Segunda"', segments=segments, base_revision=0, duration_seconds=20)
    assert all(o['status'] == 'conflict' and not o['applicable'] for o in p['operations'])
    with pytest.raises(ValueError):
        apply_operations(segments,p,[o['id'] for o in p['operations']])


def test_explicit_artist_can_fill_an_omission_without_touching_catalog():
    proposal = build_proposal(comment='Corregir artista: Carajo',segments=[{'start':0,'end':2,'text':'Hola'}],
        base_revision=0,background_context={'artist':'','song_title':'Inocencia perdida'})
    operation = proposal['operations'][0]
    assert operation['automatic_apply_allowed']
    assert operation['field'] == 'artist' and operation['current_value'] == ''
    assert operation['proposed_value'] == 'Carajo'


def test_locked_line_and_newline_instruction_stay_pending():
    rows = parse_change_request('0:10 Una frase\nSacar puntos finales')['instructions']
    assert [r['kind'] for r in rows] == ['manual_review','manual_review']
    proposal = build_proposal(comment='Desde 0:10 hasta 0:14: "Una frase"',base_revision=0,duration_seconds=30,
        segments=[{'start':12,'end':15,'text':'Una frase','operator_locked':True}])
    assert not proposal['operations'][0]['automatic_apply_allowed']


def test_deleting_one_word_is_not_interpreted_as_deleting_the_whole_tail():
    parsed = parse_change_request('Eliminar la palabra hola desde 0:10')
    assert all(r['kind'] != 'delete_from' for r in parsed['instructions'])
    assert parse_change_request('Desde -0:10 hasta 0:14: Hola')['instructions'][0]['reason'] == 'ambiguous_or_invalid_timecode'
