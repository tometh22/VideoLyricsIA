def test_timeseries_no_astext_regression():
    """Incidente prod 2026-06-11: AuditLog.detail[...].astext explotaba
    (el TypeDecorator JSONB no expone .astext — AttributeError al armar
    la query, 500 en /admin/metrics/timeseries y KPIs de Rendimiento en
    blanco). Regresión textual: accessor portable + fallback Python."""
    import inspect
    import admin_metrics
    src = inspect.getsource(admin_metrics.metrics_timeseries)
    assert 'detail["job_id"].astext' not in src  # el comentario puede nombrar .astext
    assert "_edit_rows_python" in src
    assert "as_string()" in src
