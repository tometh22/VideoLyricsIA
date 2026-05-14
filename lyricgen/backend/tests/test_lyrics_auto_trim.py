"""_trim_segments_to_voice_end — caps segment end at estimated voice-end.

Driven by V2 formula: max(3.5, len(text)*0.10 + 1.0). Validates against
the "De La Guitarra" prod incident shape: lines with long fills/outros
where LRCLib pinned `end` to the start of the next line.
"""

from pipeline import (
    _trim_segments_to_voice_end,
    _estimate_voice_end_duration,
)


def test_estimate_voice_end_floor():
    # Short text → floor at 3.5s
    assert _estimate_voice_end_duration("Sí") == 3.5
    assert _estimate_voice_end_duration("Oh oh") == 3.5


def test_estimate_voice_end_typical_line():
    # 40-char line → 40*0.10 + 1.0 = 5.0s (above floor)
    text = "Solía verla siempre temprano en las maña"  # 40 chars
    assert abs(_estimate_voice_end_duration(text) - 5.0) < 0.01


def test_trim_short_segment_untouched():
    """A 4s line with text long enough that cap > 4s stays as-is."""
    segs = [{"start": 0.0, "end": 4.0, "text": "Cantando bajo la lluvia que cae"}]
    out, trimmed, recovered = _trim_segments_to_voice_end(segs)
    assert trimmed == 0
    assert recovered == 0.0
    assert out[0]["end"] == 4.0


def test_trim_long_segment_capped():
    """The De La Guitarra outro line: 62s of `end` for "Está canción que escuchas"."""
    segs = [{"start": 250.0, "end": 312.7, "text": "Está canción que escuchas"}]
    out, trimmed, recovered = _trim_segments_to_voice_end(segs)
    assert trimmed == 1
    # cap = max(3.5, 25*0.10 + 1.0) = max(3.5, 3.5) = 3.5
    assert abs(out[0]["end"] - (250.0 + 3.5)) < 0.01
    assert recovered > 59.0


def test_trim_preserves_other_fields():
    """Trim must not drop word_timestamps or other extra keys."""
    segs = [{
        "start": 0.0, "end": 30.0, "text": "Hola mundo",
        "words": [{"word": "Hola", "start": 0.0, "end": 0.4}],
        "extra_field": "kept",
    }]
    out, trimmed, _ = _trim_segments_to_voice_end(segs)
    assert trimmed == 1
    assert out[0]["words"] == segs[0]["words"]
    assert out[0]["extra_field"] == "kept"


def test_trim_empty_text_skipped():
    """Empty text → no trim (would otherwise floor at 3.5s arbitrarily)."""
    segs = [{"start": 0.0, "end": 10.0, "text": ""}]
    out, trimmed, _ = _trim_segments_to_voice_end(segs)
    assert trimmed == 0
    assert out[0]["end"] == 10.0


def test_trim_invalid_end_skipped():
    """end <= start should not crash."""
    segs = [
        {"start": 5.0, "end": 5.0, "text": "zero duration"},
        {"start": 10.0, "end": 8.0, "text": "negative duration"},
    ]
    out, trimmed, _ = _trim_segments_to_voice_end(segs)
    assert trimmed == 0
    assert out[0]["end"] == 5.0
    assert out[1]["end"] == 8.0


def test_trim_de_la_guitarra_fixture():
    """End-to-end on the prod 'De La Guitarra' shape (29 lines, 15 long).
    Confirms aggregate behavior: ~17 lines trimmed, ~100s recovered.
    Numbers approximate because we use a subset of the actual segments.
    """
    segs = [
        {"start": 31.81, "end": 37.57, "text": "Solía verla siempre temprano en las mañanas"},
        {"start": 37.62, "end": 41.29, "text": "Tomaba el mismo tren que yo"},
        {"start": 52.71, "end": 63.80, "text": "Y me llamaba la atención de la forma que llevaba la guitarra"},  # 11s, long
        {"start": 69.48, "end": 92.68, "text": "Las estaciones triste la vieron pasar"},  # 23s, very long
        {"start": 250.06, "end": 312.79, "text": "Está canción que escuchas"},  # outro, 62s
    ]
    out, trimmed, recovered = _trim_segments_to_voice_end(segs)
    assert trimmed >= 3, f"expected at least 3 trims, got {trimmed}"
    assert recovered > 70.0, f"expected >70s recovered, got {recovered:.1f}"
    # The outro must be capped close to text-length estimate
    outro = out[-1]
    assert outro["end"] - outro["start"] < 4.5  # 25 chars * 0.1 + 1 = 3.5
