"""P1-B 2026-07-17 — espera acotada por un preview de fondo en vuelo.

Cuando el operador aprueba mientras el preview de fondo todavía corre, el
render hacía cache-miss y regeneraba con Veo un fondo que YA estaba en
vuelo (doble llamada). Con BG_PREVIEW_WAIT_S>0 el render espera (acotado)
a que el preview cachee y lo reusa. Default 0 = inerte.

Guards adversariales cubiertos:
- solo espera previews en `bg_preview_generating` (no `queued`: un preview
  que ShortWorker nunca levantó haría esperar el deadline en vano);
- solo del MISMO tenant (procedencia/costos no cruzan tenants);
- recencia de 15 min (fila zombie post-OOM no ancla la espera);
- corta apenas el preview flipea a `bg_preview_failed`;
- clamp del deadline a 60 s.
"""
import uuid

import pipeline
from database import Job, SessionLocal, User


def _mk_user(db, tenant):
    suffix = uuid.uuid4().hex[:10]
    u = User(
        username=f"wait_{suffix}", email=f"wait_{suffix}@example.com",
        hashed_password="x", tenant_id=tenant, is_active=True,
    )
    db.add(u)
    db.flush()
    return u


def _mk_job(db, user, *, job_id, filename, status, tenant=None):
    j = Job(
        job_id=job_id, user_id=user.id, tenant_id=tenant or user.tenant_id,
        artist="A", song_title="S", filename=filename, status=status,
        delivery_profile="youtube",
    )
    db.add(j)
    db.commit()
    return j


class TestWaitFlag:
    def test_default_es_inerte(self, monkeypatch):
        monkeypatch.delenv("BG_PREVIEW_WAIT_S", raising=False)
        assert pipeline._bg_preview_wait_s() == 0

    def test_clamp_a_60(self, monkeypatch):
        monkeypatch.setenv("BG_PREVIEW_WAIT_S", "300")
        assert pipeline._bg_preview_wait_s() == 60
        monkeypatch.setenv("BG_PREVIEW_WAIT_S", "-5")
        assert pipeline._bg_preview_wait_s() == 0
        monkeypatch.setenv("BG_PREVIEW_WAIT_S", "45")
        assert pipeline._bg_preview_wait_s() == 45

    def test_flag_off_no_toca_db(self, monkeypatch):
        monkeypatch.delenv("BG_PREVIEW_WAIT_S", raising=False)

        def _boom(*a, **k):
            raise AssertionError("con el flag off no debe buscar previews")

        monkeypatch.setattr(pipeline, "_find_inflight_bg_preview", _boom)
        assert pipeline._await_inflight_bg_preview(
            "abc123", "/tmp", job_id="renderjob01",
        ) is None


class TestFindInflight:
    def test_encuentra_generating_del_mismo_tenant(self, db):
        u = _mk_user(db, "tenant_a")
        _mk_job(db, u, job_id="render000001", filename="song.mp3",
                status="processing")
        _mk_job(db, u, job_id="preview00001",
                filename="bgpreview_abc123def456.preview",
                status="bg_preview_generating")
        assert pipeline._find_inflight_bg_preview(
            "abc123def456", render_job_id="render000001",
        ) == "preview00001"

    def test_ignora_queued(self, db):
        """Un preview encolado que ShortWorker nunca levantó no debe anclar
        la espera."""
        u = _mk_user(db, "tenant_b")
        _mk_job(db, u, job_id="render000002", filename="song.mp3",
                status="processing")
        _mk_job(db, u, job_id="preview00002",
                filename="bgpreview_abc123def456.preview",
                status="bg_preview_queued")
        assert pipeline._find_inflight_bg_preview(
            "abc123def456", render_job_id="render000002",
        ) is None

    def test_ignora_otro_tenant(self, db):
        """Consumir el preview de OTRO tenant cruza procedencia y costos —
        prohibido (auditoría UMG)."""
        u_a = _mk_user(db, "tenant_c")
        u_b = _mk_user(db, "tenant_d")
        _mk_job(db, u_a, job_id="render000003", filename="song.mp3",
                status="processing")
        _mk_job(db, u_b, job_id="preview00003",
                filename="bgpreview_abc123def456.preview",
                status="bg_preview_generating")
        assert pipeline._find_inflight_bg_preview(
            "abc123def456", render_job_id="render000003",
        ) is None

    def test_ignora_filas_viejas(self, db):
        from datetime import datetime, timedelta, timezone
        u = _mk_user(db, "tenant_e")
        _mk_job(db, u, job_id="render000004", filename="song.mp3",
                status="processing")
        j = _mk_job(db, u, job_id="preview00004",
                    filename="bgpreview_abc123def456.preview",
                    status="bg_preview_generating")
        j.created_at = datetime.now(timezone.utc) - timedelta(minutes=30)
        db.commit()
        assert pipeline._find_inflight_bg_preview(
            "abc123def456", render_job_id="render000004",
        ) is None


class TestAwaitInflight:
    def _arm(self, monkeypatch, *, cache_checks, preview_status="bg_preview_generating"):
        """Wiring común: sin sleeps reales, cache_check con respuestas
        secuenciales, download escribe el archivo."""
        monkeypatch.setenv("BG_PREVIEW_WAIT_S", "45")
        monkeypatch.setattr(
            pipeline, "_find_inflight_bg_preview",
            lambda key, render_job_id: "preview0000x",
        )
        import bg_preview as bp
        seq = iter(cache_checks)
        monkeypatch.setattr(bp, "cache_check", lambda k: next(seq, False))

        def _fake_download(key, dest):
            with open(dest, "w") as f:
                f.write("bg")
            return True

        monkeypatch.setattr(bp, "cache_download", _fake_download)

        class _Row:
            status = preview_status

        class _Q:
            def filter(self, *a):
                return self

            def first(self):
                return _Row() if preview_status else None

        class _DB:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def query(self, *a):
                return _Q()

        import database as dbmod
        monkeypatch.setattr(pipeline, "_bg_preview_wait_s", lambda: 45)
        # time.sleep no-op vía import local del helper
        import time as time_mod
        monkeypatch.setattr(time_mod, "sleep", lambda s: None)
        return dbmod, _DB

    def test_hit_en_segundo_poll(self, monkeypatch, tmp_path):
        dbmod, _DB = self._arm(monkeypatch, cache_checks=[False, True])
        monkeypatch.setattr(dbmod, "SessionLocal", _DB)
        path = pipeline._await_inflight_bg_preview(
            "abc123", str(tmp_path), job_id="render0000x",
        )
        assert path is not None and path.endswith("bg_cached_abc123.mp4")

    def test_corta_si_el_preview_fallo(self, monkeypatch, tmp_path):
        dbmod, _DB = self._arm(
            monkeypatch, cache_checks=[False, False, False],
            preview_status="bg_preview_failed",
        )
        monkeypatch.setattr(dbmod, "SessionLocal", _DB)
        assert pipeline._await_inflight_bg_preview(
            "abc123", str(tmp_path), job_id="render0000x",
        ) is None

    def test_deadline_agotado_devuelve_none(self, monkeypatch, tmp_path):
        import itertools
        dbmod, _DB = self._arm(
            monkeypatch, cache_checks=itertools.repeat(False),
        )
        monkeypatch.setattr(dbmod, "SessionLocal", _DB)
        # Deadline mínimo real (1s) con sleep no-op: el loop corre y expira
        # por reloj de pared casi al instante.
        monkeypatch.setattr(pipeline, "_bg_preview_wait_s", lambda: 1)
        assert pipeline._await_inflight_bg_preview(
            "abc123", str(tmp_path), job_id="render0000x",
        ) is None
