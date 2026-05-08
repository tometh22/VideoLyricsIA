"""Shared test fixtures."""

import os
import sys
import pytest

# Ensure backend modules are importable
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

os.environ["DATABASE_URL"] = "sqlite:///test.db"
os.environ["JWT_SECRET"] = "test-secret-key-for-tests"
os.environ["ADMIN_PASSWORD"] = "testadmin123"
os.environ["RATE_LIMIT_ENABLED"] = "false"
# CI defaults ENVIRONMENT unset → main.py sees "production" and the CORS
# check (PR #7) raises at import because CORS_ORIGINS is also unset.
# Tests don't make cross-origin requests, so flag the test env explicitly.
os.environ.setdefault("ENVIRONMENT", "development")

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


def pytest_unconfigure(config):
    """Hard-exit after pytest fully finishes (including its terminal summary).

    Some native libraries we depend on (moviepy/ImageMagick subprocess
    pools, boto3+urllib3 connection pools, librosa+audioread C
    extensions) leak threads or hold open handles that get destroyed
    in random order during interpreter shutdown. On CI Ubuntu runners
    that's been surfacing as `terminate called without an active
    exception` followed by SIGABRT (exit 134) — pytest reports
    "246 passed" and the runner still marks the job failed because of
    the post-summary abort.

    `pytest_unconfigure` is the LAST pytest hook to fire, after the
    terminal reporter has already printed the "X passed in Ys" summary.
    Calling os._exit() here preserves the visible summary and the real
    exit status, while bypassing the leaky teardown. xdist workers run
    a separate plugin lifecycle, so their cleanup is unaffected.
    """
    if os.environ.get("PYTEST_XDIST_WORKER"):
        return
    # `config.testsfailed` is the integer count of failed tests; non-zero
    # means we should propagate failure.
    exitstatus = 1 if getattr(config, "testsfailed", 0) else 0
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(exitstatus)
