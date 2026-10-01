from types import SimpleNamespace

import pytest

from campaign_search import matches_song


@pytest.mark.parametrize("query,expected", [
    ("garcia corazon", True), (" CORAZÓN   charly ", True),
    ("arf 001", True), ("arum-123", True), ("vivo cancion", True),
    ("abc123def456", True), ("", True), ("master.wav", True),
    ("garcia divididos", False), ("arum999", False), ("Canción inexistente", False),
])
def test_metadata_search(query, expected):
    item = SimpleNamespace(id="item", title="Canción del corazón (En Vivo)", artist="Charly García",
                           filename="ARF_001-master.wav", technical_code="ARUM-123")
    job = SimpleNamespace(job_id="abc123def456", song_title=item.title, artist=item.artist)
    assert matches_song(query, item, job) is expected
