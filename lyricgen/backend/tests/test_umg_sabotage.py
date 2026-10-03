import pytest

from delivery_preflight import build_delivery_preflight
from delivery_qc_runtime import MANUAL_ATTESTATION_CODE, MANDATORY_REVIEW_CHECKS, mandatory_reviewer_issues


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
def test_each_sabotage_case_is_covered_by_one_signed_visual_review(case, expected_code):
    legacy_checks = {code for code, _summary, _description in MANDATORY_REVIEW_CHECKS}
    assert expected_code in legacy_checks
    issues = mandatory_reviewer_issues()
    assert len(issues) == 1
    result = issues[0]
    assert case
    assert result["code"] == MANUAL_ATTESTATION_CODE
    assert result["result_status"] == "REVIEW"
    assert result["severity"] == "WARN"
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
