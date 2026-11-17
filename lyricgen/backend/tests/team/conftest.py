"""Coordination endpoints need the real ORM/auth router, not media/render runtimes."""

import pytest
from database import Base, engine, SessionLocal
from auth import ensure_default_admin


@pytest.fixture(scope="session", autouse=True)
def setup_db():
    Base.metadata.create_all(engine)
    with SessionLocal() as session:
        ensure_default_admin(session)
    yield
    Base.metadata.drop_all(engine)


@pytest.fixture(scope="session", autouse=True)
def isolate_background_library():
    yield
