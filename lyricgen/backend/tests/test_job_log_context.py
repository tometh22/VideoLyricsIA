"""Tests del job-context en logs (observability 2026-06-10).

Autopsia del incidente del mural: las líneas [BG] no llevaban job_id y
filtrar Railway por job era imposible. El _JobContextFilter inyecta el
job activo (contextvar) en cada record; el _JsonFormatter ya lo emite.
"""
import json
import logging
import sys
from io import StringIO
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from observability import (  # noqa: E402
    _JobContextFilter,
    _JsonFormatter,
    clear_job_log_context,
    set_job_log_context,
)


def _make_logger(stream):
    logger = logging.getLogger("genly.test.jobctx")
    logger.setLevel(logging.INFO)
    logger.handlers = []
    h = logging.StreamHandler(stream)
    h.setFormatter(_JsonFormatter())
    h.addFilter(_JobContextFilter())
    logger.addHandler(h)
    logger.propagate = False
    return logger


def _last_line(stream):
    return json.loads(stream.getvalue().strip().split("\n")[-1])


def test_job_id_se_inyecta_en_toda_linea():
    out = StringIO()
    log = _make_logger(out)
    set_job_log_context("abc123def456")
    try:
        log.info("[BG] Veo cache STORED: cache/veo/xyz.mp4")
        payload = _last_line(out)
        assert payload["job_id"] == "abc123def456"
        assert "[BG]" in payload["msg"]
    finally:
        clear_job_log_context()


def test_sin_contexto_no_se_inventa_job_id():
    out = StringIO()
    log = _make_logger(out)
    clear_job_log_context()
    log.info("linea sin job activo")
    assert "job_id" not in _last_line(out)


def test_extra_explicito_gana_al_contexto():
    """Quien ya pasa extra={'job_id': ...} mantiene su valor exacto."""
    out = StringIO()
    log = _make_logger(out)
    set_job_log_context("contexto999")
    try:
        log.info("linea con extra propio", extra={"job_id": "explicito111"})
        assert _last_line(out)["job_id"] == "explicito111"
    finally:
        clear_job_log_context()


def test_entrypoint_siguiente_pisa_al_anterior():
    """RQ corre jobs secuenciales en el mismo proceso: el set del job N+1
    reemplaza al de N sin reset explícito."""
    out = StringIO()
    log = _make_logger(out)
    set_job_log_context("job-viejo")
    set_job_log_context("job-nuevo")
    try:
        log.info("linea del job nuevo")
        assert _last_line(out)["job_id"] == "job-nuevo"
    finally:
        clear_job_log_context()
