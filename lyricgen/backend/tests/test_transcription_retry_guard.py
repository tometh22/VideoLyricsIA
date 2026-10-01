"""Un fallo por deadline no debe reintentarse (incidente 2026-08-26, UMG Chile).

`Retry(max=2, interval=10)` en queue_jobs re-encola el job cuando el work-horse
muere — y muere por SIGKILL de `monitor_work_horse` a `job_timeout + 60s` si el
proceso no cierra limpio. Antes de esto NO había guard: el reintento volvía a
poner el job en "transcribing" y rehacía TODO, pagando demucs + whisperX otra
vez para tirar el resultado (el job ya estaba en estado terminal).

Medido en prod: el mismo job corrió 3 veces (21:47, 22:18, 22:52), las tres
facturadas, y la tercera dejó el rastro
`[TIMING] set_timing_source skipped — job is in terminal state`.

Un deadline es determinista: si agotó 900 s, los vuelve a agotar. Los fallos
transitorios (red, 5xx) SIGUEN reintentándose — para eso se puso el Retry.
"""

import pytest

import transcription_worker as tw


# --- clasificación del error_code -----------------------------------------

@pytest.mark.parametrize("texto", [
    "JobTimeoutException",
    "No pudimos completar la transcripción. Código: JobTimeoutException.",
    "TimeoutError: VOCALSEP exceeded wall-clock budget",
    "WHISPERX exceeded wall-clock budget",
    "Task exceeded maximum timeout value",
])
def test_deadline_se_clasifica_como_no_reintentable(texto):
    assert tw.classify_error_code(texto) == "transcription_deadline"
    assert tw.classify_error_code(texto) in tw._NON_RETRYABLE_ERROR_CODES


@pytest.mark.parametrize("texto", [
    "ConnectionError: connection reset by peer",
    "HTTPError: 502 Bad Gateway",
    "HTTPError: 503 Service Unavailable",
    "ReadTimeout",                      # timeout de socket, NO de deadline
    "ValueError: audio corrupto",
    "",
])
def test_transitorios_siguen_siendo_reintentables(texto):
    code = tw.classify_error_code(texto)
    assert code == "transcription_unknown"
    assert code not in tw._NON_RETRYABLE_ERROR_CODES


def test_read_timeout_no_se_confunde_con_deadline():
    """`ReadTimeout` de requests es un bache de red: SÍ hay que reintentarlo.

    Es la distinción que hace útil al guard — si clasificáramos por "timeout"
    a secas, bloquearíamos justo los reintentos que valen la pena.
    """
    assert tw.classify_error_code("ReadTimeout") != "transcription_deadline"


# --- el guard --------------------------------------------------------------

class _Row:
    def __init__(self, status, error_code):
        self.status = status
        self.error_code = error_code


def _stub_row(monkeypatch, row):
    """Sustituye get_job_model/SessionLocal para no tocar la DB."""
    import jobs as _jobs
    import database as _db
    monkeypatch.setattr(_jobs, "get_job_model", lambda db, jid: row)

    class _Sess:
        def close(self):
            pass

    monkeypatch.setattr(_db, "SessionLocal", lambda: _Sess())


def test_descarta_reintento_tras_fallo_por_deadline(monkeypatch):
    _stub_row(monkeypatch, _Row("transcription_failed", "transcription_deadline"))
    assert tw._should_discard_retry("job1") is True


def test_permite_reintento_tras_fallo_transitorio(monkeypatch):
    _stub_row(monkeypatch, _Row("transcription_failed", "transcription_unknown"))
    assert tw._should_discard_retry("job1") is False


def test_permite_reintento_si_el_job_no_esta_en_estado_terminal(monkeypatch):
    """Un job en "transcribing" es un reintento legítimo tras muerte del worker."""
    _stub_row(monkeypatch, _Row("transcribing", "transcription_deadline"))
    assert tw._should_discard_retry("job1") is False


def test_permite_reintento_si_el_job_no_existe(monkeypatch):
    _stub_row(monkeypatch, None)
    assert tw._should_discard_retry("job1") is False


def test_fail_open_si_la_db_explota(monkeypatch):
    """Sin poder leer el job, dejamos correr el reintento.

    Repetir trabajo es MUCHO más barato que negarle a un operador una
    transcripción que sí habría salido.
    """
    import database as _db

    def _boom():
        raise RuntimeError("db caida")

    monkeypatch.setattr(_db, "SessionLocal", _boom)
    assert tw._should_discard_retry("job1") is False
