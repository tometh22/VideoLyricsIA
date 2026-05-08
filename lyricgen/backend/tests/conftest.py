"""Shared test fixtures."""

import os
import sys
import pytest

# Ensure backend modules are importable
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# main.py defaults ENVIRONMENT to "production", which then refuses to
# import without an explicit CORS_ORIGINS list (security guard against
# wildcard + credentials). Tests don't go through HTTP, so flag this
# process as test/dev BEFORE the first `from main import ...` triggers
# module-level CORS validation.
os.environ.setdefault("ENVIRONMENT", "test")
os.environ["DATABASE_URL"] = "sqlite:///test.db"
os.environ["JWT_SECRET"] = "test-secret-key-for-tests"
os.environ["ADMIN_PASSWORD"] = "testadmin123"
os.environ["RATE_LIMIT_ENABLED"] = "false"

from fastapi.testclient import TestClient
from database import Base, engine, SessionLocal, init_db


@pytest.fixture(scope="session", autouse=True)
def setup_db():
    """Create all tables once per test session."""
    init_db()
    yield
    # Cleanup
    Base.metadata.drop_all(bind=engine)
    try:
        os.unlink("test.db")
    except OSError:
        pass


@pytest.fixture
def db():
    """Yield a DB session, roll back after each test."""
    session = SessionLocal()
    yield session
    session.rollback()
    session.close()


def pytest_sessionfinish(session, exitstatus):
    """Skip Python interpreter teardown after a green run.

    librosa / audioread / sentry_sdk register C-backed atexit handlers
    that have been observed to abort with `terminate called without an
    active exception` when they tear down in CI (exit code 134, after
    every test passed). Once pytest reports its summary line, none of
    those teardowns add value — flush the streams and exit hard so the
    workflow exits 0 instead of "Aborted (core dumped)".

    Only applies to clean exits (exitstatus == 0). Failures still go
    through the normal path so the traceback / coredump is preserved.
    """
    if exitstatus == 0:
        sys.stdout.flush()
        sys.stderr.flush()
        os._exit(0)


@pytest.fixture
def client():
    """FastAPI test client."""
    from main import app
    with TestClient(app) as c:
        yield c


@pytest.fixture
def admin_token(client):
    """Login as admin and return token."""
    res = client.post("/auth/login", json={
        "username": "admin",
        "password": "testadmin123",
    })
    return res.json()["token"]


@pytest.fixture
def user_token(client):
    """Register a test user and return token."""
    import uuid
    username = f"testuser_{uuid.uuid4().hex[:6]}"
    res = client.post("/auth/register", json={
        "username": username,
        "password": "testpass12345",
        "email": f"{username}@test.com",
    })
    return res.json()["token"]


def auth(token):
    """Helper: return auth headers."""
    return {"Authorization": f"Bearer {token}"}
