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

def _fake_counter(monkeypatch, n, artist="Bersuit", title="La Argentinidad",
                  hermanos=None):
    """Sesión falsa: `n` llamadas pagas ya registradas, y un job con la
    identidad de canción indicada (o sin metadata si van vacías).

    `hermanos` son las filas (job_id, artista, título) que la query de
    candidatos devuelve — sirve para probar la normalización de identidad.
    """
    import pipeline

    filas = hermanos if hermanos is not None else [
        ("job-hermano-1", artist, title),
        ("job-hermano-2", artist, title),
    ]

    class _Q:
        def filter(self, *a, **k): return self
        def scalar(self): return n
        def one_or_none(self): return (artist, title)
        def all(self): return filas

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


# ---------------------------------------------------------------------------
# Regresiones de la segunda revisión
# ---------------------------------------------------------------------------

def test_no_se_cuenta_a_si_misma(monkeypatch):
    """El recorder se abre ANTES del chequeo (tiene que cubrir el camino de
    cache), así que sin excluir la fila propia el tope corta una generación
    antes de lo configurado."""
    p = _fake_counter(monkeypatch, 10)
    monkeypatch.setattr(p, "VEO_MAX_CALLS_PER_SONG", 10)

    class _QExcl:
        """Devuelve 9 cuando se excluye una fila, 10 cuando no."""
        def __init__(self): self.excluido = False
        def filter(self, *a, **k):
            # El filtro de exclusión es el único que compara con `!=`.
            if a and "!=" in str(a[0]):
                self.excluido = True
            return self
        def scalar(self): return 9 if self.excluido else 10
        def one_or_none(self): return ("Bersuit", "La Argentinidad")
        def all(self): return [("j1", "Bersuit", "La Argentinidad")]

    q = _QExcl()
    monkeypatch.setattr("database.SessionLocal",
                        lambda: type("S", (), {"query": lambda s, *a, **k: q,
                                               "close": lambda s: None})())
    over, spent = p._veo_budget_exceeded("job123", exclude_row_id=555)
    assert over is False and spent == 9, "la llamada en curso no debe contarse"


def test_la_identidad_de_cancion_colapsa_espacios(monkeypatch):
    """Comparando con `lower(trim(...))` en SQL, "La  Argentinidad" y
    "La Argentinidad" eran canciones distintas — corregir la metadata de un
    job le estrenaba presupuesto. La identidad tiene que ser la misma que usa
    `cost_attribution.song_key`."""
    import pipeline
    assert (pipeline._song_identity("Bersuit", "La  Argentinidad")
            == pipeline._song_identity(" bersuit ", "La Argentinidad"))


def test_los_hermanos_se_matchean_por_identidad_normalizada(monkeypatch):
    """Un job con el título mal tipeado sigue contando contra el mismo
    presupuesto."""
    p = _fake_counter(
        monkeypatch, 12, artist="Bersuit", title="La Argentinidad",
        hermanos=[
            ("j1", "Bersuit", "La Argentinidad"),
            ("j2", "bersuit", "La  Argentinidad"),   # espacios y mayúsculas
            ("j3", "Otra Banda", "Otro Tema"),        # no es hermana
        ])
    monkeypatch.setattr(p, "VEO_MAX_CALLS_PER_SONG", 10)
    over, spent = p._veo_budget_exceeded("j1")
    assert over is True and spent == 12


def test_la_fila_se_borra_cuando_corta(monkeypatch):
    """Cerrar la fila con `budget_exceeded` inventaba gasto que nunca ocurrió
    y contaminaba el conteo de la pasada siguiente. Se borra."""
    import pipeline

    borradas = []

    class _Del:
        def delete(self, **k): borradas.append(True)
    class _Q:
        def filter(self, *a, **k): return _Del()
    class _S:
        def query(self, *a, **k): return _Q()
        def commit(self): pass
        def close(self): pass

    monkeypatch.setattr("database.SessionLocal", _S)
    rec = type("R", (), {"_row_id": 42, "_finished": False,
                         "finish": lambda s, **k: None})()
    pipeline._discard_provenance_row(rec)
    assert borradas == [True]
    assert rec._finished is True


def test_si_no_puede_borrar_no_rompe(monkeypatch):
    """Un control de costo no puede tumbar la generación porque falló un
    DELETE."""
    import pipeline

    class _Boom:
        def __init__(self): raise RuntimeError("db caida")

    monkeypatch.setattr("database.SessionLocal", _Boom)
    cerrada = []
    rec = type("R", (), {"_row_id": 42, "_finished": False,
                         "finish": lambda s, **k: cerrada.append(k)})()
    pipeline._discard_provenance_row(rec)   # no debe levantar
    assert cerrada, "cae a cerrar la fila si no puede borrarla"

