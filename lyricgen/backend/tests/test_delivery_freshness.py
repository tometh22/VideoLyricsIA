"""Unit tests for the publication-freshness primitives.

The bug these guard against is invisible by construction: the portal
rebuilds the R2 key from (tenant, job_id, file_type), the render writes
to that same key, so a correction reaches the client with no change to
any row anyone looks at. Every assertion here is about making that
replacement *visible* — to the publish gate, to the operator, and to the
client.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest

import delivery_freshness as df


# Forma que deja un re-render de edición: `_snapshot_previous_deliverables`
# archiva los entregables que está por pisar, justo antes de subir los nuevos.
EDITED = [{"version": 1, "archived_at": "2026-09-15T10:00:00Z", "keys": {"video": "t/j/lyric_video.mp4.v1"}}]


class FakeJob:
    """Minimal stand-in with the columns the fingerprint reads.

    A real Job row would do, but these are pure functions over ~8
    attributes and a fake keeps each case a single readable line.
    """

    def __init__(self, **kwargs):
        self.job_id = kwargs.get("job_id", "job123456789")
        self.edit_count = kwargs.get("edit_count", 0)
        self.previous_versions = kwargs.get("previous_versions")
        self.segments_revision = kwargs.get("segments_revision", 0)
        self.render_params = kwargs.get("render_params")
        self.umg_spec = kwargs.get("umg_spec")
        self.input_audio_sha256 = kwargs.get("input_audio_sha256", "a" * 64)
        self.input_r2_key = kwargs.get("input_r2_key")
        self.s3_keys = kwargs.get("s3_keys")
        self.delivery_profile = kwargs.get("delivery_profile", "umg")
        self.status = kwargs.get("status", "done")


# ---------------------------------------------------------------------------
# render_fingerprint — tells a new cut from a double-click
# ---------------------------------------------------------------------------

def test_fingerprint_is_stable_for_the_same_render():
    """Two publishes of an untouched job must look identical, or every
    re-send would revoke UMG's approval and demand a fresh review."""
    job = FakeJob(render_params={"style": "oscuro"})
    assert df.render_fingerprint(job) == df.render_fingerprint(job)


def test_fingerprint_ignores_s3_keys():
    """s3_keys cannot be an input: the keys are deterministic, so they are
    byte-identical across renders — that is the whole problem."""
    before = df.render_fingerprint(FakeJob(s3_keys={}))
    after = df.render_fingerprint(FakeJob(s3_keys={"video": "t/j/lyric_video.mp4"}))
    assert before == after


@pytest.mark.parametrize("mutation", [
    {"edit_count": 1},
    {"segments_revision": 7},
    {"render_params": {"style": "claro"}},
    {"umg_spec": {"frame_size": "UHD-4K"}},
    {"input_audio_sha256": "b" * 64},
    {"previous_versions": [{"version": 1, "archived_at": "2026-09-15T10:00:00Z"}]},
])
def test_fingerprint_moves_when_the_render_changes(mutation):
    """Anything that changes the rendered bytes must change the identity,
    otherwise a corrected cut is published as if it were a re-send."""
    base_kwargs = {"render_params": {"style": "oscuro"}}
    base = FakeJob(**base_kwargs)
    assert df.render_fingerprint(FakeJob(**{**base_kwargs, **mutation})) != (
        df.render_fingerprint(base)
    )


def test_fingerprint_moves_on_a_second_overwrite_of_the_same_kind():
    """Two edits that produce the same params still differ: the archive of
    replaced deliverables grows, which is the most direct evidence that
    the files behind the client's download were replaced again."""
    one = FakeJob(previous_versions=[{"version": 1, "archived_at": "2026-09-15T10:00:00Z"}])
    two = FakeJob(previous_versions=[
        {"version": 1, "archived_at": "2026-09-15T10:00:00Z"},
        {"version": 2, "archived_at": "2026-09-15T12:00:00Z"},
    ])
    assert df.render_fingerprint(one) != df.render_fingerprint(two)


# ---------------------------------------------------------------------------
# prores_pending — the post-edit window R2 cannot see
# ---------------------------------------------------------------------------

def test_prores_pending_after_an_edit_dropped_the_master_key():
    """The 2026-08-03 shape: the re-render wrote a fresh MP4 and
    invalidated the master, whose PRE-EDIT object is still in R2."""
    job = FakeJob(previous_versions=EDITED, s3_keys={
        "video": "t/j/lyric_video.mp4",
        "short": "t/j/short.mp4",
        "umg_short": "t/j/umg_short.mov",
    })
    assert df.prores_pending(job) == ["umg_master"]


def test_prores_not_pending_once_the_prewarm_republished_it():
    job = FakeJob(previous_versions=EDITED, s3_keys={
        "video": "t/j/lyric_video.mp4", "short": "t/j/short.mp4",
        "umg_master": "t/j/umg_master.mov", "umg_short": "t/j/umg_short.mov",
    })
    assert df.prores_pending(job) == []


def test_legacy_job_with_no_tracked_keys_is_not_held_back():
    """Jobs predating s3_keys tracking have the column empty and their
    files perfectly uploaded. Reading "key absent" as "stale" would park
    them in a permanent 202, re-queueing a transcode they do not need —
    the same trap _deliverables_never_produced documents."""
    assert df.prores_pending(FakeJob(s3_keys=None)) == []
    assert df.prores_pending(FakeJob(s3_keys={})) == []
    assert df.prores_pending(FakeJob(
        previous_versions=EDITED, s3_keys={},
    )) == []


def test_never_edited_job_whose_master_key_was_clobbered_is_not_held_back():
    """Medido en producción el 2026-09-15: de 215 publicaciones activas,
    ~40 tenían `video` sin `umg_master` y NUNCA fueron editadas.

    El master está en R2 y es el correcto para ese render: la key se perdió
    con la escritura mayorista de s3_keys previa al 2026-05-26, que le corría
    carrera al prewarm. Marcarlas como desfasadas habría bloqueado la
    publicación de todas y encolado ~40 transcodes de varios GB al aire.
    """
    clobbered = FakeJob(edit_count=0, previous_versions=None, s3_keys={
        "video": "t/j/lyric_video.mp4", "short": "t/j/short.mp4",
    })
    assert df.prores_pending(clobbered) == []


def test_prores_pending_ignores_a_short_the_job_never_produced():
    """A partial delivery (master without vertical short) must not wait
    forever for a derivative of a file that does not exist."""
    job = FakeJob(previous_versions=EDITED, s3_keys={"video": "t/j/lyric_video.mp4"})
    assert df.prores_pending(job) == ["umg_master"]


def test_prores_pending_narrows_to_what_the_delivery_publishes():
    job = FakeJob(previous_versions=EDITED, s3_keys={
        "video": "t/j/lyric_video.mp4", "short": "t/j/short.mp4",
    })
    assert df.prores_pending(job, ["video", "umg_master"]) == ["umg_master"]


def test_prores_deliverable_is_not_gated_on_delivery_profile():
    """Campaign jobs carry a UMG spec while their profile stays
    "youtube". Gating on the profile is exactly what left those serving a
    stale master forever (2026-08-03)."""
    job = FakeJob(delivery_profile="youtube", umg_spec={"frame_size": "HD"})
    assert df.has_prores_deliverable(job) is True
    assert df.prores_pending(FakeJob(
        delivery_profile="youtube", umg_spec={"frame_size": "HD"},
        previous_versions=EDITED, s3_keys={"video": "t/j/lyric_video.mp4"},
    )) == ["umg_master"]


def test_no_prores_deliverable_means_nothing_pending():
    job = FakeJob(
        delivery_profile="youtube", umg_spec=None,
        previous_versions=EDITED, s3_keys={"video": "t/j/lyric_video.mp4"},
    )
    assert df.has_prores_deliverable(job) is False
    assert df.prores_pending(job) == []


# ---------------------------------------------------------------------------
# publication_state — what the operator could not see
# ---------------------------------------------------------------------------

class FakeDelivery:
    def __init__(self, **kwargs):
        self.published_render_fingerprint = kwargs.get("published_render_fingerprint")
        self.published_revision = kwargs.get("published_revision", 1)
        self.content_updated_at = kwargs.get("content_updated_at")
        self.approved_at = kwargs.get("approved_at")
        self.approved_by_label = kwargs.get("approved_by_label")
        self.stale_since = kwargs.get("stale_since")
        self.stale_reason = kwargs.get("stale_reason")
        self.file_types = kwargs.get("file_types", ["umg_master", "video"])


def test_publication_state_flags_a_render_published_before_the_edit():
    job = FakeJob(edit_count=1, s3_keys={
        "video": "t/j/lyric_video.mp4", "umg_master": "t/j/umg_master.mov",
    })
    delivery = FakeDelivery(published_render_fingerprint="stale-fingerprint")
    state = df.publication_state(job, delivery)
    assert state["needs_publish"] is True
    assert state["prores_pending"] == []


def test_publication_state_is_quiet_when_the_portal_has_this_cut():
    job = FakeJob(s3_keys={
        "video": "t/j/lyric_video.mp4", "umg_master": "t/j/umg_master.mov",
    })
    delivery = FakeDelivery(published_render_fingerprint=df.render_fingerprint(job))
    assert df.publication_state(job, delivery)["needs_publish"] is False


def test_row_predating_the_column_is_not_reported_as_outdated():
    """Rows published before the fingerprint existed have nothing to
    compare against. Claiming "needs publish" on all of them would bury
    the handful that really do."""
    job = FakeJob(edit_count=3)
    delivery = FakeDelivery(published_render_fingerprint=None)
    assert df.publication_state(job, delivery)["needs_publish"] is False


def test_publication_state_reports_new_content_awaiting_the_client():
    job = FakeJob(s3_keys={"video": "t/j/lyric_video.mp4", "umg_master": "t/j/m.mov"})
    delivery = FakeDelivery(
        published_render_fingerprint=df.render_fingerprint(job),
        published_revision=2,
        content_updated_at=datetime.now(timezone.utc),
        approved_at=None,
    )
    state = df.publication_state(job, delivery)
    assert state["awaiting_review"] is True
    assert state["revision"] == 2


# ---------------------------------------------------------------------------
# mark_deliveries_stale — the window the client should be told about
# ---------------------------------------------------------------------------

@pytest.fixture
def delivery_rows(db):
    """Two active publications of one job plus one removed row."""
    from database import Delivery

    job_id = uuid.uuid4().hex[:12]
    rows = [
        Delivery(
            job_id=job_id, label="Renderizado", file_types=["video"],
            artist_snapshot="A", song_title_snapshot="S",
            tenant_snapshot="default", portal_id=portal,
            added_by_user_id=1, added_at=datetime.now(timezone.utc),
            removed_at=removed,
        )
        for portal, removed in (
            ("argentina", None), ("chile", None),
            ("argentina", datetime.now(timezone.utc)),
        )
    ]
    for row in rows:
        db.add(row)
    db.commit()
    yield job_id, rows
    for row in rows:
        db.query(Delivery).filter(Delivery.id == row.id).delete()
    db.commit()


def test_mark_stale_flags_every_active_publication(db, delivery_rows):
    from database import Delivery

    job_id, _ = delivery_rows
    assert df.mark_deliveries_stale(job_id, df.STALE_EDITING) == 2

    rows = db.query(Delivery).filter(Delivery.job_id == job_id).all()
    flagged = [r for r in rows if r.stale_since is not None]
    assert len(flagged) == 2
    assert {r.portal_id for r in flagged} == {"argentina", "chile"}
    assert all(r.stale_reason == df.STALE_EDITING for r in flagged)
    # The removed row stays untouched: it is not on any portal.
    assert [r for r in rows if r.removed_at is not None][0].stale_since is None


def test_mark_stale_keeps_the_first_timestamp(db, delivery_rows):
    """The window the client cares about starts at the first edit. A
    second flag (MP4 up, master still transcoding) refines the reason
    without resetting the clock."""
    from database import Delivery

    job_id, _ = delivery_rows
    df.mark_deliveries_stale(job_id, df.STALE_EDITING)
    row = (
        db.query(Delivery)
        .filter(Delivery.job_id == job_id, Delivery.removed_at.is_(None))
        .first()
    )
    first = row.stale_since
    row.stale_since = first - timedelta(minutes=5)
    db.commit()
    original = row.stale_since

    df.mark_deliveries_stale(job_id, df.STALE_PRORES)
    db.refresh(row)
    assert row.stale_since == original
    assert row.stale_reason == df.STALE_PRORES


def test_mark_stale_never_raises_on_an_unreachable_portal_db(monkeypatch):
    """A momentarily unreachable portal database must not abort a render.
    A stale badge is a worse outcome than a 500 on the operator, but an
    aborted edit is worse than both."""
    def boom():
        raise RuntimeError("portal db down")

    monkeypatch.setattr("database.scoped_deliveries_db", boom)
    assert df.mark_deliveries_stale("nonexistent1", df.STALE_EDITING) == 0


# ---------------------------------------------------------------------------
# Wiring: every path that replaces the published files must say so first
# ---------------------------------------------------------------------------
#
# Source-level, in the style of test_edit_prores_invalidation: these three
# call sites sit inside endpoints that drive the whole render, and the
# invariant is an ORDERING one (flag before the reset wipes the evidence)
# that a behavioural test on one endpoint would not pin down for the others.

def _source(obj) -> str:
    import inspect
    return inspect.getsource(obj)


def test_retry_flags_deliveries_before_clearing_s3_keys():
    import main
    src = _source(main.retry_job)
    flag = src.index("mark_deliveries_stale")
    wipe = src.index("job.s3_keys = None")
    assert flag < wipe, (
        "el flag tiene que ir ANTES del reset: s3_keys es la única señal de "
        "que este job ya tenía entregables publicados"
    )


def test_art_track_edit_flags_deliveries_before_clearing_s3_keys():
    import main
    src = _source(main.edit_art_track)
    assert src.index("mark_deliveries_stale") < src.index("job.s3_keys = None")


def test_lyrics_edit_flags_deliveries_on_re_render_only():
    """El camino de /generate reusa el job. Lo recorre también cada canción
    de una campaña en su primera generación, donde no hay nada publicado
    que marcar — de ahí la guarda por s3_keys."""
    import main
    src = _source(main.generate_with_segments)
    assert "mark_deliveries_stale" in src
    assert "_republish_pending = bool(job_row.s3_keys)" in src
    # El aviso se manda DESPUÉS de publicar el re-render: vive en otra base
    # y no participa del rollback, así que marcarlo antes de un 409 dejaría
    # al cliente con un "aplicando cambios" que nada limpia.
    assert src.index("_republish_pending = bool(job_row.s3_keys)") < src.index(
        "if _republish_pending:"
    )
    assert src.index("_commit_pipeline_publication(") < src.index(
        "if _republish_pending:"
    )


def test_edit_pipeline_refines_the_reason_after_the_mp4_lands():
    """Terminado el re-render, el MP4 nuevo ya está arriba y el master de
    broadcast todavía no: el motivo cambia, la ventana sigue abierta."""
    import pipeline
    src = _source(pipeline.run_edit_pipeline)
    assert "mark_deliveries_stale(job_id, STALE_PRORES)" in src
    assert src.index("enqueue_prores_prewarm(job_id, \"umg_master\", force=True)") < (
        src.index("mark_deliveries_stale(job_id, STALE_PRORES)")
    )


def test_failed_edit_changes_the_reason_without_clearing_the_flag():
    """El sink de error del edit tiene que cambiar el motivo. Si no, la fila
    queda prometiendo trabajo en curso para siempre: nada más la destraba,
    porque sólo publicar limpia stale_since."""
    import pipeline
    src = _source(pipeline.run_edit_pipeline)
    assert "mark_deliveries_stale(job_id, STALE_FAILED)" in src
    assert src.index('action="job.edit_failed"') > src.index(
        "mark_deliveries_stale(job_id, STALE_FAILED)"
    )


def test_a_failed_edit_is_not_advertised_as_in_flight():
    assert df.STALE_EDITING in df.STALE_IN_FLIGHT
    assert df.STALE_PRORES in df.STALE_IN_FLIGHT
    assert df.STALE_FAILED not in df.STALE_IN_FLIGHT
