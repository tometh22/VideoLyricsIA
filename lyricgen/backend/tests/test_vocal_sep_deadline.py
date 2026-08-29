"""Presupuesto del THREAD de separate_vocals (incidente 2026-08-26/28).

`separate_vocals` corre dentro de `asyncio.to_thread`, que NO es cancelable.
Todo lo que haga sin timeout deja el thread vivo después de que el
`asyncio.wait_for` del caller se rindió, y ese huérfano bloquea el
`loop.shutdown_default_executor()` del teardown de `asyncio.run` — que en
Python 3.11 no acepta timeout. Resultado en prod: RQ mataba el job por death
penalty y se tiraban transcripciones YA terminadas.

Estos tests fijan las dos garantías que lo evitan:
  1. presupuesto y `wait_for` salen de la MISMA env var;
  2. la descarga del stem está acotada y respeta lo que queda del deadline.
"""

import sys
import types

import pytest


@pytest.fixture
def vocal_sep(monkeypatch):
    monkeypatch.delenv("REPLICATE_BUDGET_S_DEMUCS", raising=False)
    import vocal_sep as _vs
    return _vs


def test_budget_and_wait_for_share_one_source(vocal_sep, monkeypatch):
    """`thread_budget_s()` = presupuesto + margen de post-proceso.

    main.py deriva de acá el timeout de sus `wait_for`. Si se separan,
    vuelve el huérfano: el loop abandona antes de que el thread se rinda.
    """
    assert vocal_sep.budget_s() == vocal_sep._DEFAULT_BUDGET_S
    assert (vocal_sep.thread_budget_s()
            == vocal_sep.budget_s() + vocal_sep._POST_PROCESS_MARGIN_S)
    assert vocal_sep.thread_budget_s() > vocal_sep.budget_s(), (
        "el wait_for del caller DEBE ser mayor que el presupuesto interno")

    monkeypatch.setenv("REPLICATE_BUDGET_S_DEMUCS", "300")
    assert vocal_sep.budget_s() == 300.0
    assert vocal_sep.thread_budget_s() == 330.0


def test_default_budget_covers_observed_worst_case(vocal_sep):
    """987 s fue la corrida exitosa más lenta medida en prod (28/08).

    Con 900 se perdía 1 de 11. Este test es el recordatorio de por qué el
    default es 1200 y no un número redondo cualquiera.
    """
    assert vocal_sep._DEFAULT_BUDGET_S >= 987.0


def test_download_uses_bounded_request_not_urlretrieve(vocal_sep, monkeypatch, tmp_path):
    """La descarga va por `requests` con (connect, read) explícitos.

    `urllib.request.urlretrieve` no acepta timeout y el proyecto no setea
    `socket.setdefaulttimeout`: era una espera potencialmente infinita.
    """
    seen = {}

    class _Resp:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def raise_for_status(self): pass
        def iter_content(self, chunk_size=None): yield b"x" * 2048

    fake = types.ModuleType("requests")

    def _get(url, stream=None, timeout=None):
        seen["url"] = url
        seen["timeout"] = timeout
        return _Resp()

    fake.get = _get
    monkeypatch.setitem(sys.modules, "requests", fake)

    import urllib.request
    monkeypatch.setattr(
        urllib.request, "urlretrieve",
        lambda *a, **k: pytest.fail("urlretrieve no debe usarse: no tiene timeout"))

    dest = tmp_path / "stem.wav"
    ok = vocal_sep._download(
        "https://replicate.delivery/pbxt/abc/vocals.wav", str(dest))

    assert ok is True
    connect_t, read_t = seen["timeout"]
    assert connect_t == vocal_sep._DOWNLOAD_CONNECT_TIMEOUT_S
    assert read_t > 0


def test_download_read_timeout_shrinks_to_remaining_budget(vocal_sep, monkeypatch, tmp_path):
    """Con poco presupuesto restante, el read timeout se recorta.

    Así la descarga no puede comerse tiempo que no le sobra.
    """
    seen = {}

    class _Resp:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def raise_for_status(self): pass
        def iter_content(self, chunk_size=None): yield b"x" * 16

    fake = types.ModuleType("requests")

    def _get(url, stream=None, timeout=None):
        seen["timeout"] = timeout
        return _Resp()

    fake.get = _get
    monkeypatch.setitem(sys.modules, "requests", fake)

    now = vocal_sep._t.monotonic()
    dest = tmp_path / "stem.wav"
    vocal_sep._download(
        "https://replicate.delivery/pbxt/abc/vocals.wav", str(dest),
        deadline=now + 5.0,        # quedan ~5s, mucho menos que el read default
    )

    _connect_t, read_t = seen["timeout"]
    assert read_t <= 5.0, (
        f"read timeout {read_t} debería recortarse al presupuesto restante")


def test_download_aborts_between_chunks_past_deadline(vocal_sep, monkeypatch, tmp_path):
    """Un servidor que gotea nunca dispara el timeout POR READ de requests.

    Por eso re-chequeamos el deadline entre chunks.
    """
    class _Resp:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def raise_for_status(self): pass
        def iter_content(self, chunk_size=None):
            for _ in range(1000):
                yield b"x" * 1024

    fake = types.ModuleType("requests")
    fake.get = lambda url, stream=None, timeout=None: _Resp()
    monkeypatch.setitem(sys.modules, "requests", fake)

    dest = tmp_path / "stem.wav"
    ok = vocal_sep._download(
        "https://replicate.delivery/pbxt/abc/vocals.wav", str(dest),
        deadline=vocal_sep._t.monotonic() - 1.0,   # ya vencido
    )

    assert ok is False, "pasado el deadline la descarga debe abortar"


def test_download_still_refuses_non_replicate_hosts(vocal_sep, tmp_path):
    """El guard de SSRF (PR #284) sigue en pie tras el refactor."""
    dest = tmp_path / "stem.wav"
    assert vocal_sep._download("https://evil.example.com/x.wav", str(dest)) is False
    assert vocal_sep._download("file:///etc/passwd", str(dest)) is False
