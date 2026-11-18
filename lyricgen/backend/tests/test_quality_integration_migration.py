"""Exercise the additive schema transition and retain system actor history."""
import importlib.util
from pathlib import Path
import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations


def test_upgrade_and_downgrade_preserve_existing_human_and_system_records():
    spec = importlib.util.spec_from_file_location('quality_migration', Path(__file__).parents[1] / 'alembic/versions/c0e2f4a6b8d0_quality_automation_schema.py')
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    engine = sa.create_engine('sqlite://')
    with engine.begin() as connection:
        connection.execute(sa.text('CREATE TABLE jobs (job_id VARCHAR PRIMARY KEY)'))
        connection.execute(sa.text('CREATE TABLE change_request_proposals (id INTEGER PRIMARY KEY, created_by INTEGER NOT NULL)'))
        connection.execute(sa.text('INSERT INTO change_request_proposals VALUES (1,7)'))
        with Operations.context(MigrationContext.configure(connection)):
            migration.upgrade()
            migration.upgrade()
            row = connection.execute(sa.text('SELECT created_by, actor_kind FROM change_request_proposals')).one()
            assert tuple(row) == (7,'user')
            connection.execute(sa.text("INSERT INTO change_request_proposals VALUES (2,NULL,'system')"))
            with pytest.raises(RuntimeError,match='system_actor_history'):
                migration.downgrade()
            assert connection.execute(sa.text('SELECT COUNT(*) FROM change_request_proposals')).scalar() == 2
            connection.execute(sa.text('DELETE FROM change_request_proposals WHERE id=2'))
            migration.downgrade()
            assert 'acoustic_review' not in {c['name'] for c in sa.inspect(connection).get_columns('jobs')}
            assert tuple(connection.execute(sa.text('SELECT id,created_by FROM change_request_proposals')).one()) == (1,7)


def test_readonly_inventory_reports_schema_and_missing_jobs(db):
    from scripts.audit_quality_integration import audit
    result = audit(['missing-job'])
    assert result['schema']['acoustic_review_available']
    assert 'actor_kind' in result['schema']['proposal_columns']
    assert result['jobs'][0]['missing'] == ['job']
    assert not result['runtime_execution_verified']
