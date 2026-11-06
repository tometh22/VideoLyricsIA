"""Portal exports must encode the published cut, including historical campaigns."""
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
import json
import shutil
import subprocess

import pytest

import portal_prores as exports
from database import AuditLog, Delivery, SessionLocal


@pytest.fixture
def delivery(db, admin_user_id, monkeypatch):
    monkeypatch.setattr("storage._get_metadata_client", lambda: SimpleNamespace(head_object=lambda **_: {"ContentLength": 1000}))
    monkeypatch.setenv("DELIVERY_PORTAL_TOKEN", "portal-test")
    monkeypatch.setenv("DELIVERY_PORTAL_TOKEN_CHILE", "chile-test")
    row = Delivery(
        job_id="portaltest01", label="Art Track", file_types=["video", "short", "thumbnail"],
        artist_snapshot="Artist", song_title_snapshot="Song", tenant_snapshot="default",
        portal_id="argentina", added_by_user_id=admin_user_id,
        published_revision=2, published_file_keys={
            "video": "default/portaltest01/lyric_video.mp4.published-current",
            "short": "default/portaltest01/short.mp4.published-current",
            "thumbnail": "default/portaltest01/thumbnail.jpg.published-current",
        },
    )
    db.add(row)
    db.commit()
    yield row
    db.query(AuditLog).filter(AuditLog.action == "delivery.download").delete()
    db.query(Delivery).filter(Delivery.id == row.id).delete()
    db.commit()


HEADERS = {"X-Portal-Token": "portal-test", "X-Portal-Id": "argentina"}


def test_listing_offers_both_lazy_formats_without_generating(client, delivery, monkeypatch):
    client_stub = SimpleNamespace(head_object=lambda **_: {"ContentLength": 1000})
    monkeypatch.setattr("storage._get_client", lambda: client_stub)
    monkeypatch.setattr("storage.generate_signed_url", lambda key, **_: "https://files/" + key)
    enqueue = Mock()
    monkeypatch.setattr(exports, "enqueue", enqueue)
    response = client.get("/api/deliveries/items", headers=HEADERS)
    assert response.status_code == 200
    assert response.json()["portal_id"] == "argentina"
    version = next(v for s in response.json()["songs"] for v in s["versions"] if v["delivery_id"] == delivery.id)
    files = {f["type"]: f for f in version["files"]}
    assert files["umg_master"]["can_prepare"] and not files["umg_master"]["available"]
    assert files["umg_short"]["can_prepare"] and not files["umg_short"]["available"]
    enqueue.assert_not_called()


def test_chile_listing_identifies_its_scope_for_the_portal(client, delivery, db, monkeypatch):
    delivery.portal_id = "chile"
    db.commit()
    monkeypatch.setattr("storage._get_client", lambda: SimpleNamespace(head_object=lambda **_: {"ContentLength": 1000}))
    monkeypatch.setattr("storage.generate_signed_url", lambda key, **_: "https://files/" + key)
    response = client.get("/api/deliveries/items", headers={"X-Portal-Id": "chile", "X-Portal-Token": "chile-test"})
    assert response.json()["portal_id"] == "chile"
    assert any(v["delivery_id"] == delivery.id for s in response.json()["songs"] for v in s["versions"])


def test_lazy_prepare_needs_no_job_or_umg_spec(client, delivery, monkeypatch):
    monkeypatch.setattr("storage.object_etag", lambda _: "current-etag")
    enqueue = Mock(return_value="task-current")
    monkeypatch.setattr(exports, "enqueue", enqueue)
    response = client.post(f"/api/deliveries/{delivery.id}/download/umg_master", headers=HEADERS)
    assert response.status_code == 202 and response.json()["can_prepare"]
    response = client.post(f"/api/deliveries/{delivery.id}/prepare-prores", headers=HEADERS, json={"file_type": "umg_master"})
    assert response.status_code == 202
    enqueue.assert_called_once_with(delivery.id, "argentina", "umg_master", delivery.published_file_keys["video"], "current-etag")


@pytest.mark.parametrize("path,method", [("download/umg_master", "post"), ("prepare-prores", "post"), ("prepare-prores", "get")])
def test_portal_auth_and_country_isolation(client, delivery, path, method):
    url = f"/api/deliveries/{delivery.id}/{path}"
    kwargs = {"json": {"file_type": "umg_master"}} if path == "prepare-prores" and method == "post" else {}
    assert getattr(client, method)(url, **kwargs).status_code == 401
    assert getattr(client, method)(url, headers={"X-Portal-Id": "chile", "X-Portal-Token": "chile-test"}, **kwargs).status_code == 404


def test_download_rechecks_expired_master_and_never_signs_old_url(client, delivery, db, monkeypatch):
    delivery.file_types = [*delivery.file_types, "umg_master"]
    delivery.published_file_keys = {**delivery.published_file_keys, "umg_master": "expired.mov"}
    db.commit()
    monkeypatch.setattr("storage.object_status", lambda _: "missing")
    sign = Mock()
    monkeypatch.setattr("storage.generate_signed_url", sign)
    response = client.post(f"/api/deliveries/{delivery.id}/download/umg_master", headers=HEADERS)
    assert response.status_code == 202
    sign.assert_not_called()


def test_ready_download_resolves_current_cut(client, delivery, db, monkeypatch):
    delivery.file_types = [*delivery.file_types, "umg_master"]
    delivery.published_file_keys = {**delivery.published_file_keys, "umg_master": "current.mov"}
    db.commit()
    monkeypatch.setattr("storage.object_status", lambda _: "exists")
    sign = Mock(return_value="https://download/current.mov")
    monkeypatch.setattr("storage.generate_signed_url", sign)
    response = client.post(f"/api/deliveries/{delivery.id}/download/umg_master", headers=HEADERS)
    assert response.json() == {"status": "ready", "url": "https://download/current.mov"}
    assert response.headers["cache-control"] == "private, no-store"
    assert sign.call_args.args == ("current.mov",)


@pytest.mark.parametrize("state,expected", [("started", "processing"), ("failed", "failed"), ("finished", "failed"), ("not_found", "not_started")])
def test_polling_surfaces_failure_and_new_publication(client, delivery, monkeypatch, state, expected):
    monkeypatch.setattr("storage.object_etag", lambda _: "etag")
    monkeypatch.setattr(exports, "task_state", lambda _: state)
    response = client.get(f"/api/deliveries/{delivery.id}/prepare-prores", headers=HEADERS)
    assert response.json()["status"] == expected


def test_queue_unavailable_is_not_a_fake_processing_response(client, delivery, monkeypatch):
    monkeypatch.setattr("storage.object_etag", lambda _: "etag")
    monkeypatch.setattr(exports, "enqueue", Mock(side_effect=RuntimeError("offline")))
    response = client.post(f"/api/deliveries/{delivery.id}/prepare-prores", headers=HEADERS, json={})
    assert response.status_code == 503


@pytest.mark.parametrize("file_type", [["umg_master"], "x" * 21])
def test_preparation_rejects_invalid_input(client, delivery, file_type):
    response = client.post(f"/api/deliveries/{delivery.id}/prepare-prores", headers=HEADERS, json={"file_type": file_type})
    assert response.status_code == 422


def test_pointer_preparation_preserves_tracking_and_ignores_working_master(client, delivery, db, monkeypatch):
    delivery.published_file_keys = None
    delivery.file_types = [*delivery.file_types, "umg_master"]
    db.commit()
    copy = Mock(return_value={"video": "immutable.mp4", "short": "immutable-short.mp4", "thumbnail": "cover.jpg"})
    monkeypatch.setattr("delivery_snapshots.copy_snapshot", copy)
    monkeypatch.setattr("storage.object_etag", lambda _: "etag")
    enqueue = Mock(return_value="task")
    monkeypatch.setattr(exports, "enqueue", enqueue)
    response = client.post(f"/api/deliveries/{delivery.id}/prepare-prores", headers=HEADERS, json={})
    assert response.status_code == 202
    copy.assert_not_called()
    assert enqueue.call_args.args[3].endswith("lyric_video.mp4")
    db.refresh(delivery)
    assert delivery.published_file_keys is None


def test_legacy_listing_never_offers_an_unverified_working_master(client, delivery, db, monkeypatch):
    delivery.published_file_keys = None
    delivery.file_types = [*delivery.file_types, "umg_master"]
    db.commit()
    monkeypatch.setattr("storage._get_client", lambda: SimpleNamespace(head_object=lambda **_: {"ContentLength": 1000}))
    monkeypatch.setattr("storage.generate_signed_url", lambda key, **_: "https://files/" + key)
    response = client.get("/api/deliveries/items", headers=HEADERS)
    version = next(v for s in response.json()["songs"] for v in s["versions"] if v["delivery_id"] == delivery.id)
    master = next(f for f in version["files"] if f["type"] == "umg_master")
    assert master["url"] is None and master["available"] is False
    assert master["can_prepare"] is True


def _fake_transcode(monkeypatch, source, *, change=None):
    seen = {}
    def download(key, path):
        seen["source"] = key
        Path(path).write_bytes(b"published-source")
        return True
    def transcode(input_path, output_path, spec):
        seen["input"] = Path(input_path).read_bytes()
        seen["path"] = input_path
        Path(output_path).write_bytes(b"prores-from-published-source")
        if change:
            change()
    monkeypatch.setattr("storage.object_etag", lambda _: "etag")
    monkeypatch.setattr("storage.object_status", lambda _: "missing")
    monkeypatch.setattr("storage.download_object", download)
    monkeypatch.setattr("storage.upload_file", lambda path, key: key)
    monkeypatch.setattr("pipeline._transcode_to_prores", transcode)
    monkeypatch.setattr(exports, "_source_spec", lambda _: "native")
    return seen


def test_worker_uses_snapshot_and_cleans_up_local_export(delivery, db, monkeypatch):
    source = delivery.published_file_keys["video"]
    _, target = exports.export_identity(source, "etag", "umg_master")
    seen = _fake_transcode(monkeypatch, source)
    exports.materialize(delivery.id, "argentina", "umg_master", source, "etag", target)
    db.refresh(delivery)
    assert delivery.published_file_keys["umg_master"] == target
    assert seen["source"] == source and seen["input"] == b"published-source"
    assert not Path(seen["path"]).exists()
    assert delivery.published_revision == 2


def test_pointer_export_does_not_freeze_future_corrections(delivery, db, monkeypatch):
    from delivery_snapshots import portal_key
    delivery.published_file_keys = None
    db.commit()
    source = portal_key(delivery, "video")
    _, target = exports.export_identity(source, "etag", "umg_master")
    _fake_transcode(monkeypatch, source)
    exports.materialize(delivery.id, "argentina", "umg_master", source, "etag", target)
    db.refresh(delivery)
    assert delivery.published_file_keys is None
    assert delivery.published_revision == 2


def test_pointer_cache_follows_current_source_etag(delivery, db, monkeypatch):
    from delivery_snapshots import portal_key
    delivery.published_file_keys = None
    db.commit()
    source = portal_key(delivery, "video")
    _, old_target = exports.export_identity(source, "old-etag", "umg_master")
    monkeypatch.setattr("storage.object_status", lambda key: "exists" if key == old_target else "missing")
    monkeypatch.setattr("storage.object_etag", lambda _: "old-etag")
    assert exports.ready_key(delivery, "umg_master") == old_target
    monkeypatch.setattr("storage.object_etag", lambda _: "corrected-etag")
    assert exports.ready_key(delivery, "umg_master") is None


def test_correction_during_transcode_never_receives_old_master(delivery, db, monkeypatch):
    source = delivery.published_file_keys["video"]
    _, target = exports.export_identity(source, "etag", "umg_master")
    def publish_correction():
        with SessionLocal() as session:
            row = session.get(Delivery, delivery.id)
            row.published_file_keys = {"video": "corrected.mp4", "short": "corrected-short.mp4"}
            row.published_revision = 3
            session.commit()
    seen = _fake_transcode(monkeypatch, source, change=publish_correction)
    with pytest.raises(RuntimeError, match="publicación cambió"):
        exports.materialize(delivery.id, "argentina", "umg_master", source, "etag", target)
    db.refresh(delivery)
    assert delivery.published_file_keys == {"video": "corrected.mp4", "short": "corrected-short.mp4"}
    assert not Path(seen["path"]).exists()


@pytest.mark.parametrize("state,depth,enqueued", [("queued", 30, False), ("started", 30, False), ("failed", 0, True), ("finished", 0, True), ("not_found", 0, True)])
def test_enqueue_deduplicates_live_attempts_but_retries_failures(monkeypatch, state, depth, enqueued):
    import queue_jobs
    connection = SimpleNamespace(lock=lambda *_, **__: nullcontext())
    queue = SimpleNamespace(count=depth, enqueue=Mock())
    monkeypatch.setattr(queue_jobs, "_init_redis", lambda: (connection, None, queue))
    monkeypatch.setattr(queue_jobs, "_require_submissions_open", lambda: None)
    monkeypatch.setattr(exports, "_task_state", lambda *_: state)
    evict = Mock()
    monkeypatch.setattr(queue_jobs, "_evict_stale_rq_job", evict)
    exports.enqueue(1, "argentina", "umg_master", "t/j/source.mp4", "etag")
    assert queue.enqueue.called == enqueued
    assert evict.called == (state in {"failed", "finished"})


def test_enqueue_backpressure(monkeypatch):
    import queue_jobs
    queue = SimpleNamespace(count=exports.MAX_QUEUE_DEPTH, enqueue=Mock())
    connection = SimpleNamespace(lock=lambda *_, **__: nullcontext())
    monkeypatch.setattr(queue_jobs, "_init_redis", lambda: (connection, None, queue))
    monkeypatch.setattr(queue_jobs, "_require_submissions_open", lambda: None)
    monkeypatch.setattr(exports, "_task_state", lambda *_: "not_found")
    with pytest.raises(exports.QueueBusy):
        exports.enqueue(1, "argentina", "umg_master", "t/j/source.mp4", "etag")
    queue.enqueue.assert_not_called()


@pytest.mark.parametrize("rate", ["24000/1001", "30000/1001", "60000/1001", "24/1"])
def test_export_preserves_exact_native_frame_rate(monkeypatch, rate):
    monkeypatch.setattr("pipeline._probe_dims_fps", lambda _: (1920, 1080, rate))
    spec = exports._source_spec("source.mp4")
    assert spec.fps_str == rate and spec.width == 1920 and spec.height == 1080
    assert spec.prores_profile == 3 and spec.audio_codec == "pcm_s24le"


@pytest.mark.parametrize("dimensions", ["1920x1080", "1080x1920"])
def test_real_export_preserves_frames_and_encodes_broadcast_audio(tmp_path, dimensions):
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        pytest.skip("ffmpeg and ffprobe required")
    from pipeline import _transcode_to_prores
    source = tmp_path / "source.mp4"
    output = tmp_path / "master.mov"
    subprocess.run([
        "ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i",
        f"color=c=blue:s={dimensions}:r=30000/1001:d=0.3",
        "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=0.3",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
        "-shortest", str(source),
    ], check=True, capture_output=True, timeout=30)
    spec = exports._source_spec(str(source))
    _transcode_to_prores(str(source), str(output), spec, timeout_sec=60)
    def probe(path):
        return json.loads(subprocess.check_output([
            "ffprobe", "-v", "error", "-show_streams", "-of", "json", str(path),
        ], text=True, timeout=30))["streams"]
    video, audio = probe(output)
    assert video["codec_name"] == "prores" and video["profile"] == "HQ"
    assert video["pix_fmt"] == "yuv422p10le"
    assert video["r_frame_rate"] == "30000/1001"
    assert f'{video["width"]}x{video["height"]}' == dimensions
    assert video["nb_frames"] == probe(source)[0]["nb_frames"]
    assert audio["codec_name"] == "pcm_s24le"
    assert audio["sample_rate"] == "48000" and audio["channels"] == 2
