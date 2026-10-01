import zipfile
from xml.sax.saxutils import escape

import pytest

from shadow_reference_import import associate, availability, import_workbook


def workbook(tmp_path, *, headers=None, values=None, extra=None):
    path = tmp_path / "source.xlsx"
    headers = headers or {"G": "Artista", "D": "Track", "B": "Álbum"}
    values = values or {"G": "Artista", "D": "Canción", "B": "Disco", "Z": "Una línea\nOtra línea"}
    def row(number, cells):
        return f'<row r="{number}">' + ''.join(
            f'<c r="{col}{number}" t="inlineStr"><is><t>{escape(value)}</t></is></c>'
            for col, value in cells.items()) + '</row>'
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("xl/workbook.xml", '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="Lyrics Agosto" r:id="r1"/><sheet name="Art Tracks Agosto" r:id="r2"/></sheets></workbook>')
        z.writestr("xl/_rels/workbook.xml.rels", '<Relationships><Relationship Id="r1" Target="worksheets/s1.xml"/><Relationship Id="r2" Target="ignored.xml"/></Relationships>')
        z.writestr("xl/worksheets/s1.xml", '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>' + row(7, headers) + row(8, values) + (row(9, extra) if extra else '') + '</sheetData></worksheet>')
    return path


def test_import_discovers_columns_and_preserves_original(tmp_path):
    path = workbook(tmp_path)
    original = path.read_bytes()
    result = import_workbook(path)
    assert path.read_bytes() == original
    assert result["sheets"][0]["columns"]["lyrics"] == "Z"
    assert result["sheets"][0]["lyrics_header_inferred"]
    assert result["rows"][0]["lyrics"] == "Una línea\nOtra línea"
    assert result["rows"][0]["provenance"] == "unverified"
    assert result["rows"][0].get("retrieved_at") is None
    assert result["excluded_sheets"] == ["Art Tracks Agosto"]


@pytest.mark.parametrize("marker", ["[NO ENCONTRADA]", "No encontrado", "N/A", "sin letra"])
def test_markers_never_become_lyrics(tmp_path, marker):
    result = import_workbook(workbook(tmp_path, values={"G": "A", "D": "T", "Z": marker}))
    assert result["rows"][0]["lyrics"] is None
    assert result["rows"][0]["availability"] == "not_found"


def test_ambiguous_missing_header_fails_closed(tmp_path):
    path = workbook(tmp_path, values={"G": "A", "D": "T", "Z": "a\nb", "Y": "c\nd"})
    with pytest.raises(ValueError, match="Ambiguous"):
        import_workbook(path)


def test_matching_never_accepts_title_only_or_ambiguous_versions(tmp_path):
    result = import_workbook(workbook(tmp_path))
    associate(result, [{"job_id": "wrong", "artist": "Otro", "title": "Canción"}])
    assert result["rows"][0]["matched_job_id"] is None
    associate(result, [{"job_id": str(i), "artist": "Artista", "title": "Canción"} for i in range(2)])
    assert result["rows"][0]["association"] == "ambiguous"
    associate(result, [{"job_id": "a", "artist": "Artista", "title": "Canción", "album": "Otro disco"}])
    assert result["rows"][0]["association"] == "unmatched"


def test_duplicate_rows_are_not_autoaccepted(tmp_path):
    values = {"G": "A", "D": "T", "Z": "a\nb"}
    result = import_workbook(workbook(tmp_path, values=values, extra=values))
    associate(result, [{"job_id": "a", "artist": "A", "title": "T"}])
    assert all(r["association"] == "ambiguous_duplicate_rows" for r in result["rows"])


def test_availability_does_not_confuse_empty_invalid_missing():
    assert availability(123) == "invalid"
    assert availability("") == "empty"
    assert availability("¿Qué ves?") == "present"


def test_inline_source_pointer_kept_without_becoming_lyrics(tmp_path):
    result = import_workbook(workbook(tmp_path,
        headers={'G': 'Artista', 'D': 'Track', 'Z': 'Lyrics'},
        values={'G': 'A', 'D': 'T', 'Z': '[Musixmatch] https://example.test/song'}))
    row = result['rows'][0]
    assert row['availability'] == 'pointer_only'
    assert row['lyrics'] is None
    assert row['url'] == 'https://example.test/song'
