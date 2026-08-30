from eval.canonical import write_json
from eval.code_switching import _finalize_lid_case, _language_persistence, _lexical_language, _owned_segments, _usable_lid_text, decode_windows, summarize


def test_overlap_fusion_gives_each_segment_to_one_chunk():
    chunks = [
        {"start_s": 0, "end_s": 8, "segments": [
            {"start": 1, "end": 2, "text": "uno"},
            {"start": 7.4, "end": 7.8, "text": "duplicado"},
        ]},
        {"start_s": 7, "end_s": 15, "segments": [
            {"start": .2, "end": .6, "text": "duplicado"},
            {"start": 2, "end": 3, "text": "two"},
        ]},
    ]
    result = _owned_segments(chunks)
    assert [row["text"] for row in result].count("duplicado") == 1
    assert [row["text"] for row in result] == ["uno", "duplicado", "two"]


def test_lexical_confirmation_rejects_nonlexical_vocalization():
    assert _lexical_language("oh oh oh") == (None, 0.0)
    assert _lexical_language("I want to run away")[0] == "en"
    assert _lexical_language("quiero volver a casa")[0] == "es"


def test_language_switch_requires_persistence_not_two_isolated_false_chunks():
    isolated = [
        {"start_s": index * 8, "end_s": index * 8 + 8, "confirmed_language": "en" if index in {2, 9} else "es", "lid_lexical_words": 5}
        for index in range(12)
    ]
    assert _language_persistence(isolated)["persistent"] == {"es"}
    real_switch = [
        {"start_s": index * 8, "end_s": index * 8 + 8, "confirmed_language": "en" if index in {2, 5, 9} else "es", "lid_lexical_words": 8}
        for index in range(12)
    ]
    assert _language_persistence(real_switch)["persistent"] == {"es", "en"}


def test_mix_can_route_uncertain_but_cannot_activate_code_switch_decoder():
    chunks = [
        {"start_s": index * 8, "end_s": index * 8 + 8, "confirmed_language": "en" if index < 3 else "es", "forced_text_en": "we want to go home", "forced_text_es": "queremos volver a casa"}
        for index in range(6)
    ]
    mix = _finalize_lid_case({"input_source": "original_mix_fallback", "chunks": chunks})
    assert mix["mix_code_switch_candidate"]
    assert not mix["is_es_en_code_switch"]
    stem = _finalize_lid_case({"input_source": "full_vocal_stem", "chunks": chunks})
    assert stem["is_es_en_code_switch"]


def test_decode_uses_long_context_and_abstains_on_mixed_vote():
    lid = {"chunks": [
        {"start_s": 0, "end_s": 8, "confirmed_language": "es"},
        {"start_s": 8, "end_s": 16, "confirmed_language": "en"},
        {"start_s": 16, "end_s": 24, "confirmed_language": "es"},
        {"start_s": 24, "end_s": 32, "confirmed_language": "en"},
    ]}
    windows = decode_windows(lid)
    assert windows[0]["end_s"] - windows[0]["start_s"] == 24
    assert windows[0]["confirmed_language"] is None


def test_lid_rejects_subtitle_boilerplate_and_runaway_repetition():
    assert not _usable_lid_text("Thank you")[0]
    assert not _usable_lid_text("run away " * 30)[0]
    assert not _usable_lid_text("we are going to dinner and we are going to dinner " * 12)[0]
    assert _usable_lid_text("I want to go back home tonight")[0]


def test_negative_single_song_pilot_is_not_mislabeled_as_promising(tmp_path):
    write_json(tmp_path / "variant.json", {
        "spanglish_songs": 1,
        "gate": {"status": "BLOCKED_INSUFFICIENT_SPANGLISH_GOLD"},
        "lid_against_human_text_labels": {"false_positive_code_switch_routes": []},
        "cases": [{
            "spanglish": True,
            "baseline": {"word_edits": 2, "reference_words": 100},
            "candidate": {"word_edits": 10, "reference_words": 100},
        }],
    })
    report = summarize(tmp_path, tmp_path / "report.json")
    assert report["gate"]["status"] == "NO_GO_PILOT_AND_INSUFFICIENT_GOLD"
