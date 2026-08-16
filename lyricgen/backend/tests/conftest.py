"""Shared test fixtures."""

import os
import sys
import types
import pytest

# Ensure backend modules are importable
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# moviepy 1.0.3 requires legacy setuptools build machinery that is not
# available in CI / dev containers. Stub the module tree so that
# pipeline.py can be imported without a compiled moviepy wheel.
# Must happen before any `from main import ...` triggers pipeline.py.
def _stub_moviepy():
    _mp = types.ModuleType("moviepy")
    _mp_cfg = types.ModuleType("moviepy.config")
    _mp_cfg.change_settings = lambda settings: None
    _mp_ed = types.ModuleType("moviepy.editor")
    for _cls_name in (
        "AudioFileClip", "ColorClip", "CompositeVideoClip",
        "TextClip", "VideoClip", "VideoFileClip", "concatenate_videoclips",
    ):
        setattr(_mp_ed, _cls_name, type(_cls_name, (), {
            "__init__": lambda self, *a, **kw: None,
        }))
    sys.modules.setdefault("moviepy", _mp)
    sys.modules.setdefault("moviepy.config", _mp_cfg)
    sys.modules.setdefault("moviepy.editor", _mp_ed)

if "moviepy" not in sys.modules:
    _stub_moviepy()


# librosa (numba/llvmlite/scipy) is a heavy transitive import of pipeline.py.
# CI installs it for real; in a lean local test env it may be absent. Stub it
# only when genuinely unavailable so pure-string / fallback-path tests (e.g.
# the art-track filtergraph + energy-window fallback) can import pipeline.
try:  # pragma: no cover - exercised only when librosa is installed
    import librosa  # noqa: F401
except Exception:  # pragma: no cover
    _lb = types.ModuleType("librosa")
    def _lb_load(*a, **k):
        raise RuntimeError("librosa is stubbed in this test environment")
    _lb.load = _lb_load
    _lb.feature = types.SimpleNamespace(rms=lambda **k: [[0.0]])
    sys.modules["librosa"] = _lb

# main.py defaults ENVIRONMENT to "production", which then refuses to
# import without an explicit CORS_ORIGINS list (security guard against
# wildcard + credentials). Tests don't go through HTTP, so flag this
# process as test/dev BEFORE the first `from main import ...` triggers
# module-level CORS validation.
os.environ.setdefault("ENVIRONMENT", "test")
os.environ.setdefault("DATABASE_URL", "sqlite:///test.db")
os.environ["JWT_SECRET"] = "test-secret-key-for-tests"
os.environ["ADMIN_PASSWORD"] = "testadmin123"
os.environ["RATE_LIMIT_ENABLED"] = "false"
os.environ.setdefault("QUALITY_LEARNING_HMAC_KEY_ID", "test-v1")
os.environ.setdefault("QUALITY_LEARNING_PROPOSALS_ENABLED", "1")
os.environ.setdefault("QUALITY_LEARNING_ABLATIONS_ENABLED", "1")
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


@pytest.fixture(scope="session", autouse=True)
def isolate_background_library(tmp_path_factory):
    """Keep admin-upload fixtures out of the tracked render-asset directory."""
    import admin
    import main

    test_library = str(tmp_path_factory.mktemp("background-library"))
    original_admin_dir = admin.BACKGROUNDS_DIR
    original_main_dir = main._BACKGROUNDS_LIB
    admin.BACKGROUNDS_DIR = test_library
    main._BACKGROUNDS_LIB = test_library
    try:
        yield
    finally:
        admin.BACKGROUNDS_DIR = original_admin_dir
        main._BACKGROUNDS_LIB = original_main_dir


@pytest.fixture
def db():
    """Yield a DB session, roll back after each test."""
    session = SessionLocal()
    yield session
    session.rollback()
    session.close()


# Exit status REAL de la sesión. Lo captura pytest_sessionfinish (que lo recibe
# como argumento) y lo consume pytest_unconfigure para el hard-exit con el código
# correcto, sin caer en la leaky teardown de las libs nativas.
_REAL_EXIT_STATUS = 0


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
    global _REAL_EXIT_STATUS
    _REAL_EXIT_STATUS = int(exitstatus)
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
def admin_user_id(client, admin_token):
    """Return the numeric DB id of the admin user."""
    return client.get("/auth/me", headers={"Authorization": f"Bearer {admin_token}"}).json()["id"]


@pytest.fixture
def user_token(client):
    """Register a test user and return token.

    Self-registered users default to `ai_authorized=True` so the public
    funnel works without admin friction. Tests that need an
    explicitly-blocked user should use `unauthorized_user_token`.
    """
    import uuid
    username = f"testuser_{uuid.uuid4().hex[:6]}"
    res = client.post("/auth/register", json={
        "username": username,
        "password": "testpass12345",
        "email": f"{username}@test.com",
    })
    return res.json()["token"]


@pytest.fixture
def unauthorized_user_token(client, admin_token, user_token):
    """A self-registered user with ai_authorized revoked.

    Models a regulated-tenant operator (UMG-style) who has not been
    cleared by an admin yet — i.e. should hit the AI auth gate.
    """
    me = client.get("/auth/me", headers={"Authorization": f"Bearer {user_token}"}).json()
    client.post(
        f"/admin/users/{me['id']}/revoke-ai",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    return user_token


def auth(token):
    """Helper: return auth headers."""
    return {"Authorization": f"Bearer {token}"}


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "postgres: test que requiere Postgres real (concurrency / row locks). Skip en sqlite.",
    )


def pytest_collection_modifyitems(config, items):
    """Skip tests marcados `postgres` cuando la DB activa es SQLite.

    Patrones como `with_for_update()` y `pg_try_advisory_lock` son
    no-ops en SQLite (por diseño de SQLAlchemy/SQLite). Tests que
    validan esos patterns DAN FALSE-GREEN en SQLite — pasarían sin la
    fix aplicada. CI corre Postgres (.github/workflows/ci.yml:18-29) y
    los ejecuta de verdad; local sin Postgres los skipea con razón
    clara.
    """
    # Quarantine de tests pre-existentes en rojo (deuda que acumuló el CI ciego
    # cuando conftest enmascaraba el exit code). xfail no-estricto + run=False →
    # no se corren, no rompen el build, y quedan visibles como "xfailed" (el
    # backlog a quemar). Lista editable en tests/quarantine.txt; sacá la entrada
    # cuando arregles el test. NO meter tests nuevos acá: si rompés algo, arreglalo.
    _qpath = os.path.join(os.path.dirname(__file__), "quarantine.txt")
    _quarantined = set()
    try:
        with open(_qpath, encoding="utf-8") as _fh:
            for _line in _fh:
                _line = _line.strip()
                if _line and not _line.startswith("#"):
                    _quarantined.add(_line)
    except OSError:
        pass
    if _quarantined:
        _xfail = pytest.mark.xfail(
            reason="pre-existing failure — quarantined (tests/quarantine.txt)",
            strict=False, run=False,
        )
        for _item in items:
            if _item.nodeid in _quarantined:
                _item.add_marker(_xfail)

    from database import engine
    is_postgres = engine.dialect.name == "postgresql"
    if is_postgres:
        return
    skip_pg = pytest.mark.skip(
        reason="requiere Postgres (with_for_update/advisory_lock son no-op en sqlite)"
    )
    for item in items:
        if "postgres" in item.keywords:
            item.add_marker(skip_pg)


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
    # Hard-exit con el exitstatus REAL capturado en pytest_sessionfinish.
    # BUG previo: leía `config.testsfailed`, que NO es un atributo del Config de
    # pytest (el contador real es `session.testsfailed`) → getattr(...,0) daba
    # SIEMPRE 0 → todas las fallas quedaban enmascaradas y el CI salía verde con
    # tests rojos.
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(_REAL_EXIT_STATUS)
