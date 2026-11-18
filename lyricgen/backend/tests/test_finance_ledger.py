"""Planilla de finanzas del admin (finance_ledger.py).

Lo que fija:

* Sólo super-admin: expone márgenes, mano de obra y el reparto con el socio.
* Un PATCH no puede dejar un renglón que el alta rechazaría.
* Reimportar con ``replace`` no duplica la planilla.
* El audit no guarda montos (lo leen más roles que esta pantalla).
* Las métricas cuentan la PRIMERA entrega vigente de cada job, por mes de
  Buenos Aires, y separan art tracks de lyric videos.
"""
import os
import sqlite3
import subprocess
import sys
import uuid
from datetime import datetime, timezone

from tests.conftest import auth


def _entry(**overrides):
    body = {
        "period": "2026-09",
        "section": "costo_directo",
        "category": "Infraestructura",
        "provider": "Railway",
        "concept": "Consumo del mes",
        "amount_usd": 100.5,
        "basis": "api",
    }
    body.update(overrides)
    return body


def test_finance_endpoints_require_admin(client, user_token):
    assert client.get("/admin/finance/entries", headers=auth(user_token)).status_code == 403
    assert client.post("/admin/finance/entries", json=_entry(), headers=auth(user_token)).status_code == 403
    res = client.get("/admin/finance/operations?since=2026-08&until=2026-10", headers=auth(user_token))
    assert res.status_code == 403


def test_finance_requires_super_admin_when_allowlist_is_set(client, admin_token, monkeypatch):
    monkeypatch.setenv("SUPER_ADMIN_USERS", "alguien-que-no-es@example.com")
    assert client.get("/admin/finance/entries", headers=auth(admin_token)).status_code == 403


def test_crud_and_validation(client, admin_token, db):
    from database import AuditLog

    res = client.post("/admin/finance/entries", json=_entry(), headers=auth(admin_token))
    assert res.status_code == 200, res.text
    created = res.json()
    assert created["amount_usd"] == 100.5

    bad = client.post("/admin/finance/entries", json=_entry(period="2026-13"), headers=auth(admin_token))
    assert bad.status_code == 422
    bad = client.post("/admin/finance/entries", json=_entry(section="otra"), headers=auth(admin_token))
    assert bad.status_code == 422

    res = client.patch(f"/admin/finance/entries/{created['id']}", json={"amount_usd": 101}, headers=auth(admin_token))
    assert res.status_code == 200 and res.json()["amount_usd"] == 101
    res = client.patch(f"/admin/finance/entries/{created['id']}", json={"period": "sept"}, headers=auth(admin_token))
    assert res.status_code == 422

    assert client.delete(f"/admin/finance/entries/{created['id']}", headers=auth(admin_token)).status_code == 200
    ids = {e["id"] for e in client.get("/admin/finance/entries", headers=auth(admin_token)).json()["entries"]}
    assert created["id"] not in ids

    audits = db.query(AuditLog).filter(AuditLog.action.like("finance.%")).all()
    assert audits and all("amount_usd" not in (a.detail or {}) for a in audits)


def test_import_replace_does_not_duplicate(client, admin_token):
    rows = [_entry(period="2031-01", provider="R2", amount_usd=10),
            _entry(period="2031-01", provider="Replicate", amount_usd=20)]
    for _ in range(2):
        res = client.post("/admin/finance/entries/import", json={"entries": rows, "replace": True},
                          headers=auth(admin_token))
        assert res.status_code == 200, res.text
    entries = client.get("/admin/finance/entries", headers=auth(admin_token)).json()["entries"]
    mine = [e for e in entries if e["period"] == "2031-01"]
    assert sorted(e["provider"] for e in mine) == ["R2", "Replicate"]


def test_operations_counts_first_live_delivery_per_job(client, admin_token, db, admin_user_id):
    from database import Delivery

    def add(job_id, label, added_at, removed=False, portal="argentina", title="Canción"):
        db.add(Delivery(
            job_id=job_id, label=label, file_types=["mp4"], artist_snapshot="Artista",
            song_title_snapshot=title, tenant_snapshot="universal_music",
            added_by_user_id=admin_user_id, added_at=added_at, portal_id=portal,
            removed_at=datetime(2032, 3, 2, tzinfo=timezone.utc) if removed else None,
        ))

    tag = uuid.uuid4().hex[:6]
    # Mismo job entregado dos veces: cuenta una sola, en el mes de la primera.
    add(f"a{tag}01", "Campaña", datetime(2032, 3, 10, 15, tzinfo=timezone.utc), title=f"Uno {tag}")
    add(f"a{tag}01", "Campaña", datetime(2032, 4, 2, 15, tzinfo=timezone.utc), title=f"Uno {tag}")
    # 1-abr 01:00 UTC = 31-mar 22:00 en Buenos Aires.
    add(f"a{tag}02", "Art Track", datetime(2032, 4, 1, 1, tzinfo=timezone.utc), title=f"Dos {tag}")
    # Retirada: no cuenta.
    add(f"a{tag}03", "Campaña", datetime(2032, 3, 12, tzinfo=timezone.utc), removed=True, title=f"Tres {tag}")
    add(f"a{tag}04", "Campaña", datetime(2032, 3, 20, tzinfo=timezone.utc), portal="chile", title=f"Cuatro {tag}")
    db.commit()

    res = client.get("/admin/finance/operations?since=2032-03&until=2032-04", headers=auth(admin_token))
    assert res.status_code == 200, res.text
    rows = {(r["month"], r["portal"], r["type"]): r for r in res.json()["deliveries"]}
    assert rows[("2032-03", "argentina", "lyric_video")]["entregas"] == 1
    assert rows[("2032-03", "argentina", "art_track")]["entregas"] == 1
    assert rows[("2032-03", "chile", "lyric_video")]["canciones"] == 1
    assert ("2032-04", "argentina", "lyric_video") not in rows


def test_operations_rejects_bad_range(client, admin_token):
    res = client.get("/admin/finance/operations?since=2026-10&until=2026-08", headers=auth(admin_token))
    assert res.status_code == 422


def test_alembic_upgrade_creates_finance_entries(tmp_path):
    db_path = tmp_path / "alembic_finance.db"
    backend_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    env = {
        **os.environ,
        "DATABASE_URL": f"sqlite:///{db_path}",
        "JWT_SECRET": "test",
        "ADMIN_PASSWORD": "test123ab",
        "ENVIRONMENT": "development",
    }
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=backend_dir, env=env, capture_output=True, text=True, timeout=120,
    )
    assert result.returncode == 0, result.stderr
    conn = sqlite3.connect(str(db_path))
    try:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(finance_entries)")}
    finally:
        conn.close()
    assert {"id", "period", "section", "amount_usd", "basis", "evidence", "payload", "deleted_at"} <= columns


def test_auto_costs_sums_daily_totals_and_reports_coverage(client, admin_token, db):
    from datetime import date
    from database import CostDaily

    for day, amount in ((date(2025, 5, 1), 1.5), (date(2025, 5, 2), 2.0)):
        db.add(CostDaily(day=day, source="railway", grain="day", dim_type="total",
                         dim_value="total", amount_usd=amount, basis="measured"))
        # Los desgloses no se suman dos veces.
        db.add(CostDaily(day=day, source="railway", grain="day", dim_type="service",
                         dim_value="api", amount_usd=amount, basis="measured"))
    # OpenAI no se usa: hoy el recolector lee otra organización.
    db.add(CostDaily(day=date(2025, 5, 1), source="openai", grain="day", dim_type="total",
                     dim_value="total", amount_usd=99, basis="measured"))
    db.commit()
    res = client.get("/admin/finance/auto-costs?since=2025-05&until=2025-05", headers=auth(admin_token))
    assert res.status_code == 200, res.text
    costs = res.json()["costs"]
    assert [c["source"] for c in costs] == ["railway"]
    assert costs[0]["amount_usd"] == 3.5
    assert costs[0]["days"] == 2 and costs[0]["expected_days"] == 31


def test_auto_costs_includes_openai_only_when_enabled(client, admin_token, db, monkeypatch):
    from datetime import date
    from database import CostDaily

    db.add(CostDaily(day=date(2025, 6, 1), source="openai", grain="day", dim_type="total",
                     dim_value="total", amount_usd=5, basis="measured"))
    db.commit()
    url = "/admin/finance/auto-costs?since=2025-06&until=2025-06"
    assert [c["source"] for c in client.get(url, headers=auth(admin_token)).json()["costs"]] == []
    monkeypatch.setenv("FINANCE_OPENAI_AUTO", "1")
    assert [c["source"] for c in client.get(url, headers=auth(admin_token)).json()["costs"]] == ["openai"]


def test_cobro_y_pago_exigen_cuota_y_la_validan(client, admin_token):
    cobro = _entry(section="cobro", provider="Cliente", amount_usd=1000)
    assert client.post("/admin/finance/entries", json=cobro, headers=auth(admin_token)).status_code == 422
    cobro["payload"] = {"cuota": "2026-13"}
    assert client.post("/admin/finance/entries", json=cobro, headers=auth(admin_token)).status_code == 422
    cobro["payload"] = {"cuota": "2026-08", "ars": 1500000}
    assert client.post("/admin/finance/entries", json=cobro, headers=auth(admin_token)).status_code == 200


def test_import_replace_no_borra_contrato_ni_otros_proveedores(client, admin_token):
    contrato = _entry(period="2032-01", section="contrato", provider="Cliente", amount_usd=None,
                      payload={"inicio": "2032-01", "meses": 6})
    otro = _entry(period="2032-01", provider="Otro proveedor", amount_usd=5)
    client.post("/admin/finance/entries/import", json={"entries": [contrato, otro]}, headers=auth(admin_token))
    nuevo = _entry(period="2032-01", provider="Railway", amount_usd=9)
    res = client.post("/admin/finance/entries/import", json={"entries": [contrato, nuevo], "replace": True},
                      headers=auth(admin_token))
    assert res.status_code == 200, res.text
    mine = [e for e in client.get("/admin/finance/entries", headers=auth(admin_token)).json()["entries"]
            if e["period"] == "2032-01"]
    assert sorted(e["provider"] for e in mine if e["section"] == "costo_directo") == ["Otro proveedor", "Railway"]
    assert len([e for e in mine if e["section"] == "contrato"]) == 1


def test_import_sin_reemplazo_no_duplica_y_completa_cuota_vieja(client, admin_token):
    viejo = _entry(period="2033-02", section="pago_socio", provider="Socio", amount_usd=700)
    costo = _entry(period="2033-02", provider="Railway", amount_usd=12)
    for _ in range(2):
        res = client.post("/admin/finance/entries/import", json={"entries": [viejo, costo]}, headers=auth(admin_token))
        assert res.status_code == 200, res.text
    mine = [e for e in client.get("/admin/finance/entries", headers=auth(admin_token)).json()["entries"]
            if e["period"] == "2033-02"]
    assert len(mine) == 2
    assert next(e for e in mine if e["section"] == "pago_socio")["payload"]["cuota"] == "2033-02"
