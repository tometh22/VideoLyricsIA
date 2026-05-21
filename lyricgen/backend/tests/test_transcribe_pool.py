"""Pool-starvation regression guard (2026-05-21).

Incident: the dashboard (/usage, /jobs) froze on staging because
`/transcribe-uploaded` ran ~15-20s of Whisper + Vertex work while holding a
pooled DB connection (the request-scoped `db` was threaded through
`_run_transcription_for_job`). With only ~10 pool slots per process (Railway
100-conn cap / ~8 procs), a couple of concurrent transcriptions starved the
fast dashboard endpoints, which timed out after pool_timeout=30s.

Fix: transcription must NOT hold the request DB session across the heavy work.
It opens a short-lived `scoped_db()` only for the lrclib cache lookup, and the
callers close their request session before invoking it.

These tests fail if a future refactor reintroduces the held-session pattern.
"""
import inspect

import main


def test_transcription_fn_does_not_take_request_session():
    params = inspect.signature(main._run_transcription_for_job).parameters
    assert "db" not in params, (
        "_run_transcription_for_job must not receive the request-scoped db "
        "session — it would hold a pooled connection across Whisper/Vertex and "
        "starve /usage and /jobs. Use scoped_db() for the lrclib lookup."
    )


def test_transcription_uses_short_lived_scoped_db():
    src = inspect.getsource(main._run_transcription_for_job)
    assert "scoped_db()" in src, (
        "transcription must acquire a short-lived scoped_db() session for its "
        "only DB touch (lrclib), not hold the request connection."
    )


def test_transcribe_endpoints_release_connection_before_transcription():
    # Both upload paths must close the request session before the long call.
    for fn in (main.transcribe_uploaded, main.transcribe_endpoint):
        src = inspect.getsource(fn)
        assert "db.close()" in src, (
            f"{fn.__name__} must release the pooled connection (db.close()) "
            "before calling _run_transcription_for_job."
        )
