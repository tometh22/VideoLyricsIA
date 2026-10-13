from datetime import date, datetime, timedelta
import importlib.util
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "report_ops_daily", Path(__file__).resolve().parents[1] / "scripts" / "report_ops_daily.py",
)
ops = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ops)

ART = ops.ART


def at(day, hour=12):
    return datetime(2026, 10, day, hour, tzinfo=ART)


def job(job_id, **kw):
    return {"job_id": job_id, "uploaded_at": at(1), **kw}


def test_classify_sigue_el_recorrido_del_job():
    assert ops.classify(job("a"))[0] is None                                    # transcribiendo
    assert ops.classify(job("a", lyrics_ready_at=at(2)))[0] == "unopened"
    assert ops.classify(job("a", lyrics_ready_at=at(2), opened_at=at(3)))[0] == "opened"
    assert ops.classify(job("a", lyrics_ready_at=at(2), approved_at=at(3)))[0] == "approved"
    assert ops.classify(job("a", approved_at=at(3), delivered_at=at(4)))[0] == "delivered"
    assert ops.classify(job("a", delivered_at=at(4), client_approved_at=at(5)))[0] is None
    state, since = ops.classify(job("a", delivered_at=at(4), client_approved_at=at(5),
                                    open_change_requests=1, change_request_since=at(6)))
    assert (state, since) == ("change_request", at(6))


def test_reporte_cuenta_edades_aprobaciones_y_alertas():
    jobs = {f"u{i}": job(f"u{i}", lyrics_ready_at=at(1)) for i in range(41)}
    jobs["d1"] = job("d1", approved_at=at(5), delivered_at=at(6))
    data = {"jobs": jobs, "approvals": [{"job_id": "d1", "at": at(5), "operator": "ana"}]}
    report = ops.build_report(data, date(2026, 10, 7), holidays=set())
    assert report["states"]["unopened"]["count"] == 41
    assert report["states"]["unopened"]["oldest_days"] >= 6
    assert report["approvals_week"] == {"ana": 1}
    assert report["approvals_today_total"] == 0
    assert report["delivered_week"] == 1 and report["upload_to_delivered_median_days"] == 5.0
    alerts = " ".join(report["alerts"])
    assert "Backlog sin abrir: 41" in alerts
    assert "sin abrir hace más de 3 días" in alerts
    assert "Cero aprobaciones en un día hábil" in alerts


def test_sin_alerta_de_cero_aprobaciones_en_fin_de_semana_o_feriado():
    data = {"jobs": {}, "approvals": []}
    assert ops.build_report(data, date(2026, 10, 10), holidays=set())["alerts"] == []    # sábado
    assert ops.build_report(data, date(2026, 10, 12), holidays={date(2026, 10, 12)})["alerts"] == []


def test_html_no_rompe_con_nombres_raros():
    data = {"jobs": {}, "approvals": [{"job_id": "x", "at": at(7), "operator": "<b>op</b>"}]}
    body = ops.render_html(ops.build_report(data, date(2026, 10, 7), holidays=set()))
    assert "&lt;b&gt;op&lt;/b&gt;" in body and "<b>op</b>" not in body
