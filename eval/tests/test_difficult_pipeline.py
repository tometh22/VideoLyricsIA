from eval.difficult_pipeline import _offset_segments, independent_family_agreement, whisper_family_medoid


def test_slow_tta_maps_timestamps_back_to_original_clock():
    result = _offset_segments([{"start": 2, "end": 4, "text": "hola"}], 10, 0.8)
    assert result[0]["start"] == 11.6
    assert result[0]["end"] == 13.2


def test_tta_is_one_family_and_independent_agreement_verifies():
    families = {
        "original": [{"text": "quiero volver a casa"}],
        "slow": [{"text": "quiero volver a casa"}],
        "pitch": [{"text": "quiero volar a casa"}],
    }
    winner, scores = whisper_family_medoid(families)
    assert winner in {"original", "slow"}
    assert scores[winner] > scores["pitch"]
    assert independent_family_agreement(families[winner], [{"text": "quiero volver a casa"}])["verified"]
    assert not independent_family_agreement(families[winner], [{"text": "texto completamente distinto"}])["verified"]
