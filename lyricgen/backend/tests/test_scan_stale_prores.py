"""Tests del scan diario de masters ProRes stale (guardia 2026-06-10).

Post-incidente: 15 masters stale latentes (pre-#622) se descubrieron
recién cuando una operadora descargó uno. scan_stale_prores() es la
auditoría automática: master más viejo que su MP4 fuente → alerta.
"""
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from database import Job, SessionLocal  # noqa: E402

_T = "tenant_stale_scan_test"
_NOW = datetime(2026, 6, 10, 12, 0, tzinfo=timezone.utc)


def _seed(db, job_id, s3_keys):
    db.add(Job(
        job_id=job_id, user_id=1, tenant_id=_T, artist="A",
        filename=f"{job_id}.wav", style="oscuro", status="done",
        delivery_profile="both", s3_keys=s3_keys,
        # Stay inside the production newest-N scan window in the full suite.
        created_at=datetime.now(timezone.utc),
    ))
    db.commit()


def _cleanup(db):
    db.query(Job).filter(Job.tenant_id == _T).delete(synchronize_session=False)
    db.commit()


class _FakeClient:
    """head_object con LastModified controlado por key."""

    def __init__(self, mtimes):
        self.mtimes = mtimes

    def head_object(self, Bucket, Key):
        if Key not in self.mtimes:
            raise RuntimeError("404")
        return {"LastModified": self.mtimes[Key]}


def _run_scan(mtimes):
    import prores
    import storage
    fake_sentry = mock.MagicMock()
    with mock.patch.object(storage, "is_enabled", return_value=True), \
         mock.patch.object(storage, "_get_client", return_value=_FakeClient(mtimes)), \
         mock.patch.dict(sys.modules, {"sentry_sdk": fake_sentry}):
        scope = mock.MagicMock()
        fake_sentry.push_scope.return_value.__enter__ = mock.Mock(return_value=scope)
        fake_sentry.push_scope.return_value.__exit__ = mock.Mock(return_value=False)
        result = prores.scan_stale_prores(limit=50)
    return result, fake_sentry, scope


def test_detecta_master_stale_y_alerta():
    db = SessionLocal()
    try:
        _cleanup(db)
        _seed(db, "stalejob01", {"video": "t/stalejob01/lyric_video.mp4",
                                 "umg_master": "t/stalejob01/umg_master.mov"})
        stale, sentry, scope = _run_scan({
            "t/stalejob01/lyric_video.mp4": _NOW,
            "t/stalejob01/umg_master.mov": _NOW - timedelta(hours=2),
        })
        assert [s["job_id"] for s in stale] == ["stalejob01"]
        assert stale[0]["lag_seconds"] == 7200
        sentry.capture_message.assert_called_once()
        assert scope.fingerprint == ["stale-prores-scan"]
    finally:
        _cleanup(db)
        db.close()


def test_master_fresco_no_alerta():
    db = SessionLocal()
    try:
        _cleanup(db)
        _seed(db, "freshjob01", {"video": "t/freshjob01/lyric_video.mp4",
                                 "umg_master": "t/freshjob01/umg_master.mov"})
        stale, sentry, _ = _run_scan({
            "t/freshjob01/lyric_video.mp4": _NOW - timedelta(hours=2),
            "t/freshjob01/umg_master.mov": _NOW,
        })
        assert stale == []
        sentry.capture_message.assert_not_called()
    finally:
        _cleanup(db)
        db.close()


def test_sin_master_o_sin_objeto_se_saltea():
    """Jobs sin umg_master (YouTube-only o invalidados esperando lazy
    re-transcode) y objetos borrados de R2 no son staleness."""
    db = SessionLocal()
    try:
        _cleanup(db)
        _seed(db, "youtubeonly", {"video": "t/youtubeonly/lyric_video.mp4"})
        _seed(db, "borradoderr2", {"video": "t/borradoderr2/lyric_video.mp4",
                                   "umg_master": "t/borradoderr2/umg_master.mov"})
        stale, sentry, _ = _run_scan({
            "t/youtubeonly/lyric_video.mp4": _NOW,
            # borradoderr2: ninguno de los dos en R2 → head_object 404 → skip
        })
        assert stale == []
        sentry.capture_message.assert_not_called()
    finally:
        _cleanup(db)
        db.close()
