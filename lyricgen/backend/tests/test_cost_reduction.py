"""Focused regressions without loading the renderer or using a live database."""

from __future__ import annotations

import ast
import asyncio
import io
import logging
import os
import sys
import tempfile
import zipfile
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import JSON, Column, DateTime, Integer, String, create_engine, event, func
from sqlalchemy.orm import Session, declarative_base

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

from download_bundle import TemporaryZipResponse  # noqa: E402


def _function(filename, name, **namespace):
    """Run the actual handler with lightweight dependency boundaries."""
    module = ast.parse((BACKEND / filename).read_text())
    node = next(n for n in module.body if isinstance(
        n, (ast.FunctionDef, ast.AsyncFunctionDef),
    ) and n.name == name)
    node.decorator_list = []
    node.args.defaults = []
    node.args.kw_defaults = [None] * len(node.args.kw_defaults)
    isolated = ast.Module(body=[
        ast.ImportFrom(module="__future__", names=[
            ast.alias(name="annotations"),
        ], level=0), node,
    ], type_ignores=[])
    exec(compile(ast.fix_missing_locations(isolated), str(BACKEND / filename), "exec"), namespace)
    return namespace[name]


Base = declarative_base()


class AuditRow(Base):
    __tablename__ = "audit"
    id = Column(Integer, primary_key=True)
    action = Column(String)
    detail = Column(JSON)


class EventRow(Base):
    __tablename__ = "events"
    id = Column(Integer, primary_key=True)
    name = Column(String)
    job_id = Column(String)
    user_id = Column(Integer)
    tenant_id = Column(String)
    properties = Column(JSON)
    occurred_at = Column(DateTime)


@pytest.fixture
def local_db():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session
    engine.dispose()


def test_campaign_verdict_selects_latest_across_versions_without_loading_history(local_db):
    rows = [
        {"action": "semaforo.verdict.v2", "detail": {"job_id": "unrelated", "color": "red"}}
        for _ in range(300)
    ] + [
        {"action": "semaforo.verdict.v2", "detail": {"job_id": "wanted", "color": "red"}},
        {"action": "semaforo.verdict.v1", "detail": {"job_id": "wanted", "color": "green"}},
        {"action": "other", "detail": {"job_id": "wanted", "color": "yellow"}},
        {"action": "semaforo.verdict.v2", "detail": {"job_id": "second", "color": "yellow"}},
        {"action": "semaforo.verdict.v2", "detail": None},
    ]
    local_db.execute(AuditRow.__table__.insert(), rows)
    local_db.commit()
    loaded = []
    listener = lambda row, _: loaded.append(row)
    event.listen(AuditRow, "load", listener)
    try:
        select = _function("batch_campaigns.py", "_latest_semaforo_verdicts", AuditLog=AuditRow, func=func)
        assert select(local_db, ["wanted", "second", "missing", "wanted"]) == {
            "wanted": {"job_id": "wanted", "color": "green"},
            "second": {"job_id": "second", "color": "yellow"},
        }
        assert len(loaded) <= 2
        assert select(local_db, []) == {}
    finally:
        event.remove(AuditRow, "load", listener)


def test_editor_sequence_uses_own_session_without_loading_other_history(local_db, monkeypatch):
    local_db.execute(EventRow.__table__.insert(), [
        {"name": "editor_activity_heartbeat", "job_id": "job", "user_id": 7,
         "properties": {"session_id": "own-session", "activity_seq": 2}},
    ] + [
        {"name": "editor_activity_heartbeat", "job_id": "job", "user_id": 7,
         "properties": {"session_id": "other-session", "activity_seq": 900}}
        for _ in range(100)
    ])
    local_db.commit()
    loaded = []
    listener = lambda row, _: loaded.append(row)
    event.listen(EventRow, "load", listener)
    monkeypatch.setitem(sys.modules, "evidence_attestation", SimpleNamespace(
        lyric_snapshot_hash=lambda _: "snapshot",
    ))
    document = SimpleNamespace(lock_user_id=7,
        lock_expires_at=datetime.now(timezone.utc) + timedelta(minutes=1),
        revision=4, current_segments=[])
    handler = _function("main.py", "editor_activity_heartbeat",
        ProductEvent=EventRow, datetime=datetime, timezone=timezone,
        HTTPException=HTTPException,
        _editor_document_or_404=lambda *_: (SimpleNamespace(transcription_quality={}), document))
    try:
        result = asyncio.run(handler(None, "job", SimpleNamespace(
            session_id="own-session", activity_seq=3, task="review"),
            {"id": 7, "tenant_id": "tenant"}, local_db))
        assert result["revision"] == 4
        assert len(loaded) == 1
        with pytest.raises(HTTPException) as failure:
            asyncio.run(handler(None, "job", SimpleNamespace(
                session_id="own-session", activity_seq=5, task="review"),
                {"id": 7, "tenant_id": "tenant"}, local_db))
        assert failure.value.status_code == 409
    finally:
        event.remove(EventRow, "load", listener)


@pytest.fixture
def bundle_handler(tmp_path):
    source = tmp_path / "outputs" / "job"
    source.mkdir(parents=True)
    (source / "video.mp4").write_bytes(b"video" * 30_000)
    (source / "thumb.jpg").write_bytes(b"image")
    (source / "master.mov").write_bytes(b"excluded")

    @contextmanager
    def scoped_db():
        yield None

    return _function("main.py", "download_all_zip", os=os,
        HTTPException=HTTPException, scoped_db=scoped_db,
        verify_media_token=lambda *_: {"id": 7}, _job_scope=lambda _: {},
        get_job=lambda *_args, **_kw: {"status": "done", "artist": "Artist",
            "files": {"video_url": "video", "thumbnail_url": "thumb", "umg_master_url": "master"}},
        _BUNDLE_TYPES=("video", "thumbnail"),
        FILE_MAP={"video": "video.mp4", "thumbnail": "thumb.jpg"},
        OUTPUTS_DIR=str(tmp_path / "outputs"),
        storage=SimpleNamespace(is_enabled=lambda: False),
        logger=logging.getLogger(__name__), _audit_media_access=lambda *_args, **_kw: None)


@pytest.mark.parametrize("interrupted", [False, True])
def test_zip_keeps_files_until_send_and_cleans_up_on_interruption(bundle_handler, interrupted):
    response = bundle_handler("job", None, "token")
    assert isinstance(response, TemporaryZipResponse)
    assert Path(response.path).exists()
    with zipfile.ZipFile(response.path) as archive:
        assert archive.namelist() == ["video.mp4", "thumb.jpg"]
    body = []

    async def send(message):
        if message["type"] == "http.response.body":
            assert Path(response.path).exists()
            if interrupted:
                raise ConnectionError("client disconnected")
            body.append(message.get("body", b""))

    async def receive():
        return {"type": "http.disconnect"}

    scope = {"type": "http", "method": "GET", "headers": [], "extensions": {}}
    if interrupted:
        with pytest.raises(ConnectionError):
            asyncio.run(response(scope, receive, send))
    else:
        asyncio.run(response(scope, receive, send))
        assert max(map(len, body)) <= response.chunk_size
        with zipfile.ZipFile(io.BytesIO(b"".join(body))) as archive:
            assert archive.read("video.mp4") == b"video" * 30_000
    assert not Path(response.temp_dir).exists()


def test_zip_cleans_staged_files_when_audit_fails_before_response(bundle_handler, tmp_path, monkeypatch):
    created = []
    original = tempfile.mkdtemp

    def staged_dir(**kwargs):
        path = original(dir=tmp_path, **kwargs)
        created.append(Path(path))
        return path

    def failed_audit(*_args, **_kwargs):
        raise RuntimeError("audit unavailable")

    monkeypatch.setattr(tempfile, "mkdtemp", staged_dir)
    monkeypatch.setitem(bundle_handler.__globals__, "_audit_media_access", failed_audit)
    with pytest.raises(RuntimeError, match="audit unavailable"):
        bundle_handler("job", None, "token")
    assert len(created) == 1
    assert not created[0].exists()


def test_zip_releases_r2_staging_files_and_preserves_local_deliverables(bundle_handler, monkeypatch):
    original_job = bundle_handler.__globals__["get_job"]

    def stored_job(*args, **kwargs):
        return {**original_job(*args, **kwargs), "s3_keys": {
            "video": "r2-video", "thumbnail": "r2-thumbnail",
        }}

    def download(key, dest):
        Path(dest).write_bytes(key.encode())
        return True

    monkeypatch.setitem(bundle_handler.__globals__, "get_job", stored_job)
    monkeypatch.setitem(bundle_handler.__globals__, "storage", SimpleNamespace(
        is_enabled=lambda: True, download_object=download,
    ))
    response = bundle_handler("job", None, "token")
    try:
        assert list(Path(response.temp_dir).iterdir()) == [Path(response.path)]
        with zipfile.ZipFile(response.path) as archive:
            assert archive.read("video.mp4") == b"r2-video"
        source = Path(bundle_handler.__globals__["OUTPUTS_DIR"]) / "job" / "video.mp4"
        assert source.read_bytes() == b"video" * 30_000
    finally:
        import shutil
        shutil.rmtree(response.temp_dir)
