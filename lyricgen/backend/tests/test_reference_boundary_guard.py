from lyric_review import build_review
from reference_lines import crossed_reference_boundaries


def _segments(*texts):
    return [{"start": i * 2, "end": i * 2 + 1, "text": text} for i, text in enumerate(texts)]


REFERENCE = "Hoy volvi por el camino\nCantando en la ciudad"


def test_partial_reference_flags_unique_crossing_without_editing_later_song():
    before = _segments("Hoy volví", "por el camino cantando en la", "ciudad", "Otro puente en inglés")
    after = _segments("Hoy volví por el camino", "Cantando en la ciudad", "Otro puente en inglés")
    assert crossed_reference_boundaries(before, REFERENCE) == [1]
    assert crossed_reference_boundaries(after, REFERENCE) == []

    find_crossings = lambda review: [item for item in review["items"] if item["kind"] == "reference_boundary"]
    found = find_crossings(build_review(before, official_text=REFERENCE, official_origin="sheet", portal="chile"))
    assert len(found) == 1
    assert found[0]["listen"] is True
    assert found[0]["occurrences"][0]["fix"]["type"] == "inspect_structure"
    assert not find_crossings(build_review(before, official_text=REFERENCE, official_origin="lrclib", portal="chile"))


def test_repeated_pair_and_version_mismatch_are_ignored():
    merged = "Hoy volví por el camino cantando en la ciudad"
    assert crossed_reference_boundaries(_segments(merged, merged), REFERENCE) == []
    assert crossed_reference_boundaries(_segments(merged), REFERENCE + "\n" + REFERENCE) == []
    assert crossed_reference_boundaries(_segments("Hoy salí por el camino cantando en la ciudad"), REFERENCE) == []
