"""Lead-in de aparición de líneas (lead_in.py).

Datos que motivan el módulo (prod 03/07): 94% de los 886 ajustes finos de
inicio de los operadores van hacia ANTES (mediana −0.41s); el sweep offline
mostró que 0.2–0.4s replica ese pulido (34.8% → ~49% de líneas ≤0.3s del
gold). El contrato clave: solo `start`, solo hacia antes, sin pisar la
línea anterior, y default apagado.
"""
import lead_in


def _segs():
    return [
        {"start": 10.0, "end": 12.0, "text": "a"},
        {"start": 14.0, "end": 16.0, "text": "b"},     # 2s de aire
        {"start": 16.05, "end": 18.0, "text": "c"},    # encadenada (0.05 de gap)
    ]


def test_default_off(monkeypatch):
    monkeypatch.delenv("LYRIC_LEAD_IN_S", raising=False)
    segs = _segs()
    assert lead_in.apply(segs) is segs  # no-op total con default 0


def test_shifts_starts_earlier_with_clamp():
    out = lead_in.apply(_segs(), lead_s=0.4)
    assert out[0]["start"] == 9.6                  # aire de sobra
    assert out[1]["start"] == 13.6                 # aire de sobra
    # encadenada: el clamp al end anterior (16.0 + gap) no deja lead útil,
    # y NUNCA se mueve hacia después.
    assert out[2]["start"] == 16.01
    # ends y textos intactos
    assert [s["end"] for s in out] == [12.0, 16.0, 18.0]


def test_never_moves_later_on_overlapping_input():
    segs = [
        {"start": 10.0, "end": 13.0, "text": "a"},
        {"start": 12.5, "end": 15.0, "text": "b"},   # ya solapada con `a`
    ]
    out = lead_in.apply(segs, lead_s=0.4)
    assert out[1]["start"] == 12.5  # el clamp caería después → no tocar


def test_clamps_at_zero():
    out = lead_in.apply([{"start": 0.2, "end": 2.0, "text": "a"}], lead_s=0.5)
    assert out[0]["start"] == 0.0


def test_words_untouched():
    segs = [{"start": 10.0, "end": 12.0, "text": "a",
             "words": [{"word": "a", "start": 10.0, "end": 10.4}]}]
    out = lead_in.apply(segs, lead_s=0.3)
    assert out[0]["start"] == 9.7
    assert out[0]["words"][0]["start"] == 10.0  # highlight sigue en el onset real


def test_env_parsing(monkeypatch):
    monkeypatch.setenv("LYRIC_LEAD_IN_S", "0.1")
    assert lead_in.lead_seconds() == 0.1
    monkeypatch.setenv("LYRIC_LEAD_IN_S", "-1")
    assert lead_in.lead_seconds() == 0.0
    monkeypatch.setenv("LYRIC_LEAD_IN_S", "banana")
    assert lead_in.lead_seconds() == 0.0


def test_bad_segment_passthrough():
    segs = [{"start": "x", "end": None, "text": "raro"},
            {"start": 5.0, "end": 7.0, "text": "ok"}]
    out = lead_in.apply(segs, lead_s=0.4)
    assert out[0]["start"] == "x"      # intacto
    assert out[1]["start"] < 5.0       # el sano igual se adelanta


# ── hold (LYRIC_HOLD_S) ──────────────────────────────────────────────────────

def test_hold_default_off(monkeypatch):
    monkeypatch.delenv("LYRIC_HOLD_S", raising=False)
    segs = _segs()
    assert lead_in.apply_hold(segs) is segs


def test_hold_extends_into_gap_with_cap():
    out = lead_in.apply_hold(_segs(), hold_s=0.25)
    assert out[0]["end"] == 12.25          # aire de sobra: end+0.25
    # línea 1 termina en 16.0 y la siguiente arranca 16.05: tope en 16.04
    assert out[1]["end"] == 16.04
    assert out[2]["end"] == 18.0           # última línea intacta
    assert [s["start"] for s in out] == [10.0, 14.0, 16.05]  # starts intactos


def test_hold_never_shortens():
    segs = [{"start": 1.0, "end": 5.0, "text": "a"},
            {"start": 4.0, "end": 6.0, "text": "b"}]  # ya solapadas
    out = lead_in.apply_hold(segs, hold_s=0.25)
    assert out[0]["end"] == 5.0            # tope (4.0-0.01) < end → no tocar


def test_polish_composes_lead_then_hold(monkeypatch):
    """El tope del hold usa el start YA adelantado: con lead activo, la
    línea siguiente arranca antes, y el hold no puede pisarla."""
    monkeypatch.setenv("LYRIC_LEAD_IN_S", "0.15")
    monkeypatch.setenv("LYRIC_HOLD_S", "0.25")
    segs = [{"start": 10.0, "end": 13.7, "text": "a"},
            {"start": 14.0, "end": 16.0, "text": "b"}]
    out = lead_in.polish(segs)
    assert out[1]["start"] == 13.85        # lead aplicado (14.0-0.15)
    assert out[0]["end"] == 13.84          # hold topeado en el start NUEVO
    assert out[0]["end"] < out[1]["start"]


def test_hold_env_parsing(monkeypatch):
    monkeypatch.setenv("LYRIC_HOLD_S", "0.25")
    assert lead_in.hold_seconds() == 0.25
    monkeypatch.setenv("LYRIC_HOLD_S", "nope")
    assert lead_in.hold_seconds() == 0.0


def test_lead_clampeado_al_tope(monkeypatch, caplog):
    """0.4 en staging fue el footgun: leads grandes se perciben como
    desincronización (ver _MAX_LEAD_S). Arriba del tope se clampea."""
    import lead_in
    monkeypatch.setenv("LYRIC_LEAD_IN_S", "0.4")
    assert lead_in.lead_seconds() == lead_in._MAX_LEAD_S


def test_lead_bajo_el_tope_pasa_intacto(monkeypatch):
    import lead_in
    monkeypatch.setenv("LYRIC_LEAD_IN_S", "0.08")
    assert lead_in.lead_seconds() == 0.08
