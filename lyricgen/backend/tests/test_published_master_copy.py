from unittest.mock import Mock

import pytest
import storage


@pytest.mark.parametrize('source', ['tenant/job/umg_master.mov', 'tenant/job/umg_short.mov'])
def test_master_snapshot_uses_managed_copy_without_waiting_for_timeout(monkeypatch, source):
    client = Mock()
    monkeypatch.setattr(storage, '_get_client', lambda: client)
    monkeypatch.setattr(storage, 'object_exists', lambda _: True)
    assert storage.copy_object(source, source + '.published-test') is True
    client.copy_object.assert_not_called()
    client.copy.assert_called_once_with(CopySource={'Bucket': storage.R2_BUCKET, 'Key': source},
                                       Bucket=storage.R2_BUCKET, Key=source + '.published-test')


def test_master_copy_failure_is_not_reported_as_published(monkeypatch):
    client = Mock()
    client.copy.side_effect = RuntimeError('storage unavailable')
    monkeypatch.setattr(storage, '_get_client', lambda: client)
    monkeypatch.setattr(storage, 'object_exists', lambda _: True)
    with pytest.raises(RuntimeError, match='storage unavailable'):
        storage.copy_object('tenant/job/umg_master.mov', 'snapshot.mov')
