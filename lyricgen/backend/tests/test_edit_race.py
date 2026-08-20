"""Race condition test para POST /edit/{job_id}.

ANTES del fix:
    main.py /edit endpoint leía edit_count, validaba < _MAX_EDITS=3,
    incrementaba — sin lock de row. Dos POST simultáneos al mismo job
    en pending_review ambos pasaban el check antes de persistir.
    Resultado: user hacía 5-6 edits cuando el límite UI decía 3. Cada
    edit con text_motion ∈ {subtle,float} dispara Veo (~$0.90).

DESPUÉS del fix:
    `with_for_update()` toma row lock en Postgres. El segundo request
    espera a que el primero commit/rollback antes de leer. El check
    `< _MAX_EDITS` ahora ve el incremento del primero y rechaza con
    400.

WARNING: este test solo es válido en Postgres. En SQLite,
with_for_update() es no-op y el test daría false-green (pasaría sin
la fix). El marker `postgres` + conftest skip handler lo cuida.
"""

import threading
import time
import uuid

import pytest

from database import Job as JobModel


@pytest.mark.postgres
def test_concurrent_edits_respect_max_edits_limit(client, user_token, db):
    """5 requests POST /edit concurrentes (usuario NO-admin); solo
    _MAX_EDITS=3 deben ganar. Los otros 2 deben recibir 400 con
    'Maximum edit limit'. Verifica que el lock with_for_update()
    serializa el read-validate-write de edit_count.

    Usa user_token (no admin): el límite solo aplica a no-admins —
    los admins son exentos (ver test_admin_exempt_from_edit_limit).
    """
    from pipeline import _MAX_EDITS

    # El job debe vivir en el tenant del usuario para que /edit lo
    # encuentre (filtra por current_user["tenant_id"]).
    me = client.get(
        "/auth/me", headers={"Authorization": f"Bearer {user_token}"}
    ).json()
    tenant_id = me["tenant_id"]

    # Setup: job en pending_review listo para edits. bg_r2_key_cached
    # y segments_json no None para que pase los checks de
    # request_edit.
    job_id = uuid.uuid4().hex[:12]
    job = JobModel(
        job_id=job_id,
        user_id=me["id"],
        tenant_id=tenant_id,
        artist="Test",
        song_title="Race Test",
        filename="race-test.wav",
        status="pending_review",
        delivery_profile="youtube",
        progress=100,
        bg_r2_key_cached="fake/key.mp4",
        segments_json=[{"start": 0.0, "end": 1.0, "text": "test"}],
        edit_count=0,
    )
    db.add(job)
    db.commit()

    # Start gate maximiza el race window: todos los threads esperan
    # antes del POST, después se sueltan simultáneamente.
    start_gate = threading.Event()
    statuses: list[int] = []
    statuses_lock = threading.Lock()
    n_threads = 5

    def submit_edit():
        start_gate.wait(timeout=5.0)
        res = client.post(
            f"/edit/{job_id}",
            headers={"Authorization": f"Bearer {user_token}"},
            json={
                "edit_type": "typography",
                "font": "Arial",
            },
        )
        with statuses_lock:
            statuses.append(res.status_code)

    threads = [threading.Thread(target=submit_edit) for _ in range(n_threads)]
    for t in threads:
        t.start()
    time.sleep(0.1)  # asegurar que todos llegaron a start_gate.wait()
    start_gate.set()
    for t in threads:
        t.join(timeout=10.0)

    ok_count = sum(1 for s in statuses if s == 200)
    bad_count = sum(1 for s in statuses if s == 400)

    assert ok_count == _MAX_EDITS, (
        f"esperado {_MAX_EDITS} edits exitosos, hubo {ok_count}. "
        f"Status codes: {sorted(statuses)}. Race condition no protegida — "
        f"verificá with_for_update() en main.py:/edit endpoint."
    )
    assert bad_count == n_threads - _MAX_EDITS, (
        f"esperado {n_threads - _MAX_EDITS} requests rechazados con 400, "
        f"hubo {bad_count}. Status codes: {sorted(statuses)}."
    )

    db.expire_all()
    fresh = db.query(JobModel).filter(JobModel.job_id == job_id).first()
    assert fresh.edit_count == _MAX_EDITS, (
        f"edit_count en DB es {fresh.edit_count}, esperado {_MAX_EDITS}. "
        f"El race se coló — múltiples requests pasaron el check < _MAX_EDITS."
    )


def test_admin_exempt_from_edit_limit(client, admin_token, db, monkeypatch):
    """Un admin puede editar más allá de _MAX_EDITS. Arrancamos el job
    YA en el límite (edit_count = _MAX_EDITS) y verificamos que el
    siguiente /edit pasa (200) en vez de 400 'Maximum edit limit'.
    """
    from pipeline import _MAX_EDITS

    # enqueue_edit sin Redis real en CI: no-op capturador.
    import main
    monkeypatch.setattr(main, "enqueue_edit", lambda **kwargs: "test:noop")

    me = client.get(
        "/auth/me", headers={"Authorization": f"Bearer {admin_token}"}
    ).json()
    assert me["role"] == "admin"

    job_id = uuid.uuid4().hex[:12]
    job = JobModel(
        job_id=job_id,
        user_id=me["id"],
        tenant_id=me["tenant_id"],
        artist="Test",
        song_title="Admin Exempt",
        filename="admin-exempt.wav",
        status="pending_review",
        delivery_profile="youtube",
        progress=100,
        bg_r2_key_cached="fake/key.mp4",
        segments_json=[{"start": 0.0, "end": 1.0, "text": "test"}],
        edit_count=_MAX_EDITS,  # ya en el límite
    )
    db.add(job)
    db.commit()

    res = client.post(
        f"/edit/{job_id}",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={"edit_type": "typography", "font": "Arial"},
    )
    assert res.status_code == 200, (
        f"admin debería poder editar más allá de _MAX_EDITS, "
        f"got {res.status_code}: {res.text}"
    )
    body = res.json()
    assert body.get("edit_limit_exempt") is True
    assert body["edit_count"] == _MAX_EDITS + 1


def test_status_reports_edit_limit_exempt_for_admin(client, admin_token, db):
    """GET /status devuelve edit_limit_exempt=True para admin (la UI lo
    usa para mostrar 'sin límite' y no bloquear el panel de edición)."""
    job_id = uuid.uuid4().hex[:12]
    me = client.get(
        "/auth/me", headers={"Authorization": f"Bearer {admin_token}"}
    ).json()
    job = JobModel(
        job_id=job_id,
        user_id=me["id"],
        tenant_id=me["tenant_id"],
        artist="Test",
        song_title="Exempt Status",
        filename="admin-status.wav",
        status="pending_review",
        delivery_profile="youtube",
        progress=100,
        edit_count=3,
    )
    db.add(job)
    db.commit()

    res = client.get(
        f"/status/{job_id}", headers={"Authorization": f"Bearer {admin_token}"}
    )
    assert res.status_code == 200, res.text
    assert res.json().get("edit_limit_exempt") is True
