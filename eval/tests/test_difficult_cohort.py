from eval.difficult_cohort import _active_minutes, text_language_profile


def test_text_profile_distinguishes_code_switch_from_all_english():
    mixed = text_language_profile([
        {"text": "Quiero quedarme contigo"}, {"text": "Nunca te vayas de aquí"},
        {"text": "I want to run away"}, {"text": "You are with me tonight"},
    ])
    english = text_language_profile([
        {"text": "I want to run away"}, {"text": "You are with me tonight"},
        {"text": "We have a reason"},
    ])
    assert mixed["is_es_en_code_switch"] is True
    assert english["is_es_en_code_switch"] is False


def test_active_time_never_counts_overnight_gap():
    rows = [
        {"created_at": "2026-01-01T10:00:00+00:00"},
        {"created_at": "2026-01-01T10:01:00+00:00"},
        {"created_at": "2026-01-02T10:01:00+00:00"},
    ]
    result = _active_minutes(rows)
    assert result["active_minutes_proxy"] == 1.5
    assert result["compact_session_minutes"] is None
