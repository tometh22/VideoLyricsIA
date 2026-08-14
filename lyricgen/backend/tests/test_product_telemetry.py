import math

from product_telemetry import valid_property


def test_numeric_metrics_cannot_store_lyrics_or_poison_percentiles():
    assert valid_property("text_changes", 3)
    assert not valid_property("text_changes", "Hoy temprano estuve pensando en vos")
    assert not valid_property("duration_ms", -1)
    assert not valid_property("duration_ms", math.nan)
    assert not valid_property("duration_ms", 100_000_000)
    assert not valid_property("active_edit_ms", 14_400_001)
    assert not valid_property("text_changes", 10_001)
    assert not valid_property("status", 600)


def test_session_and_boolean_types_are_strict():
    assert valid_property("session_id", "019abcde-1234-4567-8901-abcdefabcdef")
    assert not valid_property("session_id", "lyric text with spaces")
    assert not valid_property("session_id", "subtitulos-realizados")
    assert valid_property("session_id", "editor-1786674137720-abc123")
    assert valid_property("quality_acknowledged", True)
    assert not valid_property("quality_acknowledged", 1)


def test_categories_are_enums_or_short_machine_codes_only():
    assert valid_property("view", "advanced")
    assert not valid_property("view", "Hoy temprano estuve pensando")
    assert valid_property("reason", "stale_revision")
    assert not valid_property("reason", "letra libre con espacios")
    assert not valid_property("unknown", "anything")
