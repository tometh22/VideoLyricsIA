from eval.difficulty_router import choose_threshold


def test_live_and_code_switch_ambiguity_always_route_heavy():
    rows = [
        {"song_id": "hard", "label_difficult": 1, "is_live": 0, "lid_missing": 0, "lid_ambiguous_fraction": 0, "oof_probability": .9},
        {"song_id": "live", "label_difficult": 1, "is_live": 1, "lid_missing": 0, "lid_ambiguous_fraction": 0, "oof_probability": .01},
        {"song_id": "uncertain", "label_difficult": 0, "is_live": 0, "lid_missing": 0, "lid_mixed": 1, "lid_ambiguous_fraction": .5, "oof_probability": .01},
        {"song_id": "easy", "label_difficult": 0, "is_live": 0, "lid_missing": 0, "lid_ambiguous_fraction": 0, "oof_probability": .01},
    ]
    result = choose_threshold(rows, 1.0)
    assert set(result["song_ids"]) == {"hard", "live", "uncertain"}
