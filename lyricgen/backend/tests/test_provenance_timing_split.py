"""Desglose queue vs predict en la telemetría de Replicate.

`duration_ms` mide la ventana entera de la llamada, así que una corrida lenta
es ambigua entre "el modelo tardó" y "esperamos GPU" — dos problemas con
soluciones opuestas. Diagnosticar el incidente 2026-08-26/28 obligó a ir a la
API de Replicate a mano.

Lo que ese desglose mostró (238 corridas de demucs, jun-ago 2026): la
degradación fue 100% cola. `predict_time` mediana 87,6 s ANTES y DESPUÉS;
la cola pasó de 23,5 s a 204,3 s con picos de 1824 s.
"""

import datetime as dt
import types

from replicate_budget import prediction_timing


class _Pred:
    def __init__(self, **kw):
        for k, v in kw.items():
            setattr(self, k, v)


def test_extrae_queue_y_predict():
    p = _Pred(
        metrics={"predict_time": 87.6, "total_time": 291.9},
        created_at="2026-08-28T16:23:25.000000Z",
        started_at="2026-08-28T16:26:49.300000Z",   # 204,3 s de cola
    )
    queue_ms, predict_ms = prediction_timing(p)
    assert predict_ms == 87600
    assert queue_ms == 204300


def test_acepta_datetime_ya_parseado():
    now = dt.datetime(2026, 8, 28, 16, 0, 0, tzinfo=dt.timezone.utc)
    p = _Pred(
        metrics={"predict_time": 10.0},
        created_at=now,
        started_at=now + dt.timedelta(seconds=5),
    )
    assert prediction_timing(p) == (5000, 10000)


def test_cola_nunca_negativa():
    """Un skew de reloj no debe producir una cola negativa."""
    p = _Pred(
        metrics={"predict_time": 1.0},
        created_at="2026-08-28T16:00:05.000000Z",
        started_at="2026-08-28T16:00:00.000000Z",
    )
    queue_ms, _ = prediction_timing(p)
    assert queue_ms == 0


def test_sin_metrics_devuelve_none_en_predict():
    """Una corrida que murió antes de arrancar no tiene predict_time.

    Es exactamente el caso de un timeout de cola — y ahí el queue_time es el
    dato que importa, así que se conserva.
    """
    p = _Pred(
        metrics=None,
        created_at="2026-08-28T16:00:00.000000Z",
        started_at="2026-08-28T16:05:00.000000Z",
    )
    queue_ms, predict_ms = prediction_timing(p)
    assert predict_ms is None
    assert queue_ms == 300000


def test_es_telemetria_nunca_levanta():
    """Ante basura devuelve (None, None) en vez de romper la llamada real."""
    assert prediction_timing(None) == (None, None)
    assert prediction_timing(_Pred()) == (None, None)
    assert prediction_timing(_Pred(metrics="no-soy-un-dict",
                                   created_at="fecha-invalida")) == (None, None)


def test_finish_provenance_tolera_prediction_ausente():
    """El camino sin prediction (modelo sin version hash) sigue funcionando."""
    from replicate_budget import finish_replicate_provenance

    visto = {}

    rec = types.SimpleNamespace(
        finish=lambda **kw: visto.update(kw))
    finish_replicate_provenance(rec, "succeeded")
    assert visto["predict_time_ms"] is None
    assert visto["queue_time_ms"] is None
    assert visto["response_summary"] == "succeeded"
