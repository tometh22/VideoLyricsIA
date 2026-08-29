"""El replay receive del middleware de reintento de DB debe propagar disconnect.

BUG (PR #1200, en prod desde 2026-08-25): `_make_replay_receive` esperaba sobre
un `asyncio.Event()` que nunca se hacía `set()`, así que la segunda llamada
colgaba para siempre.

Disparador real: `/events/{job_id}` (SSE). El middleware usa este replay en GETs
cuando `attempt > 0` — o sea cuando la request se reintentó por un
`OperationalError` transitorio. El `listen_for_disconnect` de `StreamingResponse`
quedaba colgado ahí y, como en un SSE el stream no termina solo, el server nunca
se enteraba de que el cliente se había ido: el generador seguía corriendo y
ocupando un worker de uvicorn.
"""

import asyncio

import pytest

from main import _make_replay_receive


@pytest.mark.asyncio
async def test_primera_llamada_reproduce_el_body():
    recv = _make_replay_receive(b'{"a":1}')
    msg = await recv()
    assert msg["type"] == "http.request"
    assert msg["body"] == b'{"a":1}'
    assert msg["more_body"] is False


@pytest.mark.asyncio
async def test_segunda_llamada_propaga_el_disconnect_real():
    """Es la regresión: antes colgaba para siempre en el Event."""
    async def upstream():
        return {"type": "http.disconnect"}

    recv = _make_replay_receive(b"", upstream=upstream)
    await recv()
    msg = await asyncio.wait_for(recv(), timeout=2.0)
    assert msg["type"] == "http.disconnect"


@pytest.mark.asyncio
async def test_no_inventa_un_disconnect_antes_de_tiempo():
    """Mientras el cliente sigue conectado, el replay NO debe sintetizar
    un disconnect: eso cancelaría el SSE antes de su primer evento — el fallo
    que el comentario del middleware ya documentaba."""
    entregado = asyncio.Event()

    async def upstream():
        await entregado.wait()          # cliente conectado, sin novedades
        return {"type": "http.disconnect"}

    recv = _make_replay_receive(b"", upstream=upstream)
    await recv()
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(recv(), timeout=0.4)

    entregado.set()
    msg = await asyncio.wait_for(recv(), timeout=2.0)
    assert msg["type"] == "http.disconnect"


@pytest.mark.asyncio
async def test_sin_upstream_conserva_la_espera_historica():
    """Sin canal real seguimos esperando en vez de sintetizar un disconnect.

    Devolver `http.disconnect` de una cancelaría el stream antes del primer
    evento. La espera es el mal menor y sólo ocurre si nadie pasa `upstream`.
    """
    recv = _make_replay_receive(b"")
    await recv()
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(recv(), timeout=0.4)
