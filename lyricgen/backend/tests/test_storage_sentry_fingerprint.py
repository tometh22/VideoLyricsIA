"""Regresión del agrupado de alertas R2 en Sentry (2026-06-10).

Los tripwires del incidente agus.cafisi ([R2-DELETE-INPUT] /
[R2-BULK-DELETE]) incluían la key única en el TÍTULO del mensaje → cada
borrado rutinario del reaper creaba un issue nuevo en el feed y enterraba
errores reales. La invariante que fija este archivo: el título es estable
por code path (Sentry agrupa) y la key viaja en extra (forensia intacta).
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


def test_delete_input_agrupa_por_caller_y_no_filtra_key_al_titulo():
    fake = _FakeSentry()
    with mock.patch.dict(sys.modules, {"sentry_sdk": fake}):
        import storage

        client = mock.Mock()
        with mock.patch.object(storage, "_get_client", return_value=client):
            storage.delete_object("inputs/tenant-x/job123/audio.mp3")

    assert len(fake.messages) == 1
    title, level = fake.messages[0]
    # Título estable: sin la key única (que rompía el agrupado).
    assert "inputs/tenant-x" not in title
    assert title.startswith("[R2-DELETE-INPUT] via ")
    assert level == "warning"
    # Fingerprint por code path + key preservada para forensia.
    assert fake.scope.fingerprint is not None
    assert fake.scope.fingerprint[0] == "r2-delete-input"
    assert fake.scope.extras["r2.key"] == "inputs/tenant-x/job123/audio.mp3"
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
