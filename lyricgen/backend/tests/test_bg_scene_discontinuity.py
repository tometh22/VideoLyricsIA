"""Tests del detector de corte de escena en fondos (guardia 2026-06-10).

Incidente mural: Veo i2v con semilla≠prompt devuelve un clip que morphea
de una escena a otra; el palindrome lo repite todo el video. El detector
compara primer vs último frame. Calibración con clips reales del
incidente (implementación ffmpeg): contaminados 0.261-0.273, sanos
0.004-0.063, umbral default 0.18.

El núcleo (_frame_pair_discontinuity) es numpy puro — se testea sin
ffmpeg ni moviepy (el conftest stubea moviepy). La extracción de frames
se testea aparte, gateada por la presencia del binario ffmpeg.
"""
import shutil
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pipeline import (  # noqa: E402
    _BG_SCENE_CUT_THRESHOLD,
    _bg_scene_discontinuity,
    _frame_pair_discontinuity,
)

_FFMPEG = shutil.which("ffmpeg") is not None


def _solid(r, g, b, noise=0.0, seed=0):
    rng = np.random.default_rng(seed)
    f = np.zeros((180, 320, 3), dtype="float32")
    f[..., 0], f[..., 1], f[..., 2] = r, g, b
    if noise:
        f = np.clip(f + rng.normal(0, noise, f.shape), 0, 255)
    return f


class TestNucleoPuro:
    def test_misma_escena_mide_bajo(self):
        """Mismo ambiente con ruido (gente moviéndose, grano) → bajo."""
        a = _solid(60, 80, 120, noise=12, seed=1)
        b = _solid(60, 80, 120, noise=12, seed=2)
        assert _frame_pair_discontinuity(a, b) < _BG_SCENE_CUT_THRESHOLD

    def test_escenas_distintas_mide_alto(self):
        """El caso mural→café destilado: paletas y contenido distintos."""
        a = _solid(150, 60, 30)   # atardecer cálido (mural)
        b = _solid(40, 60, 110)   # interior frío (café)
        assert _frame_pair_discontinuity(a, b) >= _BG_SCENE_CUT_THRESHOLD

    def test_frame_identico_es_cero(self):
        a = _solid(100, 100, 100)
        assert _frame_pair_discontinuity(a, a) == 0.0


class TestExtraccion:
    @pytest.mark.skipif(not _FFMPEG, reason="requiere binario ffmpeg")
    def test_clip_con_corte_mide_alto(self, tmp_path):
        """Mitad roja, mitad azul vía lavfi — integración completa."""
        p = tmp_path / "cut.mp4"
        subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error",
             "-f", "lavfi", "-i", "color=c=darkred:s=320x180:r=12:d=2",
             "-f", "lavfi", "-i", "color=c=navy:s=320x180:r=12:d=2",
             "-filter_complex", "[0:v][1:v]concat=n=2:v=1:a=0",
             "-c:v", "libx264", "-pix_fmt", "yuv420p", str(p)],
            check=True, timeout=120,
        )
        assert _bg_scene_discontinuity(str(p)) >= _BG_SCENE_CUT_THRESHOLD

    @pytest.mark.skipif(not _FFMPEG, reason="requiere binario ffmpeg")
    def test_clip_estable_mide_bajo(self, tmp_path):
        p = tmp_path / "single.mp4"
        subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error",
             "-f", "lavfi", "-i", "color=c=darkslateblue:s=320x180:r=12:d=4",
             "-c:v", "libx264", "-pix_fmt", "yuv420p", str(p)],
            check=True, timeout=120,
        )
        assert _bg_scene_discontinuity(str(p)) < _BG_SCENE_CUT_THRESHOLD

    def test_fail_open_con_archivo_invalido(self, tmp_path):
        """Contrato del QA de fondo: un error del detector jamás bloquea
        el render — devuelve 0.0."""
        p = tmp_path / "garbage.mp4"
        p.write_bytes(b"esto no es un mp4")
        assert _bg_scene_discontinuity(str(p)) == 0.0


def test_umbral_calibrado_con_margen():
    """Pinned: el umbral default debe quedar entre las mediciones reales
    del incidente (sanos <=0.063, contaminados >=0.261). Si alguien lo
    mueve fuera de ese rango, este test lo discute."""
    assert 0.063 < _BG_SCENE_CUT_THRESHOLD < 0.261
