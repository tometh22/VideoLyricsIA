"""Tests del hardening de caches de fondo (audit adversarial 2026-06-09).

Dos vías por las que un job podía recibir el fondo de OTRO job/params:

1. `bg_cache_key` (Capa C pre-gen) venía del cliente y se usaba sin
   validar: un key stale o ajeno servía cualquier objeto de bg_cache/.
   Fix: pipeline._validate_bg_cache_key recomputa el hash server-side con
   la MISMA función del preview y descarta mismatches.

2. El cache de Veo image-to-video hasheaba prompt+model+params pero NO la
   imagen semilla: misma canción re-rendereada con otra foto recibía el
   clip de la foto anterior. Fix: pipeline._seed_image_digest entra a
   cache_params["img"].
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bg_preview import compute_bg_cache_key  # noqa: E402
from pipeline import _seed_image_digest, _validate_bg_cache_key, _veo_cache_key  # noqa: E402

_JOB_PARAMS = dict(
    job_id="testjob123",
    artist="Rata Blanca",
    song_title="La Leyenda Del Hada Y El Mago",
    style="auto",
    movement_style="estatico",
    effect="",
    custom_colors="",
    genre="rock",
    concept="",
    background_hint="",
    bg_verbatim=False,
    match_lyrics=True,
)


def _preview_params(**overrides):
    """Los params como los hashea el preview (App.jsx previewEntry +
    bg_preview.compute_bg_cache_key). Espejo del shape del request real."""
    p = {
        "artist": _JOB_PARAMS["artist"],
        "song_title": _JOB_PARAMS["song_title"],
        "style": _JOB_PARAMS["style"],
        "movement_style": _JOB_PARAMS["movement_style"],
        "effect": _JOB_PARAMS["effect"],
        "custom_colors": _JOB_PARAMS["custom_colors"],
        "genre": _JOB_PARAMS["genre"],
        "concept": _JOB_PARAMS["concept"],
        "background_hint": _JOB_PARAMS["background_hint"],
        "bg_verbatim": _JOB_PARAMS["bg_verbatim"],
        "background_mode": "veo",
        "animate_image": False,
        "match_lyrics": _JOB_PARAMS["match_lyrics"],
    }
    p.update(overrides)
    return p


class TestValidateBgCacheKey:
    def test_key_legitimo_pasa(self):
        """El key que el preview computó para ESTOS params se acepta."""
        key = compute_bg_cache_key(_preview_params())
        assert _validate_bg_cache_key(key, **_JOB_PARAMS) == key

    def test_key_stale_se_descarta(self):
        """El operador cambió un param después del preview (la familia del
        bug de Ana M.): el key de los params VIEJOS no puede servir el
        fondo viejo para el render nuevo."""
        stale = compute_bg_cache_key(_preview_params(effect="light"))
        assert _validate_bg_cache_key(stale, **_JOB_PARAMS) is None

    def test_key_de_otra_cancion_se_descarta(self):
        """Un key de otro artista/tema (cross-job, cross-tenant o request
        crafteado) nunca sirve fondo para este job."""
        foreign = compute_bg_cache_key(
            _preview_params(artist="Catupecu Machu", song_title="A Veces Vuelvo")
        )
        assert _validate_bg_cache_key(foreign, **_JOB_PARAMS) is None

    def test_key_basura_se_descarta(self):
        assert _validate_bg_cache_key("zzzzzzzzzzzz", **_JOB_PARAMS) is None

    def test_params_none_normalizan_como_el_preview(self):
        """/generate pasa None para hint/concept vacíos; el preview manda
        "". Ambos tienen que hashear igual o todo cache-hit legítimo se
        pierde (regresión de costo silenciosa)."""
        key = compute_bg_cache_key(_preview_params(background_hint="", concept=""))
        params = dict(_JOB_PARAMS, background_hint=None, concept=None)
        assert _validate_bg_cache_key(key, **params) == key


class TestSeedImageDigest:
    def test_imagenes_distintas_digest_distinto(self, tmp_path):
        a = tmp_path / "a.png"
        b = tmp_path / "b.png"
        a.write_bytes(b"imagen-de-bersuit")
        b.write_bytes(b"imagen-nueva-distinta")
        assert _seed_image_digest(str(a)) != _seed_image_digest(str(b))

    def test_misma_imagen_digest_estable(self, tmp_path):
        a = tmp_path / "a.png"
        a.write_bytes(b"misma-imagen")
        assert _seed_image_digest(str(a)) == _seed_image_digest(str(a))

    def test_imagen_ilegible_marca_unica_por_job(self):
        d1 = _seed_image_digest("/no/existe.png", "job1")
        d2 = _seed_image_digest("/no/existe.png", "job2")
        assert d1 != d2 and d1.startswith("unreadable:")

    def test_veo_cache_key_cambia_con_la_imagen(self, tmp_path):
        """El caso del hallazgo: mismo prompt+model+ns, dos imágenes
        semilla → cache keys de Veo DISTINTOS."""
        a = tmp_path / "a.png"
        b = tmp_path / "b.png"
        a.write_bytes(b"foto-vieja")
        b.write_bytes(b"foto-nueva")
        base = {"aspectRatio": "16:9", "sampleCount": 1, "generateAudio": False,
                "blur_sigma": 1.0, "ns": "rata blanca|hada y el mago"}
        k_a = _veo_cache_key("prompt", "veo-3.1", {**base, "img": _seed_image_digest(str(a))})
        k_b = _veo_cache_key("prompt", "veo-3.1", {**base, "img": _seed_image_digest(str(b))})
        k_none = _veo_cache_key("prompt", "veo-3.1", base)
        assert len({k_a, k_b, k_none}) == 3


class TestJobBgCacheKeyParity:
    """v6: job_bg_cache_key es la única fuente de los hardcodes que main.py
    (recompute) y pipeline (validación defensiva) comparten."""

    def test_paridad_con_el_preview(self):
        from bg_preview import job_bg_cache_key
        assert job_bg_cache_key(**{
            k: v for k, v in _JOB_PARAMS.items() if k != "job_id"
        }) == compute_bg_cache_key(_preview_params())

    def test_paridad_con_espacios_y_case(self):
        """La normalización vive SOLO en compute_bg_cache_key — inputs con
        espacios/case distintos hashean igual en ambos lados."""
        from bg_preview import job_bg_cache_key
        job_side = job_bg_cache_key(
            artist="  Rata Blanca ", song_title=_JOB_PARAMS["song_title"],
            style="AUTO", movement_style=" Estatico ",
            effect="", custom_colors="", genre="ROCK", concept="",
            background_hint="", bg_verbatim=False, match_lyrics=True,
        )
        assert job_side == compute_bg_cache_key(_preview_params())

    def test_falla_devuelve_none(self, monkeypatch):
        import bg_preview as bp
        monkeypatch.setattr(bp, "compute_bg_cache_key",
                            lambda p: (_ for _ in ()).throw(RuntimeError("boom")))
        assert bp.job_bg_cache_key(**{
            k: v for k, v in _JOB_PARAMS.items() if k != "job_id"
        }) is None


class TestLyricsFingerprint:
    """v6: con match_lyrics el prompt del fondo depende de la LETRA — el
    hash la incluye como fingerprint de TEXTO (timestamps fuera: el 93% de
    las correcciones del operador son de timing y no deben rotar el key)."""

    def test_misma_letra_distinto_timing_mismo_key(self):
        a = compute_bg_cache_key(_preview_params(lyrics_text="hola  Mundo cruel"))
        b = compute_bg_cache_key(_preview_params(lyrics_text="Hola mundo   CRUEL"))
        assert a == b  # normalización: lower + espacios colapsados

    def test_letra_distinta_key_distinto(self):
        a = compute_bg_cache_key(_preview_params(lyrics_text="una letra"))
        b = compute_bg_cache_key(_preview_params(lyrics_text="otra letra"))
        assert a != b

    def test_sin_letra_es_sentinel_estable(self):
        assert compute_bg_cache_key(_preview_params()) == \
            compute_bg_cache_key(_preview_params(lyrics_text=""))

    def test_match_lyrics_off_ignora_la_letra(self):
        a = compute_bg_cache_key(_preview_params(match_lyrics=False,
                                                 lyrics_text="una letra"))
        b = compute_bg_cache_key(_preview_params(match_lyrics=False,
                                                 lyrics_text="otra letra"))
        assert a == b

    def test_validator_con_letra_acepta_y_stale_descarta(self):
        key = compute_bg_cache_key(_preview_params(lyrics_text="poco a poco"))
        params = dict(_JOB_PARAMS, lyrics_text="Poco  a POCO")
        assert _validate_bg_cache_key(key, **params) == key
        edited = dict(_JOB_PARAMS, lyrics_text="letra editada despues")
        assert _validate_bg_cache_key(key, **edited) is None


class TestPreviewComplianceGuards:
    """Invariantes que sostienen el aislamiento del cache global bg_cache/
    (revisión adversarial 2026-07-17): el preview JAMÁS genera con
    personas, así que el key no necesita separar por people-policy."""

    def test_preview_request_no_acepta_bypass(self):
        from main import _GeneratePreviewReq
        fields = set(_GeneratePreviewReq.model_fields)
        assert "bypass_content_validation" not in fields
        assert "force_content_validation" not in fields

    def test_gradiente_de_fallback_no_se_cachea(self, monkeypatch, tmp_path):
        """v6: si los 2 intentos fallan safety, el gradiente NO se sube bajo
        la key real — cachearlo hacía que un render futuro lo heredara como
        si fuera un fondo Veo bueno, salteando su propia generación."""
        import os as _os
        import bg_preview as bp
        import pipeline as pl
        import jobs as jobs_mod

        calls = {"cache_put": 0}
        monkeypatch.setattr(bp, "cache_check", lambda k: False)
        monkeypatch.setattr(
            bp, "cache_put",
            lambda *a, **k: calls.__setitem__("cache_put", calls["cache_put"] + 1) or "k",
        )
        monkeypatch.setattr(jobs_mod, "update_job", lambda *a, **k: None)

        def _fake_ensure(style, job_dir, **kw):
            p = _os.path.join(job_dir, "cand.mp4")
            with open(p, "w") as f:
                f.write("x")
            return p

        def _fake_gradient(job_dir, style, filename="fallback.mp4"):
            p = _os.path.join(job_dir, filename)
            with open(p, "w") as f:
                f.write("g")
            return p

        monkeypatch.setattr(pl, "_ensure_background", _fake_ensure)
        monkeypatch.setattr(pl, "_compute_allow_people", lambda *a, **k: False)
        monkeypatch.setattr(pl, "_raise_if_job_timeout", lambda e: None)
        # SIEMPRE unsafe → agota los 2 intentos → gradiente
        monkeypatch.setattr(pl, "_validate_background_asset_for_job",
                            lambda *a, **k: False)
        monkeypatch.setattr(pl, "_write_safe_gradient_background", _fake_gradient)

        from background_policy import runtime_rollout_fingerprint
        params = {"artist": "A", "song_title": "S"}
        res = bp.run_bg_preview_job(
            "job123fake99", bp.compute_bg_cache_key(params), params,
            runtime_rollout_fingerprint(),
        )
        assert res["status"] == "bg_preview_done"
        assert calls["cache_put"] == 0

    def test_fondo_valido_si_se_cachea(self, monkeypatch, tmp_path):
        """Control del test anterior: con validación OK, cache_put corre."""
        import os as _os
        import bg_preview as bp
        import pipeline as pl
        import jobs as jobs_mod

        calls = {"cache_put": 0}
        monkeypatch.setattr(bp, "cache_check", lambda k: False)
        monkeypatch.setattr(
            bp, "cache_put",
            lambda *a, **k: calls.__setitem__("cache_put", calls["cache_put"] + 1) or "k",
        )
        monkeypatch.setattr(jobs_mod, "update_job", lambda *a, **k: None)

        def _fake_ensure(style, job_dir, **kw):
            p = _os.path.join(job_dir, "cand.mp4")
            with open(p, "w") as f:
                f.write("x")
            return p

        monkeypatch.setattr(pl, "_ensure_background", _fake_ensure)
        monkeypatch.setattr(pl, "_compute_allow_people", lambda *a, **k: False)
        monkeypatch.setattr(pl, "_raise_if_job_timeout", lambda e: None)
        monkeypatch.setattr(pl, "_validate_background_asset_for_job",
                            lambda *a, **k: True)

        from background_policy import runtime_rollout_fingerprint
        params = {"artist": "A", "song_title": "S"}
        res = bp.run_bg_preview_job(
            "job123fake98", bp.compute_bg_cache_key(params), params,
            runtime_rollout_fingerprint(),
        )
        assert res["status"] == "bg_preview_done"
        assert calls["cache_put"] == 1
