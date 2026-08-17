"""Retención de ProRes en R2 — la lógica que evita perder un máster.

El script libera storage borrando ProRes. La guarda dura es
`_is_regenerable`: si el MP4 fuente no está en el bucket, `ensure_prores_exists`
NO puede recrear el .mov y borrarlo pierde el máster para siempre. Medido en
prod: 304/312 másters (97,4%) son regenerables; los 8 restantes deben
preservarse SIEMPRE.
"""

import importlib.util
import pathlib

import pytest

_SCRIPT = (pathlib.Path(__file__).resolve().parents[3]
           / "scripts" / "r2_prores_retention.py")
_spec = importlib.util.spec_from_file_location("r2_prores_retention", _SCRIPT)
r2 = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(r2)


# ---------------------------------------------------------------------------
# Detección de versionados (copias superadas por una edición)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("key", [
    "tenant/job1/umg_master.mov.v0",
    "tenant/job1/umg_master.mov.v1",
    "tenant/job1/umg_short.mov.v12",
])
def test_detecta_prores_versionado(key):
    assert r2.VERSIONED_MOV.search(key)


@pytest.mark.parametrize("key", [
    "tenant/job1/umg_master.mov",      # vigente, NO es una versión superada
    "tenant/job1/lyric_video.mp4",
    "tenant/job1/lyric_video.mp4.v1",  # versionado pero MP4: no lo tocamos
])
def test_no_marca_como_versionado_lo_que_no_es_mov_vN(key):
    assert not r2.VERSIONED_MOV.search(key)


# ---------------------------------------------------------------------------
# Guarda dura: sólo es borrable lo que se puede regenerar desde el MP4
# ---------------------------------------------------------------------------

def test_master_con_mp4_fuente_es_regenerable():
    keys = {"t/j1/umg_master.mov", "t/j1/lyric_video.mp4"}
    assert r2._is_regenerable("t/j1/umg_master.mov", keys) is True


def test_master_sin_mp4_fuente_NO_es_regenerable():
    # Éste es el caso que perdería el máster para siempre.
    keys = {"t/j1/umg_master.mov"}
    assert r2._is_regenerable("t/j1/umg_master.mov", keys) is False


def test_short_usa_su_propia_fuente_no_la_del_master():
    # umg_short.mov se regenera de short.mp4; tener sólo lyric_video.mp4
    # no alcanza.
    keys = {"t/j1/umg_short.mov", "t/j1/lyric_video.mp4"}
    assert r2._is_regenerable("t/j1/umg_short.mov", keys) is False
    keys.add("t/j1/short.mp4")
    assert r2._is_regenerable("t/j1/umg_short.mov", keys) is True


def test_versionado_resuelve_su_fuente_ignorando_el_sufijo():
    keys = {"t/j1/umg_master.mov.v2", "t/j1/lyric_video.mp4"}
    assert r2._is_regenerable("t/j1/umg_master.mov.v2", keys) is True


def test_archivo_desconocido_nunca_es_regenerable():
    # Cualquier cosa que no sea un ProRes conocido se preserva por defecto.
    keys = {"t/j1/inputs/original.wav", "t/j1/lyric_video.mp4"}
    assert r2._is_regenerable("t/j1/inputs/original.wav", keys) is False
    assert r2._is_regenerable("t/j1/lyric_video.mp4", keys) is False


def test_la_fuente_debe_ser_del_mismo_job():
    # Un MP4 de OTRO job no habilita borrar este máster.
    keys = {"t/j1/umg_master.mov", "t/j2/lyric_video.mp4"}
    assert r2._is_regenerable("t/j1/umg_master.mov", keys) is False
