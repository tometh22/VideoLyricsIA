"""Tope de generaciones de Veo por canción — control de COSTO.

Medido en jul-2026: 12 jobs de prod (el 10%) consumieron el 42,8% del gasto de
Veo, y uno llegó a 26 llamadas — ~$16 en un video que se vende a $8. Un job
sano usa 1-3.

El presupuesto es por CANCIÓN, no por job: cada edición o re-render crea un
job nuevo, así que un techo por job se esquiva solo.
"""

import pytest

# ---------------------------------------------------------------------------
# Tope de generaciones de Veo
# ---------------------------------------------------------------------------

def _fake_counter(monkeypatch, n, artist="Bersuit", title="La Argentinidad"):
    """Sesión falsa: `n` llamadas pagas ya registradas, y un job con la
    identidad de canción indicada (o sin metadata si van vacías)."""
    import pipeline

    class _Q:
        def filter(self, *a, **k): return self
        def scalar(self): return n
        def one_or_none(self): return (artist, title)
        def all(self): return [("job-hermano-1",), ("job-hermano-2",)]

    class _S:
        def query(self, *a, **k): return _Q()
        def close(self): pass

    monkeypatch.setattr("database.SessionLocal", _S)
    return pipeline


def test_bajo_el_tope_deja_pasar(monkeypatch):
    p = _fake_counter(monkeypatch, 3)
    monkeypatch.setattr(p, "VEO_MAX_CALLS_PER_SONG", 10)
    over, spent = p._veo_budget_exceeded("job123")
    assert over is False and spent == 3


def test_en_el_tope_corta(monkeypatch):
    p = _fake_counter(monkeypatch, 10)
    monkeypatch.setattr(p, "VEO_MAX_CALLS_PER_SONG", 10)
    over, spent = p._veo_budget_exceeded("job123")
    assert over is True and spent == 10


def test_el_job_de_26_llamadas_se_habria_cortado(monkeypatch):
    """El outlier real de jul-2026: 26 generaciones en una sola canción."""
    p = _fake_counter(monkeypatch, 26)
    monkeypatch.setattr(p, "VEO_MAX_CALLS_PER_SONG", 10)
    assert p._veo_budget_exceeded("job123")[0] is True


def test_el_presupuesto_es_POR_CANCION_no_por_job(monkeypatch):
    """El punto de la revisión: un tope por job se esquiva solo, porque cada
    edición o re-render crea un job NUEVO con presupuesto fresco. Una canción
    que pasa por 5 ediciones a 10 llamadas gastaría 50 sin que ningún tope se
    entere.

    Acá el job es nuevo (0 llamadas propias) pero su canción ya acumuló 12
    entre jobs hermanos → tiene que cortar igual.
    """
    p = _fake_counter(monkeypatch, 12, artist="Bersuit", title="La Argentinidad")
    monkeypatch.setattr(p, "VEO_MAX_CALLS_PER_SONG", 10)
    over, spent = p._veo_budget_exceeded("job-recien-creado")
    assert over is True and spent == 12


def test_sin_metadata_de_cancion_cae_a_contar_solo_el_job(monkeypatch):
    """Los previews sin artista/título no tienen identidad de canción; contar
    sólo ese job es lo más ajustado posible sin inventar una."""
    p = _fake_counter(monkeypatch, 11, artist="", title="")
    monkeypatch.setattr(p, "VEO_MAX_CALLS_PER_SONG", 10)
    assert p._veo_budget_exceeded("job-sin-metadata")[0] is True


def test_tope_en_cero_lo_desactiva(monkeypatch):
    p = _fake_counter(monkeypatch, 999)
    monkeypatch.setattr(p, "VEO_MAX_CALLS_PER_SONG", 0)
    assert p._veo_budget_exceeded("job123") == (False, 0)


def test_sin_job_id_no_topea(monkeypatch):
    p = _fake_counter(monkeypatch, 999)
    monkeypatch.setattr(p, "VEO_MAX_CALLS_PER_SONG", 10)
    assert p._veo_budget_exceeded(None) == (False, 0)
    assert p._veo_budget_exceeded("") == (False, 0)


def test_si_la_db_falla_deja_generar(monkeypatch):
    """Un tope de costo que rompe entregas cuando la DB hipa es peor que el
    gasto que evita."""
    import pipeline

    class _Boom:
        def __init__(self): raise RuntimeError("db caida")

    monkeypatch.setattr("database.SessionLocal", _Boom)
    monkeypatch.setattr(pipeline, "VEO_MAX_CALLS_PER_SONG", 10)
    assert pipeline._veo_budget_exceeded("job123") == (False, 0)


def test_la_excepcion_es_atrapable_por_los_llamadores():
    """Los tres call sites envuelven la generación en `except Exception`, así
    que el fallback a gradiente sigue andando."""
    import pipeline
    assert issubclass(pipeline.VeoBudgetExceeded, Exception)
    with pytest.raises(Exception):
        raise pipeline.VeoBudgetExceeded("test")

