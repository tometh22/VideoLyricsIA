from eval.autopsy import word_edit_operations
from eval.bootstrap import song_bootstrap_ci


def test_word_edit_operation_types():
    edits = word_edit_operations("yo quiero despegar", "yo puedo despegar hoy")
    assert [edit["type"] for edit in edits] == ["substitution", "insertion"]
    assert edits[0]["ref_word"] == "quiero"
    assert edits[0]["hyp_word"] == "puedo"


def test_song_bootstrap_is_deterministic_and_song_blocked():
    songs = [{"errors": 1, "words": 10}, {"errors": 4, "words": 10}]
    statistic = lambda sample: sum(row["errors"] for row in sample) / sum(row["words"] for row in sample)
    first = song_bootstrap_ci(songs, statistic, iterations=100)
    second = song_bootstrap_ci(songs, statistic, iterations=100)
    assert first == second
    assert first["estimate"] == 0.25
    assert first["unit"] == "song"
