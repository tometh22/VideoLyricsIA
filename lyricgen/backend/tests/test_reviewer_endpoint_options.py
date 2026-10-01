import pytest
from reviewer_endpoint_options import options


def signals():
    ctc={'clock':'original_mix_decoded','audio_sha256':'a','window':{'start':0,'end':4},
        'frames':[{'time':i*.02,'last_character_exit_relative_log_score':-abs(i-120)} for i in range(200)]}
    activity={'clock':'original_mix_decoded','audio_sha256':'a','window':{'start':0,'end':4},
        'receptive_field_seconds':115/70,
        'frames':[{'time':i/70,'probability':float(i<168)} for i in range(280)]}
    return ctc,activity


def test_review_options_preserve_baseline_and_cannot_shorten_or_cross_occurrence():
    ctc,activity=signals()
    result=options(ctc,activity,2.,3.)
    assert result['baseline_alternative']==2.
    assert not result['automatic_apply_allowed']
    for method in ['A_ctc_path_peaks','B_activity_fall_ctc']:
        assert result[method]
        assert all(2.<c['end']<=3. for c in result[method])
    assert result['selector_decision']=='keep_baseline_pending_human_review'


def test_wrong_audio_or_clock_rejects_transfer():
    ctc,activity=signals()
    activity['audio_sha256']='b'
    with pytest.raises(ValueError,match='different_audio'):options(ctc,activity,2.,3.)
    activity['clock']='stem'
    with pytest.raises(ValueError,match='unverified_clock'):options(ctc,activity,2.,3.)


def test_no_later_context_is_abstention_not_correctness():
    ctc,activity=signals()
    result=options(ctc,activity,4.,4.)
    assert result['A_ctc_path_peaks']==result['B_activity_fall_ctc']==[]
    assert result['abstentions']['A']
    assert not result['same_word_continuation_verified']
