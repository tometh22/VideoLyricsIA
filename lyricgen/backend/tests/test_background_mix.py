"""Mix de fondos del contrato UMG (background_mix.py).

Lo que fija:

* El tipo de fondo sale de lo que el pipeline hizo (ai_provenance), no del
  movement_style crudo: "foto-estatica" termina en Veo.
* "foto-parallax" es foto aunque el job haya tenido un Veo antes.
* Se cuenta la primera entrega vigente de cada job, sin art tracks (tampoco
  los publicados con otra etiqueta), agrupado por campaña.
* Sólo super-admin.
"""
import uuid
from datetime import datetime, timedelta, timezone

from tests.conftest import auth

T0 = datetime(2026, 10, 1, 12, tzinfo=timezone.utc)


def _prov(step, tool, minutes=0):
    return (step, tool, T0 + timedelta(minutes=minutes))


def test_classify_uses_provenance_and_photo_movement():
    from background_mix import classify

    lite = _prov("video_bg", "veo-3.1-lite-generate-001")
    image = _prov("image_bg", "gemini-2.5-flash-image")
    assert classify({"movement_style": "estatico"}, [lite]) == {"type": "veo", "model": "lite"}
    assert classify({}, [_prov("video_bg", "veo-3.1-fast-generate-001")])["model"] == "fast"
    # foto-parallax manda aunque haya un Veo de un render anterior.
    assert classify({"movement_style": "foto-parallax"}, [lite])["type"] == "foto"
    assert classify({"movement_style": "foto-parallax"}, [])["type"] == "foto"
    # "foto-estatica" se normaliza a "estatico": lo que dice la provenance.
    assert classify({"movement_style": "foto-estatica"}, [lite])["type"] == "veo"
    # Sin movement de foto, gana la última generación real.
    assert classify({}, [lite, _prov("image_bg", "gemini-2.5-flash-image", 5)])["type"] == "foto"
    assert classify({}, [image, _prov("video_bg", "veo-3.1-lite-generate-001", 5)])["type"] == "veo"
    # El reuso de caché no pisa la generación real; solo, cuenta como Veo reusado.
    reuse = _prov("video_bg", "bg-cache-reuse", 10)
    assert classify({}, [image, reuse])["type"] == "foto"
    assert classify({}, [reuse]) == {"type": "veo", "model": "reuso"}
    assert classify({}, [])["type"] == "sin_dato"


def test_photo_movements_match_pipeline_normalizer():
    from background_mix import PHOTO_MOVEMENTS
    from pipeline import _normalize_movement_style

    candidates = {
        "foto-parallax", "photo", "parallax", "foto+parallax", "foto_parallax",
        "foto-estatica", "photo-static", "foto_static", "estatico", "sutil",
        "estandar", "animado", "static", "",
    }
    expected = {m for m in candidates if _normalize_movement_style(m) == "foto-parallax"}
    assert expected == set(PHOTO_MOVEMENTS)


def test_background_mix_requires_super_admin(client, user_token, admin_token, monkeypatch):
    assert client.get("/admin/finance/background-mix", headers=auth(user_token)).status_code == 403
    monkeypatch.setenv("SUPER_ADMIN_USERS", "alguien-que-no-es@example.com")
    assert client.get("/admin/finance/background-mix", headers=auth(admin_token)).status_code == 403


def test_background_mix_groups_live_deliveries_by_campaign(client, admin_token, db, admin_user_id):
    from database import AIProvenance, BatchCampaign, Delivery, Job

    tag = uuid.uuid4().hex[:6]
    camp = f"mx{tag}"
    db.add(BatchCampaign(id=camp, tenant_id="universal_music", created_by=admin_user_id,
                         name=f"Campaña {tag}", destination_portal="argentina"))
    db.flush()

    def job(suffix, render_params, prov=(), campaign=camp):
        job_id = f"m{tag}{suffix}"
        db.add(Job(job_id=job_id, user_id=admin_user_id, tenant_id="universal_music",
                   artist="Artista", song_title=f"Canción {suffix}", filename="a.mp3",
                   status="done", campaign_id=campaign, render_params=render_params))
        db.flush()
        for step, tool in prov:
            db.add(AIProvenance(job_id=job_id, step=step, tool_name=tool,
                                tool_provider="google_vertex", prompt_sent="p", created_at=T0))
        return job_id

    def deliver(job_id, label="Campaña", added=T0, removed=False, portal="argentina"):
        db.add(Delivery(job_id=job_id, label=label, file_types=["video"], artist_snapshot="Artista",
                        song_title_snapshot=job_id, tenant_snapshot="universal_music",
                        added_by_user_id=admin_user_id, added_at=added, portal_id=portal,
                        removed_at=T0 if removed else None))

    try:
        veo = job("01", {"movement_style": "estatico"}, [("video_bg", "veo-3.1-lite-generate-001")])
        foto = job("02", {"movement_style": "foto-parallax", "effect": "bokeh"},
                   [("image_bg", "gemini-2.5-flash-image")])
        retirado = job("03", {}, [("video_bg", "veo-3.1-lite-generate-001")])
        art = job("04", {"art_track": True})
        suelto = job("05", {}, [("video_bg", "veo-3.1-fast-generate-001")], campaign=None)
        # Publicado dos veces: un solo video.
        deliver(veo)
        deliver(veo, added=T0 + timedelta(days=3))
        deliver(foto)
        deliver(retirado, removed=True)
        deliver(art, label="Opción 3")
        deliver(suelto, label="Renderizado", portal="chile")
        deliver(f"m{tag}99", label="Art Track")
        db.commit()

        res = client.get("/admin/finance/background-mix", headers=auth(admin_token))
        assert res.status_code == 200, res.text
        body = res.json()
        group = next(g for g in body["groups"] if g["key"] == f"campaign:{camp}")
        assert (group["videos"], group["veo"], group["foto"]) == (2, 1, 1)
        assert group["veo_models"] == {"lite": 1}
        assert group["efectos"] == {"bokeh": 1}
        assert group["name"] == f"Campaña {tag}" and group["portal"] == "argentina"

        mine = {i["job_id"]: i for i in body["items"] if i["job_id"].startswith(f"m{tag}")}
        assert set(mine) == {veo, foto, suelto}
        assert mine[suelto]["group"] == "sin_campana:chile"
        assert mine[suelto]["veo_model"] == "fast"
        assert body["excluded_art_tracks"] >= 1
    finally:
        # Lo que se commiteó acá es visible para el resto de la suite (en CI
        # es Postgres compartido): otros tests borran todas las campañas y
        # cuentan entregas, así que no puede quedar nada.
        db.rollback()
        ids = [f"m{tag}{n}" for n in ("01", "02", "03", "04", "05", "99")]
        db.query(Delivery).filter(Delivery.job_id.in_(ids)).delete(synchronize_session=False)
        db.query(AIProvenance).filter(AIProvenance.job_id.in_(ids)).delete(synchronize_session=False)
        db.query(Job).filter(Job.job_id.in_(ids)).delete(synchronize_session=False)
        db.query(BatchCampaign).filter(BatchCampaign.id == camp).delete(synchronize_session=False)
        db.commit()
