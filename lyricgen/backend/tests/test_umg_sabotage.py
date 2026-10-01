import pytest

from delivery_preflight import build_delivery_preflight
from delivery_qc_runtime import mandatory_reviewer_issues


SABOTAGE_CASES = {
    "franjas_negras": "UMG_BLACK_BARS",
    "texto_en_fondo": "UMG_BACKGROUND_TEXT",
    "cambio_de_escena": "UMG_SCENE_CHANGE",
    "salto_de_luminancia": "UMG_LUMINANCE_STABLE",
    "contraste_bajo": "UMG_MOBILE_CONTRAST",
    "linea_entra_tarde": "UMG_LYRIC_NOT_LATE",
    "titulo_distinto_metadata": "UMG_TITLE_METADATA",
    "imagen_fija_estirada": "UMG_IMAGE_NOT_STRETCHED",
}


@pytest.mark.parametrize(("case", "expected_code"), SABOTAGE_CASES.items())
def test_each_unsigned_sabotage_case_is_a_real_fail(case, expected_code):
    issues = {row["code"]: row for row in mandatory_reviewer_issues()}
    result = issues[expected_code]
    assert case
    assert result["severity"] == "FAIL"
    assert result["manual_verification_required"] is True
    assert result["detector"] == "mandatory_signed_reviewer_checklist"


def test_title_mismatch_also_fails_automatically_when_ocr_is_available():
    report = build_delivery_preflight(
        metadata={"artist": "Artista", "title": "Título correcto"},
        segments=[{"segment_id": "one", "start": 0, "end": 2, "text": "Hola"}],
        asset={
            "duration": 2,
            "rendered_title": "Título saboteado",
            "rendered_artist": "Artista",
        },
    )
    issue = next(row for row in report["issues"] if row["code"] == "METADATA_TITLE_MISMATCH")
    assert issue["severity"] == "FAIL"
    assert report["decision"] == "BLOCK"
