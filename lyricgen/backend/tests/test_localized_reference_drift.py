from copy import deepcopy
from transcription_language import build_language_contract, localized_reference_drift, diagnostic_reference_text

REFERENCE = 'Voy a dar que hablar\nVoy a pescar con la red un gran pulpo\nBusco quien me quiera'

def test_repeated_foreign_tail_is_not_hidden_by_distinct_token_count():
    rows = [{'text': line} for line in REFERENCE.splitlines()[:2]]
    rows += [{'text': 'Pudje erke avlaren, Pudje erke avlaren,'},
             {'text': 'Tøs, tøs, tøs, tøs, tøs, tøs'}]
    original = deepcopy(rows)
    assert localized_reference_drift(rows, REFERENCE) == [2, 3]
    c = build_language_contract(rows, REFERENCE, infer_uncertain_from_request=False)
    assert c['needs_language_review']
    assert c['output_reference_localized_drift_indices'] == [2, 3]
    assert rows == original

def test_matching_majority_does_not_dilute_local_tail():
    rows = [{'text': line} for line in REFERENCE.splitlines()[:2]] * 20
    rows += [{'text': 'Pudje erke avlaren, Pudje erke avlaren,'}, {'text': 'Tøs tøs tøs tøs'}]
    assert localized_reference_drift(rows, REFERENCE) == [40, 41]

def test_vocalizations_do_not_become_language_errors():
    rows = [{'text': line} for line in REFERENCE.splitlines()[:2]]
    rows += [{'text': 'oh oh oh oh'}, {'text': 'uoh ouh uoh ouh'}]
    assert localized_reference_drift(rows, REFERENCE) == []

def test_bilingual_reference_preserves_foreign_repetitions():
    rows = [{'text': line} for line in REFERENCE.splitlines()[:2]]
    rows += [{'text': 'We can change the world tonight'}, {'text': 'Come back come back'}]
    ref = REFERENCE + '\nWe can change the world tonight\nCome back come back'
    assert localized_reference_drift(rows, ref) == []

def test_missing_reference_no_fabricated_drift():
    assert localized_reference_drift([{'text': 'Tøs tøs tøs tøs'}] * 2, '') == []

def test_single_unmatched_fragment_or_no_matching_anchors_abstains():
    rows = [{'text': line} for line in REFERENCE.splitlines()[:2]]
    assert localized_reference_drift(rows + [{'text': 'Tøs tøs tøs tøs'}], REFERENCE) == []
    assert localized_reference_drift([{'text': 'Tøs tøs tøs tøs'}] * 2, REFERENCE) == []

def test_rejected_alignment_keeps_audio_hypothesis_for_diagnostics_only():
    result = {'reference_lyrics': '', 'reference_candidate_rejected': True,
              'reference_hypothesis_candidate': {'text': REFERENCE,
                'complete_audio_verified': True, 'source_kind': 'gemini_complete_audio_derived'}}
    original = deepcopy(result)
    assert diagnostic_reference_text(result) == REFERENCE
    assert result == original
    result['reference_hypothesis_candidate']['source_kind'] = 'catalog_unverified'
    assert diagnostic_reference_text(result) == ''
    result['reference_hypothesis_candidate']['source_kind'] = 'gemini_complete_audio_derived'
    result['reference_hypothesis_candidate']['complete_audio_verified'] = False
    assert diagnostic_reference_text(result) == ''
