"""CV2 (audit Sprint 2 2026-05-25) — multipart_abort retry + orphan list.

Sin retry, R2 transient errors dejaban orphans forever en el bucket.
Con retry exponencial + idempotent NoSuchUpload handling, el cleanup
es robusto. list_orphan_multipart_uploads expone visibility.
"""

import pytest
from unittest.mock import MagicMock, patch


def test_multipart_abort_succeeds_first_try(monkeypatch):
    """Happy path: abort exitoso al primer intento."""
    import storage

    mock_client = MagicMock()
    mock_client.abort_multipart_upload.return_value = {}
    monkeypatch.setattr(storage, "_get_client", lambda: mock_client)

    result = storage.multipart_abort("key1", "upload1")
    assert result is True
    assert mock_client.abort_multipart_upload.call_count == 1


def test_multipart_abort_retries_on_transient_error(monkeypatch):
    """Retry exponential: fails 2 veces, succeeds on 3rd attempt."""
    import storage

    mock_client = MagicMock()
    # Fail twice, then succeed.
    mock_client.abort_multipart_upload.side_effect = [
        Exception("transient R2 error"),
        Exception("transient R2 error"),
        {},
    ]
    monkeypatch.setattr(storage, "_get_client", lambda: mock_client)
    monkeypatch.setattr(storage, "logger", MagicMock())  # silence logs

    # Speed up: monkeypatch time.sleep to no-op.
    monkeypatch.setattr("time.sleep", lambda _: None)

    result = storage.multipart_abort("key1", "upload1", max_retries=3)
    assert result is True
    assert mock_client.abort_multipart_upload.call_count == 3


def test_multipart_abort_fails_after_max_retries(monkeypatch):
    """Si todos los retries fallan, retorna False (no raise)."""
    import storage

    mock_client = MagicMock()
    mock_client.abort_multipart_upload.side_effect = Exception("persistent error")
    monkeypatch.setattr(storage, "_get_client", lambda: mock_client)
    monkeypatch.setattr(storage, "logger", MagicMock())
    monkeypatch.setattr("time.sleep", lambda _: None)

    result = storage.multipart_abort("key1", "upload1", max_retries=3)
    assert result is False
    assert mock_client.abort_multipart_upload.call_count == 3


def test_multipart_abort_idempotent_on_no_such_upload(monkeypatch):
    """NoSuchUpload = ya fue abortada → tratamos como success (idempotent)."""
    import storage

    mock_client = MagicMock()
    mock_client.abort_multipart_upload.side_effect = Exception("NoSuchUpload: blah")
    monkeypatch.setattr(storage, "_get_client", lambda: mock_client)

    result = storage.multipart_abort("key1", "upload1")
    assert result is True
    # No retry — la 1st call ya retornó porque es idempotent.
    assert mock_client.abort_multipart_upload.call_count == 1


def test_multipart_abort_no_client_returns_false(monkeypatch):
    """Si R2 client no está configurado, retorna False."""
    import storage
    monkeypatch.setattr(storage, "_get_client", lambda: None)
    assert storage.multipart_abort("k", "u") is False


def test_list_orphan_multipart_uploads_filters_by_age(monkeypatch):
    """Solo retorna uploads más viejos que `older_than_hours`."""
    import storage
    from datetime import datetime, timedelta, timezone

    now = datetime.now(timezone.utc)
    young = now - timedelta(hours=1)
    old = now - timedelta(hours=48)

    mock_client = MagicMock()
    mock_paginator = MagicMock()
    mock_paginator.paginate.return_value = [
        {
            "Uploads": [
                {"Key": "k_young", "UploadId": "u_y", "Initiated": young},
                {"Key": "k_old", "UploadId": "u_o", "Initiated": old},
            ]
        }
    ]
    mock_client.get_paginator.return_value = mock_paginator
    monkeypatch.setattr(storage, "_get_client", lambda: mock_client)

    result = storage.list_orphan_multipart_uploads(older_than_hours=24)
    assert len(result) == 1
    assert result[0]["key"] == "k_old"
    assert result[0]["upload_id"] == "u_o"
    assert result[0]["age_hours"] >= 24


def test_list_orphan_multipart_uploads_no_client_returns_empty(monkeypatch):
    """Sin client → retorna [], no raise."""
    import storage
    monkeypatch.setattr(storage, "_get_client", lambda: None)
    assert storage.list_orphan_multipart_uploads() == []
