"""chorus_snap: fragmentos del coro reparados a la frase canónica.

Caso real (job ae6f1165, outro de "Rodando Por Ahí"): gap_rescue dejó
"Rodando por ahí, estuve rodando" y un órfano "Por ahí" donde el coro dice
"Estuve rodando por ahí". Rotor lo muestra limpio porque estampa la frase
del coro; esto hace lo mismo con la señal que ya teníamos (repetition_group).
"""
import pytest

import chorus_snap as cs


CANON = "Estuve rodando por ahí"


def _seg(texto, ini, fin, **extra):
    d = {"start": ini, "end": fin, "text": texto}
    d.update(extra)
    return d


def _coro(n=4, ini=190.0, paso=8.0, texto=CANON):
    """n repeticiones limpias del coro → forman el repetition_group."""
    return [_seg(texto, ini + k * paso, ini + k * paso + 3.0) for k in range(n)]


def test_repara_fragmento_del_coro():
    segs = _coro() + [_seg("Rodando por ahí, estuve rodando", 216.1, 222.0)]
    out, stats = cs.snap(segs)
    frag = [s for s in out if 216 <= s["start"] <= 217][0]
    assert frag["text"] == CANON
    assert frag.get("chorus_snapped") is True
    assert stats["snapped"] >= 1


def test_absorbe_el_orfano_corto():
    """'Por ahí' (2 palabras, 2.2s) pegado a un coro → se funde, no queda suelto."""
    segs = _coro() + [_seg("Rodando por ahí estuve", 216.1, 222.0),
                      _seg("Por ahí", 222.1, 224.3)]
    out, stats = cs.snap(segs)
    textos = [s["text"] for s in out]
    assert "Por ahí" not in textos, "el órfano debe absorberse"
    assert stats["merged"] >= 1
    # y no quedan dos coros idénticos pegados
    snapped = [s for s in out if s.get("chorus_snapped")]
    assert snapped and snapped[-1]["end"] >= 224.0


def test_no_toca_una_letra_distinta():
    """Una línea que NO es el coro (verso real) dentro de la zona se deja."""
    verso = "Cuando los meses pasan y mi ropa no ha cambiado"
    segs = _coro() + [_seg(verso, 210.0, 214.0)]
    out, _ = cs.snap(segs)
    assert any(s["text"] == verso for s in out), "el verso real no se toca"


def test_no_toca_coro_ya_limpio():
    segs = _coro(n=5)
    out, stats = cs.snap(segs)
    assert stats["snapped"] == 0 and stats["merged"] == 0
    assert [s["text"] for s in out] == [CANON] * 5


def test_fuera_de_la_zona_no_snapea():
    """Un fragmento lejos del grupo (otra parte de la canción) no se toca."""
    segs = _coro() + [_seg("por ahí", 30.0, 32.0)]
    out, _ = cs.snap(segs)
    assert any(s["text"] == "por ahí" for s in out)


def test_grupo_chico_no_participa():
    """Menos de min_group repeticiones → no hay canónica que estampar."""
    segs = [_seg(CANON, 10.0, 13.0), _seg(CANON, 20.0, 23.0),
            _seg("por ahí", 24.0, 26.0)]
    out, stats = cs.snap(segs)
    assert stats["snapped"] == 0


def test_no_snapea_linea_mucho_mas_larga():
    """Una línea larga que apenas comparte palabras no es un fragmento."""
    largo = "por ahí caminaba solo pensando en todo lo que dejé atrás ese día"
    segs = _coro() + [_seg(largo, 216.0, 222.0)]
    out, _ = cs.snap(segs)
    assert any(s["text"] == largo for s in out)


def test_nunca_levanta_con_basura():
    out, stats = cs.snap([{"start": "x"}, None, 42])  # type: ignore
    assert isinstance(out, list) and isinstance(stats, dict)


def test_kill_switch(monkeypatch):
    monkeypatch.delenv("CHORUS_SNAP_ENABLED", raising=False)
    assert cs.is_enabled() is False
    monkeypatch.setenv("CHORUS_SNAP_ENABLED", "1")
    assert cs.is_enabled() is True
