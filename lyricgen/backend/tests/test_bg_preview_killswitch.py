"""Kill-switch del pre-generado de fondos.

Medido en jul-2026 sobre los dos entornos: **147 fondos pre-generados, 4
reusados** — $91/mes fabricando fondos que se descartan.

La causa no es la que se creyó primero (que el operador cambia las opciones y
rota la clave del cache). Son dos flujos que no se cruzan: el 79% de los
renders de staging entra por API — el bot de regresión y el preflight — y esos
nunca disparan preview; y los que sí usan el wizard renderizan 51-56 min
después, cuando la ventana útil es de 30-90 s.

Apagarlo no hace esperar más al operador: el render genera el fondo igual
porque el pre-generado ya se descartó.
"""

import pytest

from tests.conftest import auth


def _body():
    return {"artist": "Bersuit", "song_title": "La Argentinidad Al Palo",
            "style": "auto", "movement_style": "estatico"}


def test_apagado_devuelve_skipped_sin_encolar(client, admin_token, monkeypatch):
    """Contrato clave: se reusa el `skipped` que el frontend YA maneja, así
    que apagarlo no rompe la UI ni deja el wizard esperando."""
    monkeypatch.setenv("BG_PREVIEW_ENABLED", "0")
    res = client.post("/generate-preview", headers=auth(admin_token),
                      json=_body())
    assert res.status_code == 200
    body = res.json()
    assert body["skipped"] is True
    assert body["reason"] == "disabled"
    # No debe devolver job_id: si encolara, seguiría gastando.
    assert "job_id" not in body


def test_el_mensaje_le_dice_al_operador_que_igual_va_a_salir(client, admin_token,
                                                            monkeypatch):
    """Si el mensaje sugiriera que algo falló, el operador reintentaría — y
    cada reintento cuesta."""
    monkeypatch.setenv("BG_PREVIEW_ENABLED", "0")
    msg = client.post("/generate-preview", headers=auth(admin_token),
                      json=_body()).json()["message"]
    assert "genera igual" in msg


def test_prendido_no_cambia_el_comportamiento(client, admin_token, monkeypatch):
    """Default = prendido. El flag es opt-in a apagar, así que no altera
    ningún entorno hasta que alguien lo setea."""
    monkeypatch.setenv("BG_PREVIEW_ENABLED", "1")
    body = client.post("/generate-preview", headers=auth(admin_token),
                       json=_body()).json()
    assert body.get("reason") != "disabled"


def test_sin_la_variable_queda_prendido(client, admin_token, monkeypatch):
    monkeypatch.delenv("BG_PREVIEW_ENABLED", raising=False)
    body = client.post("/generate-preview", headers=auth(admin_token),
                       json=_body()).json()
    assert body.get("reason") != "disabled"


def test_acepta_las_variantes_de_apagado(client, admin_token, monkeypatch):
    for valor in ("0", "false", "off", "no", "FALSE", " Off "):
        monkeypatch.setenv("BG_PREVIEW_ENABLED", valor)
        body = client.post("/generate-preview", headers=auth(admin_token),
                           json=_body()).json()
        assert body.get("reason") == "disabled", valor


def test_worker_omite_backlog_ya_encolado_cuando_se_apaga(monkeypatch):
    """El switch también debe cortar jobs que ya estaban esperando en Redis."""
    import bg_preview
    import jobs

    monkeypatch.setenv("BG_PREVIEW_ENABLED", "0")
    updates = []
    monkeypatch.setattr(jobs, "update_job", lambda *args, **kw: updates.append(kw))
    monkeypatch.setattr(
        bg_preview,
        "cache_check",
        lambda _key: (_ for _ in ()).throw(
            AssertionError("un preview desactivado no debe tocar cache ni generar")
        ),
    )

    result = bg_preview.run_bg_preview_job(
        "previewqueued01",
        "unused-key",
        {"artist": "Bersuit", "song_title": "La Argentinidad Al Palo"},
    )

    assert result == {
        "job_id": "previewqueued01",
        "status": "bg_preview_done",
        "bg_cache_key": "unused-key",
        "cached": False,
        "skipped": True,
        "reason": "disabled",
    }
    assert updates == [{
        "status": "bg_preview_done",
        "current_step": "disabled",
        "error": None,
    }]


def test_worker_reintenta_si_no_puede_persistir_disabled(monkeypatch):
    """RQ must see the exception instead of consuming a still-queued job."""
    import bg_preview
    import jobs

    monkeypatch.setenv("BG_PREVIEW_ENABLED", "0")
    monkeypatch.setattr(
        jobs,
        "update_job",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("database temporarily unavailable")
        ),
    )
    monkeypatch.setattr(
        bg_preview,
        "cache_check",
        lambda _key: (_ for _ in ()).throw(
            AssertionError("disabled preview must not reach cache/provider")
        ),
    )

    with pytest.raises(RuntimeError, match="database temporarily unavailable"):
        bg_preview.run_bg_preview_job(
            "previewqueued01", "unused-key",
            {"artist": "Bersuit", "song_title": "La Argentinidad Al Palo"},
        )
