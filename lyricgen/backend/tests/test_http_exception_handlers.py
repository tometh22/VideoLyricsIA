"""HTTP contracts for database races and pool backpressure."""

import asyncio

from sqlalchemy.exc import TimeoutError as SQLAlchemyTimeoutError
from sqlalchemy.orm.exc import ObjectDeletedError
from starlette.requests import Request

from main import _object_deleted_handler, _pool_timeout_handler


def _request(path="/health"):
    return Request({
        "type": "http",
        "method": "GET",
        "path": path,
        "headers": [],
        "query_string": b"",
        "server": ("testserver", 80),
        "scheme": "http",
        "client": ("testclient", 123),
    })


def test_object_deleted_error_is_a_clean_404():
    response = asyncio.run(
        _object_deleted_handler(_request("/jobs/gone"), ObjectDeletedError("Job", "gone"))
    )

    assert response.status_code == 404
    assert response.body == b'{"detail":"El recurso ya no existe."}'


def test_sqlalchemy_pool_timeout_is_503_with_retry_after():
    response = asyncio.run(
        _pool_timeout_handler(_request("/jobs/status"), SQLAlchemyTimeoutError("pool exhausted"))
    )

    assert response.status_code == 503
    assert response.headers["retry-after"] == "3"
    assert b"saturado" in response.body
