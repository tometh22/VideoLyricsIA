"""Fase 3 (2026-09-13): reusar el texto APROBADO por un humano cuando llega
el mismo audio (input_audio_sha256) al mismo tenant. Contrato:
  - flag APPROVED_TEXT_REUSE_ENABLED (default off).
  - lookup: sólo versiones is_approved de OTRO job del MISMO tenant con el
    mismo hash; <3 líneas → None.
  - aplicación: por el mismo aligner que la letra pegada; si declina o rompe,
    el resultado ASR queda intacto y la provenance dice used=False.
"""
import asyncio
import uuid

import transcription_worker as tw
from tests.test_reanchor import _make_user, _seed_job

SHA = "c" * 64
APPROVED = [
    {"start": 0.0, "end": 2.0, "text": "linea aprobada uno"},
    {"start": 2.0, "end": 4.0, "text": "linea aprobada dos"},
    {"start": 4.0, "end": 6.0, "text": "linea aprobada tres"},
]


def _set_sha(job_id, sha):
    from database import Job, SessionLocal
    db = SessionLocal()
    try:
        row = db.query(Job).filter(Job.job_id == job_id).first()
        row.input_audio_sha256 = sha
        db.commit()
    finally:
        db.close()


def _approve_version(job_id, tenant_id, segments, *, approved=True, revision=3):
    from database import EditorVersion, SessionLocal
    db = SessionLocal()
    try:
        db.add(EditorVersion(id=str(uuid.uuid4()), job_id=job_id, tenant_id=tenant_id,
                             revision=revision, segments=segments, reason="approve",
                             is_approved=approved))
        db.commit()
    finally:
        db.close()


def test_lookup_returns_approved_text_of_another_job_same_audio(client):
    _, user_id, tenant_id = _make_user(client)
    donor = _seed_job(user_id, tenant_id, segments=list(APPROVED), with_audio=False)
    newcomer = _seed_job(user_id, tenant_id, segments=None, with_audio=False)
    _set_sha(donor, SHA); _set_sha(newcomer, SHA)
    _approve_version(donor, tenant_id, APPROVED)
    reuse = tw._approved_text_for_audio(tenant_id, SHA, exclude_job_id=newcomer)
    assert reuse["from_job_id"] == donor
    assert reuse["line_count"] == 3
    assert reuse["text"] == "linea aprobada uno\nlinea aprobada dos\nlinea aprobada tres"
    # El propio job nunca se reusa a sí mismo.
    assert tw._approved_text_for_audio(tenant_id, SHA, exclude_job_id=donor) is None


def test_lookup_ignores_unapproved_other_tenant_and_short_versions(client):
    _, user_id, tenant_id = _make_user(client)
    _, other_user, other_tenant = _make_user(client)
    donor = _seed_job(user_id, tenant_id, segments=list(APPROVED), with_audio=False)
    newcomer = _seed_job(user_id, tenant_id, segments=None, with_audio=False)
    _set_sha(donor, SHA); _set_sha(newcomer, SHA)
    _approve_version(donor, tenant_id, APPROVED, approved=False)
    assert tw._approved_text_for_audio(tenant_id, SHA, exclude_job_id=newcomer) is None
    _approve_version(donor, tenant_id, APPROVED[:2], approved=True, revision=4)
    assert tw._approved_text_for_audio(tenant_id, SHA, exclude_job_id=newcomer) is None
    # Aprobada en otro tenant con el mismo audio: no cruza tenants.
    stranger = _seed_job(other_user, other_tenant, segments=list(APPROVED), with_audio=False)
    _set_sha(stranger, SHA)
    _approve_version(stranger, other_tenant, APPROVED)
    assert tw._approved_text_for_audio(tenant_id, SHA, exclude_job_id=newcomer) is None
    assert tw._approved_text_for_audio(other_tenant, SHA, exclude_job_id=newcomer)["from_job_id"] == stranger


def test_flag_default_off(monkeypatch):
    monkeypatch.delenv("APPROVED_TEXT_REUSE_ENABLED", raising=False)
    assert tw._approved_reuse_enabled() is False
    monkeypatch.setenv("APPROVED_TEXT_REUSE_ENABLED", "1")
    assert tw._approved_reuse_enabled() is True


def _asr():
    return {"segments": [{"start": 0, "end": 1, "text": "asr libre uno"},
                         {"start": 1, "end": 2, "text": "asr libre dos"},
                         {"start": 2, "end": 3, "text": "asr libre tres"}]}


REUSE = {"text": "linea aprobada uno\nlinea aprobada dos\nlinea aprobada tres",
         "from_job_id": "donor1", "from_revision": 3, "line_count": 3, "approved_at": None}


def test_apply_replaces_text_when_aligner_applies():
    async def aligner(result, audio_path, job_id, text):
        out = dict(result)
        out["segments"] = [{"start": i, "end": i + 1, "text": ln} for i, ln in enumerate(text.splitlines())]
        out["anchor_alignment"] = {"status": "applied"}
        out["timing_source"] = "anchor_ctc"
        return out
    out = asyncio.run(tw._maybe_apply_approved_reuse(_asr(), "/tmp/x.wav", "job1", REUSE, aligner=aligner))
    assert [s["text"] for s in out["segments"]] == REUSE["text"].splitlines()
    assert out["approved_text_reuse"] == {"from_job_id": "donor1", "from_revision": 3,
                                          "line_count": 3, "approved_at": None, "used": True}
    assert "text" not in out["approved_text_reuse"]


def test_apply_keeps_asr_when_aligner_declines_or_raises():
    async def declined(result, *a, **k):
        out = dict(result); out["anchor_alignment"] = {"status": "declined", "reason": "low_score"}; return out
    out = asyncio.run(tw._maybe_apply_approved_reuse(_asr(), "/tmp/x.wav", "job1", REUSE, aligner=declined))
    assert [s["text"] for s in out["segments"]] == [s["text"] for s in _asr()["segments"]]
    assert out["approved_text_reuse"]["used"] is False
    assert out["approved_text_reuse"]["reason"] == "declined"

    async def boom(*a, **k):
        raise RuntimeError("ctc exploded")
    out = asyncio.run(tw._maybe_apply_approved_reuse(_asr(), "/tmp/x.wav", "job1", REUSE, aligner=boom))
    assert [s["text"] for s in out["segments"]] == [s["text"] for s in _asr()["segments"]]
    assert out["approved_text_reuse"]["used"] is False
    # Sin candidato no toca nada.
    assert asyncio.run(tw._maybe_apply_approved_reuse(_asr(), "/tmp/x.wav", "job1", None, aligner=boom)) == _asr()
