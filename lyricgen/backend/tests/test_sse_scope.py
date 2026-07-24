"""Test del SSE re-validation en /events/{job_id}.

ANTES del fix: el stream verificaba auth UNA vez al abrir conexión.
Si admin transfería al user entre tenants mid-stream, el SSE seguía
emitiendo eventos del job viejo. Improbable de explotar (cliente
actual no rotea users entre tenants) pero "world-class" cierra la
puerta.

DESPUÉS del fix: cada poll re-verifica que `User.tenant_id` siga
matcheando el original. Si cambió, emite `event: unauthorized` y
cierra el stream.
"""

import uuid

from database import Job as JobModel, User


def test_sse_emits_unauthorized_when_tenant_changes(client, user_token, db):
    """Si el tenant_id del user cambia durante el stream, /events debe
    cerrar (con event unauthorized en body, o 401/404 al inicio del
    stream).

    Simplificación: mutamos User.tenant_id en DB ANTES de abrir el
    stream. El initial scope check del endpoint o el primer poll del
    generator deberían cortar. Esto cubre la mayor parte del fix sin
    requerir timing complejo de streaming concurrente.
    """
    me = client.get("/auth/me", headers={"Authorization": f"Bearer {user_token}"}).json()
    user_id = me["id"]
    original_tenant = me["tenant_id"]

    job_id = uuid.uuid4().hex[:12]
    job = JobModel(
        job_id=job_id,
        user_id=1,
        tenant_id=original_tenant,
        artist="Test",
        song_title="SSE Scope",
        filename="test.mp3",
        status="processing",  # no-terminal — stream entra al poll loop
        delivery_profile="youtube",
        progress=20,
        current_step="whisper",
    )
    db.add(job)
    db.commit()

    # Mutar tenant_id (simula admin que mueve user a otro tenant)
    fresh_user = db.query(User).filter(User.id == user_id).first()
    fresh_user.tenant_id = "other_tenant_evil"
    db.commit()

    res = client.get(f"/events/{job_id}?token={user_token}")
    # /events puede:
    #   - 404: initial scope check cortó (job no es del nuevo tenant)
    #   - 401: token quedó inválido
    #   - 200 con event:unauthorized: el re-check del generator disparó
    # Lo importante: NO 200 con datos válidos del job de otro tenant.
    if res.status_code == 200:
        assert "unauthorized" in res.text.lower(), (
            f"SSE devolvió 200 sin evento unauthorized: {res.text[:500]}"
        )
    else:
        assert res.status_code in (401, 404), (
            f"esperado 200/401/404, got {res.status_code}: {res.text[:200]}"
        )
