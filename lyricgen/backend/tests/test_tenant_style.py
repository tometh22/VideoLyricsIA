"""Contract for the per-account style profile.

Guards the three properties that make this safe to turn on for a live
account:

  1. The strip only touches sentence-final punctuation on lyric lines.
  2. `segments_json` never sees the stripped text (the editor must keep
     real punctuation, and run_edit_pipeline re-persists segments after
     rendering — rebinding there would corrupt the stored lyrics).
  3. An explicit operator choice always beats the account default.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import tenant_style  # noqa: E402
from tenant_style import (  # noqa: E402
    StyleProfileError,
    normalize_style_profile,
    strip_trailing_punctuation,
)


# --- strip_trailing_punctuation ------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    # The literal complaints UMG filed.
    ("Costumbres argentinas.", "Costumbres argentinas"),
    ("No hay nadie mas que vos y yo,", "No hay nadie mas que vos y yo"),
    ("Y sigue el tren...", "Y sigue el tren"),
    ("Y sigue el tren…", "Y sigue el tren"),
    ("Escuchame bien:", "Escuchame bien"),
    ("Dime una cosa;", "Dime una cosa"),
    # Untouched: these carry meaning nobody asked us to drop.
    ("¿Quien te mira?", "¿Quien te mira?"),
    ("¡La argentinidad al palo!", "¡La argentinidad al palo!"),
    # Mid-line punctuation stays — "No, no puedo" reads correctly.
    ("No, no puedo mas.", "No, no puedo mas"),
    # Nothing to do.
    ("Mil horas", "Mil horas"),
    ("", ""),
])
def test_strip_solo_toca_el_final(raw, expected):
    assert strip_trailing_punctuation(raw) == expected


def test_strip_preserva_espacios_finales():
    # Trailing whitespace is layout, not punctuation — the renderer's own
    # centering depends on the string it gets.
    assert strip_trailing_punctuation("Mil horas.   ") == "Mil horas   "


@pytest.mark.parametrize("raw", ["...", "…", ".", "   ", ",,,"])
def test_strip_no_vacia_una_linea(raw):
    # A stylistic "..." card must not silently disappear.
    assert strip_trailing_punctuation(raw) == raw


# --- normalize_style_profile ---------------------------------------------

def test_normalize_es_sparse():
    # Absent keys mean "no opinion" — they must NOT come back defaulted,
    # or every account row would start overriding platform behaviour it
    # never asked about.
    assert normalize_style_profile({"font_scale": 1.3}) == {"font_scale": 1.3}
    assert normalize_style_profile(None) == {}
    assert normalize_style_profile("") == {}


def test_normalize_acepta_json_string():
    assert normalize_style_profile('{"strip_trailing_punctuation": true}') == {
        "strip_trailing_punctuation": True,
    }


@pytest.mark.parametrize("bad", [
    {"font_scale": 9.0},          # fuera del clamp del render
    {"font_scale": 0.1},
    {"font_scale": "grande"},
    {"unknown_key": 1},
    "no-json",
    [1, 2],
])
def test_normalize_rechaza_basura(bad):
    with pytest.raises(StyleProfileError):
        normalize_style_profile(bad)


def test_normalize_font_scale_en_los_bordes():
    assert normalize_style_profile({"font_scale": 0.6}) == {"font_scale": 0.6}
    assert normalize_style_profile({"font_scale": 1.5}) == {"font_scale": 1.5}


# --- _display_segments (the render boundary) ------------------------------

def _display(segments, profile):
    from pipeline import _display_segments
    return _display_segments(segments, profile)


def test_display_segments_no_muta_el_original():
    """El contrato que protege segments_json.

    run_edit_pipeline persiste `segments` DESPUÉS de renderizar cuando el
    edit es de letra. Si el strip mutara la lista original (o la
    rebindeara), esa escritura guardaría el texto sin puntos y el editor
    del operador perdería la puntuación para siempre.
    """
    original = [{"start": 0.0, "end": 1.0, "text": "Mil horas."}]
    out = _display(original, {"strip_trailing_punctuation": True})
    assert out[0]["text"] == "Mil horas"
    assert original[0]["text"] == "Mil horas.", "mutó la lista original"
    assert out is not original


def test_display_segments_sin_perfil_devuelve_lo_mismo():
    segs = [{"text": "Mil horas."}]
    assert _display(segs, None) is segs
    assert _display(segs, {}) is segs
    assert _display(segs, {"font_scale": 1.3}) is segs


def test_display_segments_preserva_los_otros_campos():
    segs = [{"start": 1.5, "end": 3.0, "text": "Mil horas.",
             "words": [{"word": "Mil"}], "ctc_lr": 0.9}]
    out = _display(segs, {"strip_trailing_punctuation": True})
    assert out[0]["start"] == 1.5
    assert out[0]["end"] == 3.0
    assert out[0]["ctc_lr"] == 0.9
    assert out[0]["words"] == [{"word": "Mil"}]


def test_display_segments_tolera_texto_no_string():
    segs = [{"text": None}, {"text": 42}, {"no_text": 1}]
    out = _display(segs, {"strip_trailing_punctuation": True})
    assert len(out) == 3


# --- _effective_font_scale (explicit choice wins) -------------------------

def _eff(raw, profile):
    from main import _effective_font_scale
    return _effective_font_scale(raw, profile)


def test_font_scale_vacio_usa_el_default_de_cuenta():
    assert _eff("", {"font_scale": 1.3}) == 1.3
    assert _eff(None, {"font_scale": 1.3}) == 1.3
    assert _eff("   ", {"font_scale": 1.3}) == 1.3


def test_font_scale_explicito_gana_incluso_si_es_el_default_de_plataforma():
    # El operador tiene que poder renderizar un job de UMG en 1.0 a propósito.
    assert _eff("1.0", {"font_scale": 1.3}) == 1.0
    assert _eff("1.15", {"font_scale": 1.3}) == 1.15


def test_font_scale_sin_perfil_cae_en_1():
    assert _eff("", {}) == 1.0
    assert _eff(None, {}) == 1.0


def test_font_scale_clampea_igual_que_el_render():
    assert _eff("9.0", {}) == 1.5
    assert _eff("0.1", {}) == 0.6
    # Un perfil corrupto que se coló por una escritura directa a la DB no
    # debe poder empujar el render fuera del clamp.
    assert _eff("", {"font_scale": 9.0}) == 1.5
    assert _eff("", {"font_scale": "grande"}) == 1.0


def test_font_scale_basura_explicita_cae_en_el_perfil():
    assert _eff("abc", {"font_scale": 1.3}) == 1.3


# --- resolve precedence ---------------------------------------------------

class _FakeRow:
    def __init__(self, scope, scope_key, profile):
        self.scope, self.scope_key, self.profile = scope, scope_key, profile


class _FakeQuery:
    def __init__(self, rows):
        self._rows = rows

    def filter(self, *a, **k):
        return self

    def all(self):
        return self._rows


class _FakeDB:
    def __init__(self, rows):
        self._rows = rows

    def query(self, *a, **k):
        return _FakeQuery(self._rows)


def test_resolve_prefiere_tenant_sobre_billing_group():
    db = _FakeDB([
        _FakeRow("billing_group", "universal_music", {"font_scale": 1.0}),
        _FakeRow("tenant", "universal_chile", {"font_scale": 1.3}),
    ])
    got = tenant_style.resolve_style_profile(
        db, tenant_id="universal_chile", billing_group="universal_music",
    )
    assert got == {"font_scale": 1.3}


def test_resolve_cae_al_billing_group():
    # Un tenant nuevo de Universal sin fila propia hereda la del grupo —
    # que es justamente por qué el scope existe (UMG_TENANTS son cinco).
    db = _FakeDB([
        _FakeRow("billing_group", "universal_music",
                 {"strip_trailing_punctuation": True}),
    ])
    got = tenant_style.resolve_style_profile(
        db, tenant_id="universal_mexico", billing_group="universal_music",
    )
    assert got == {"strip_trailing_punctuation": True}


def test_resolve_sin_filas_es_vacio():
    assert tenant_style.resolve_style_profile(_FakeDB([]), tenant_id="pepe") == {}


def test_resolve_ignora_una_fila_corrupta():
    # Una preferencia mal escrita no puede tumbar un upload.
    db = _FakeDB([_FakeRow("tenant", "pepe", {"font_scale": 99})])
    assert tenant_style.resolve_style_profile(db, tenant_id="pepe") == {}


def test_resolve_sobrevive_un_error_de_db():
    class _Boom:
        def query(self, *a, **k):
            raise RuntimeError("pool agotado")

    assert tenant_style.resolve_style_profile(_Boom(), tenant_id="pepe") == {}


# --- quality_json snapshot -----------------------------------------------

def _snapshot(result):
    from transcription_worker import _quality_snapshot
    return _quality_snapshot(result)


def test_quality_snapshot_none_cuando_no_se_midio():
    """NULL en la columna tiene que significar "no se midió", no "midió 0".

    Sin ASR words no hay `coverage_final`; persistir ceros ahí haría que un
    gate futuro leyera 0% de cobertura sobre un job perfectamente sano.
    """
    assert _snapshot({}) is None
    assert _snapshot({"postpass_stats": {}}) is None
    assert _snapshot(None) is None
    assert _snapshot("no soy un dict") is None


def test_quality_snapshot_extrae_las_metricas():
    r = {
        "coverage_warning": True,
        "timing_source": "ctc_align",
        "postpass_stats": {"coverage_final": {
            "audio_coverage": 0.76,
            "uncovered_spans": 3,
            "uncovered_seconds": 12.5,
            "worst_span_s": 8.1,
            "text_mismatches": 2,
            "voiced_gaps": 1,
            "voiced_gap_s": 14.0,
        }},
    }
    got = _snapshot(r)
    assert got["audio_coverage"] == 0.76
    assert got["text_mismatches"] == 2
    assert got["voiced_gap_s"] == 14.0
    assert got["coverage_warning"] is True
    assert got["timing_source"] == "ctc_align"


def test_quality_snapshot_omite_las_claves_ausentes():
    # Una versión vieja de summarize() no debe meter nulls en la columna.
    got = _snapshot({"postpass_stats": {"coverage_final": {"audio_coverage": 1.0}}})
    assert got == {"audio_coverage": 1.0, "coverage_warning": False}
