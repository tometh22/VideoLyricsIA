"""R2 observability: normal deletes are logs, failures are Sentry signals.

Un borrado rutinario del reaper debe dejar trazabilidad en logs, pero no
crear un issue Sentry por cada input. Los fallos reales de delete_object sí
deben seguir llegando a Sentry.
"""
import sys
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


class _FakeScope:
    def __init__(self):
        self.fingerprint = None
        self.tags = {}
        self.extras = {}

    def set_tag(self, k, v):
        self.tags[k] = v

    def set_extra(self, k, v):
        self.extras[k] = v


class _FakeSentry:
    """Imita la superficie de sentry_sdk que usa storage.py."""

    def __init__(self):
        self.scope = _FakeScope()
        self.messages = []
        self.exceptions = []

    def push_scope(self):
        fake = self

        class _Ctx:
            def __enter__(self):
                return fake.scope

            def __exit__(self, *a):
                return False

        return _Ctx()

    def capture_message(self, message, level=None):
        self.messages.append((message, level))

    def capture_exception(self, exc):
        self.exceptions.append(exc)


def test_delete_input_normal_no_crea_issue_sentry():
    fake = _FakeSentry()
    with mock.patch.dict(sys.modules, {"sentry_sdk": fake}):
        import storage

        client = mock.Mock()
        with mock.patch.object(storage, "_get_client", return_value=client):
            storage.delete_object("inputs/tenant-x/job123/audio.mp3")

    assert fake.messages == []
    assert fake.exceptions == []
    # El delete real llegó a R2 igual.
    client.delete_object.assert_called_once()


def test_delete_no_input_no_alerta():
    """Borrar fuera de inputs/ (outputs, bg_cache) no dispara el tripwire."""
    fake = _FakeSentry()
    with mock.patch.dict(sys.modules, {"sentry_sdk": fake}):
        import storage

        with mock.patch.object(storage, "_get_client", return_value=mock.Mock()):
            storage.delete_object("bg_cache/abc123.mp4")

    assert fake.messages == []


def test_delete_failure_creates_sentry_exception():
    fake = _FakeSentry()
    failure = RuntimeError("R2 unavailable")
    with mock.patch.dict(sys.modules, {"sentry_sdk": fake}):
        import storage

        client = mock.Mock()
        client.delete_object.side_effect = failure
        with mock.patch.object(storage, "_get_client", return_value=client):
            try:
                storage.delete_object("inputs/tenant-x/job123/audio.mp3")
            except RuntimeError as exc:
                assert exc is failure
            else:  # pragma: no cover
                raise AssertionError("delete_object must propagate the R2 error")

    assert fake.messages == []
    assert fake.exceptions == [failure]
    assert fake.scope.tags["event"] == "r2.delete_failed"
    assert fake.scope.extras["r2.key"] == "inputs/tenant-x/job123/audio.mp3"
