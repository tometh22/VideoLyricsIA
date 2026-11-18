"""Artista y título canónicos para UMG (pedidos de sep-nov 2026)."""
from types import SimpleNamespace

import pytest

import umg_metadata
from umg_metadata import Learned, normalize


@pytest.mark.parametrize("artist,title,expected", [
    ("Charly Garcia", "V.S.D.", ("Charly García", "V.S.D.")),
    ("CHARLY GARCIA", "Influenza", ("Charly García", "Influenza")),
    ("Seru Giran", "En La Vereda Del Sol", ("Serú Girán", "En La Vereda Del Sol")),
    ("Enanitos Verdes", "Tibio Corazon", ("Los Enanitos Verdes", "Tibio Corazon")),
    ("LOS ENANITOS VERDES", "Celdas", ("Los Enanitos Verdes", "Celdas")),
    ("Coti - Ft. Enrique Iglesias", "¿Dónde Están Corazón?", ("Coti Ft. Enrique Iglesias", "¿Dónde Están Corazón?")),
    ("Coti", "¿Dónde Están Corazón?", ("Coti Ft. Enrique Iglesias", "¿Dónde Están Corazón?")),
    ("Enrique Iglesias", "Donde Estan Corazon", ("Coti Ft. Enrique Iglesias", "Donde Estan Corazon")),
    ("Divididos", "Zamba Por Vos", ("Mercedes Sosa", "Zamba Por Vos")),
    ("Bersuit Vergarabat", "Tuyo", ("Bersuit Vergarabat", "Tuyu")),
    ("Illya Kuryaki And The Valderramas", "Guerrilla Sexual", ("Illya Kuryaki And The Valderramas", "Guerrilla Sexua")),
    ("Saiko", "Cuando Miro a Tus Ojos", ("Saiko", "Cuando Miro En Tus Ojos")),
    ("Congreso", "Donde Estaras", ("Congreso", "¿Dónde Estarás?")),
    ("Congreso", "Heroína De New York", ("Congreso", "Heroína De Nueva York")),
    ("Los Prisioneros", "Porque Los Ricos", ("Los Prisioneros", "Por Qué Los Ricos")),
    ("Mercedes Sosa, Ariel Ramirez", "La Anunciación", ("Mercedes Sosa, Ariel Ramírez", "La Anunciación")),
    ("Fito Paez & Spinetta", "Parte Del Aire", ("Fito Páez & Spinetta", "Parte Del Aire")),
    ("Coti feat. Fito Paez", "Andar Conmigo", ("Coti Feat. Fito Páez", "Andar Conmigo")),
])
def test_normalize_known_umg_corrections(artist, title, expected):
    result = normalize(artist, title)
    assert (result.artist, result.title) == expected
    assert result.changed


def test_unknown_names_and_other_versions_are_left_alone():
    for artist, title in (("Banda Nueva", "Canción"), ("Coti", "¿Dónde Están Corazón? - En Vivo"),
                          ("Charly García", "Demoliendo Hoteles")):
        result = normalize(artist, title)
        assert (result.artist, result.title) == (artist, title)
        assert not result.changed


def test_double_spaces_are_cleaned_but_marked_as_whitespace():
    result = normalize("Bersuit Vergarabat", "Yo Tomo (Live In Buenos Aires  2001)")
    assert result.title == "Yo Tomo (Live In Buenos Aires 2001)"
    assert [c["reason"] for c in result.changes] == ["whitespace"]


def test_learned_spelling_applies_to_every_song_of_the_artist():
    artists, songs = {}, {}
    umg_metadata._learn(artists, songs, field_name="artist", before="Bambu", after="BAMBÚ",
                        artist="Bambu", title="Tema 1")
    learned = Learned(artists=artists, songs=songs)
    assert normalize("Bambu", "Otro Tema", learned=learned).artist == "Bambú"


def test_learned_different_artist_applies_only_to_that_song():
    artists, songs = {}, {}
    umg_metadata._learn(artists, songs, field_name="artist", before="Banda X",
                        after="Solista Y", artist="Banda X", title="Zamba")
    learned = Learned(artists=artists, songs=songs)
    assert normalize("Banda X", "Zamba", learned=learned).artist == "Solista Y"
    assert normalize("Banda X", "Otra", learned=learned).artist == "Banda X"


def test_learned_title_correction():
    artists, songs = {}, {}
    umg_metadata._learn(artists, songs, field_name="song_title", before="Tema Viejo",
                        after="Tema Nuevo", artist="Banda", title="Tema Viejo")
    learned = Learned(artists=artists, songs=songs)
    assert normalize("Banda", "tema viejo", learned=learned).title == "Tema Nuevo"


def test_learned_corrections_read_from_recorded_events(db):
    job = SimpleNamespace(job_id="abc123def456", tenant_id="universal_music",
                          artist="Bambu", song_title="Tema")
    umg_metadata.record_correction(db, job, field_name="artist", before="Bambu", after="Bambú")
    db.flush()
    umg_metadata.invalidate_learned()
    learned = umg_metadata.learned_corrections(db)
    assert normalize("BAMBU", "Otro", learned=learned).artist == "Bambú"
    db.rollback()
    umg_metadata.invalidate_learned()


def _job(**kw):
    base = dict(job_id="j1", artist="Charly Garcia", song_title="V.S.D.", campaign_id="c1",
                workload_class="batch", delivery_profile="youtube", tenant_id="t")
    base.update(kw)
    return SimpleNamespace(**base)


def test_normalize_job_only_for_umg_jobs(monkeypatch):
    monkeypatch.setattr(umg_metadata, "learned_corrections", lambda db: Learned())
    job = _job()
    assert umg_metadata.normalize_job(None, job).artist == "Charly García"
    assert job.artist == "Charly García"
    other = _job(campaign_id=None, workload_class="interactive")
    assert umg_metadata.normalize_job(None, other) is None
    assert other.artist == "Charly Garcia"


def test_publication_gate(monkeypatch):
    monkeypatch.setattr(umg_metadata, "learned_corrections", lambda db: Learned())
    monkeypatch.delenv("UMG_METADATA_GATE", raising=False)
    problem = umg_metadata.publication_problem(None, _job())
    assert problem["code"] == "metadata_not_canonical"
    assert problem["expected_artist"] == "Charly García"
    assert "Charly García" in problem["message"]
    assert umg_metadata.publication_problem(None, _job(artist="Charly García")) is None
    # Espacios dobles no se ven en la tarjeta: no frenan.
    assert umg_metadata.publication_problem(None, _job(artist="Charly  García")) is None
    monkeypatch.setenv("UMG_METADATA_GATE", "observe")
    assert umg_metadata.publication_problem(None, _job()) is None


def test_request_already_met_by_canonical_name():
    assert umg_metadata.matches_request("Los Enanitos Verdes", "LOS ENANITOS VERDES")
    assert not umg_metadata.matches_request("Charly Garcia", "Charly García")


def test_catalog_has_no_conflicting_aliases():
    import json
    raw = json.loads(umg_metadata.CATALOG_PATH.read_text(encoding="utf-8"))
    seen = {}
    for row in raw["artists"]:
        for name in [row["name"], *(row.get("aliases") or [])]:
            k = umg_metadata.key(name)
            assert seen.setdefault(k, row["name"]) == row["name"], name
