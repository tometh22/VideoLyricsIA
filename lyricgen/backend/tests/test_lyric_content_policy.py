from lyric_content_policy import classify_content, should_include_as_lyric


def test_metadata_and_spoken_outro_are_not_lyrics():
    assert classify_content("CC por Antarctica Films Argentina") == "METADATA"
    assert not should_include_as_lyric("Lyrics: Example")
    assert not should_include_as_lyric("¡Gracias!", isolated_tail=True)
    for credit in (
        "Subtitles by John", "Subtitulado por Juan", "Créditos: Juan",
        "Credits: John", "Subtitles: John", "Subtitles created by John",
    ):
        assert classify_content(credit, provider_kind="sung") == "METADATA"
        assert not should_include_as_lyric(credit, provider_kind="sung")


def test_sung_crowd_and_vocalizations_remain_lyrics():
    assert should_include_as_lyric("Real, uoh uoh", provider_kind="sung_crowd")
    assert should_include_as_lyric("Nooooo", provider_kind="sustained")


def test_chatter_word_is_retained_when_acoustic_role_says_it_is_sung():
    assert classify_content("¡Gracias!", provider_kind="sung") == "SUNG_LEAD"
    assert should_include_as_lyric(
        "¡Gracias!", provider_kind="sung", isolated_tail=True,
    )
    assert classify_content("¡Gracias!") == "SPEECH_CANDIDATE"
    assert should_include_as_lyric("¡Gracias!", isolated_tail=False)
