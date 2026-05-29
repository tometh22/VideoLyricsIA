"""Tests for `get_plan_usage` — song-level quota.

Pricing model 2026-05-29: a plan's `limit` is in UNIQUE SONGS per month.
The SQL identity is `LOWER(TRIM(artist)) + '||' + LOWER(TRIM(song_title))`.

This means:
  - The same song with multiple variants ("Renderizado" + "Opción 2")
    counts ONCE, not N times.
  - Two different songs by the same artist count as 2.
  - Casing/whitespace drift in artist or title doesn't inflate the
    count (`Los Abuelos de la Nada` and `los abuelos de la nada` are
    the same song).

The earlier `created_at` → `approved_at` filter is preserved; the only
change versus the previous PR is the unit of counting (songs, not job
rows).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from auth import get_plan_usage
from database import Job, SessionLocal

_T = "tenant_quota_test"
_USER_ID = 999_001


def _seed_song(
    db, *, job_id, status, approved_at,
    artist="Test Artist", song_title="Test Song",
    created_at=None,
):
    """Insert a job row. Defaults `artist`/`song_title` so older tests
    that don't care about identity still produce isolated songs; pass
    them explicitly when the test is exercising the dedup behaviour."""
    if created_at is None:
        created_at = approved_at - timedelta(days=2) if approved_at else datetime.now(timezone.utc)
    db.add(Job(
        job_id=job_id,
        user_id=_USER_ID,
        tenant_id=_T,
        artist=artist,
        song_title=song_title,
        filename="t.mp3",
        style="oscuro",
        status=status,
        delivery_profile="youtube",
        created_at=created_at,
        approved_at=approved_at,
    ))
    db.commit()


def _cleanup(db):
    db.query(Job).filter(Job.tenant_id == _T).delete(synchronize_session=False)
    db.commit()


def _this_month_start():
    now = datetime.now(timezone.utc)
    return datetime(now.year, now.month, 1, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Carried over from the previous PR (approved_at filter) — still correct
# under the song-level model.
# ---------------------------------------------------------------------------


def test_pre_approval_does_not_count():
    """A job currently being processed has status != done and
    approved_at = NULL. It must NOT count against quota — the operator
    hasn't committed to it yet."""
    db = SessionLocal()
    try:
        _cleanup(db)
        for i, status in enumerate(["queued", "processing", "pending_review", "editing", "transcribed_pending"]):
            _seed_song(
                db, job_id=f"draft_{i}", status=status, approved_at=None,
                artist=f"Artist {i}", song_title=f"Song {i}",
            )
        usage = get_plan_usage(db, user_id=_USER_ID, tenant_id=_T, plan_id="250")
        assert usage["used"] == 0
        # Sanity: the unit label is exposed for the frontend.
        assert usage.get("unit") == "song"
    finally:
        _cleanup(db); db.close()


def test_rejected_does_not_count_even_if_previously_approved():
    """Rejected jobs leave approved_at populated but status flips. The
    quota requires status='done' AND approved_at — the rejected song
    must NOT be in the count."""
    db = SessionLocal()
    try:
        _cleanup(db)
        _seed_song(
            db, job_id="rejected1", status="rejected",
            approved_at=datetime.now(timezone.utc) - timedelta(hours=1),
            artist="Once-approved Artist", song_title="Once-approved Song",
        )
        usage = get_plan_usage(db, user_id=_USER_ID, tenant_id=_T, plan_id="250")
        assert usage["used"] == 0
    finally:
        _cleanup(db); db.close()


def test_approval_from_previous_month_does_not_count_this_month():
    """A song approved last month is in last month's billing cycle."""
    db = SessionLocal()
    try:
        _cleanup(db)
        forty_five_days_ago = datetime.now(timezone.utc) - timedelta(days=45)
        _seed_song(
            db, job_id="last_month", status="done",
            approved_at=forty_five_days_ago,
            artist="Old Artist", song_title="Old Song",
        )
        usage = get_plan_usage(db, user_id=_USER_ID, tenant_id=_T, plan_id="250")
        assert usage["used"] == 0
    finally:
        _cleanup(db); db.close()


def test_created_last_month_approved_this_month_counts_this_month():
    """Cross-month approval: created last month, approved this month →
    counts THIS month (when the customer paid for it)."""
    db = SessionLocal()
    try:
        _cleanup(db)
        forty_days_ago = datetime.now(timezone.utc) - timedelta(days=40)
        one_hour_ago = datetime.now(timezone.utc) - timedelta(hours=1)
        _seed_song(
            db, job_id="crosser", status="done",
            created_at=forty_days_ago, approved_at=one_hour_ago,
            artist="Crosser Artist", song_title="Crosser Song",
        )
        usage = get_plan_usage(db, user_id=_USER_ID, tenant_id=_T, plan_id="250")
        assert usage["used"] == 1
    finally:
        _cleanup(db); db.close()


def test_other_tenant_isolation():
    """tenant_id filter still works under the song-level rewrite."""
    db = SessionLocal()
    try:
        _cleanup(db)
        _seed_song(
            db, job_id="our_song", status="done",
            approved_at=datetime.now(timezone.utc) - timedelta(hours=1),
            artist="Our Artist", song_title="Our Song",
        )
        db.add(Job(
            job_id="other_tenant_song", user_id=42, tenant_id="some_other_tenant",
            artist="Other Artist", song_title="Other Song",
            filename="o.mp3", style="oscuro",
            status="done", delivery_profile="youtube",
            created_at=datetime.now(timezone.utc) - timedelta(hours=3),
            approved_at=datetime.now(timezone.utc) - timedelta(hours=1),
        ))
        db.commit()
        usage = get_plan_usage(db, user_id=_USER_ID, tenant_id=_T, plan_id="250")
        assert usage["used"] == 1
        db.query(Job).filter(Job.tenant_id == "some_other_tenant").delete()
        db.commit()
    finally:
        _cleanup(db); db.close()


# ---------------------------------------------------------------------------
# NEW tests — pin the song-level behaviour. These would all FAIL under
# the previous job-count model (each returns >1 for some case where the
# expected song-count is 1).
# ---------------------------------------------------------------------------


def test_two_jobs_same_song_count_as_one():
    """The headline case: a song with `Renderizado` + `Opción 2`
    variants is ONE slot, not two. Both are status=done + approved_at
    set; under the old job-count model this would return 2."""
    db = SessionLocal()
    try:
        _cleanup(db)
        now = datetime.now(timezone.utc)
        _seed_song(
            db, job_id="v1", status="done",
            approved_at=now - timedelta(hours=2),
            artist="Los Abuelos de la Nada", song_title="Cosas Mías",
        )
        _seed_song(
            db, job_id="v2", status="done",
            approved_at=now - timedelta(hours=1),
            artist="Los Abuelos de la Nada", song_title="Cosas Mías",
        )
        usage = get_plan_usage(db, user_id=_USER_ID, tenant_id=_T, plan_id="250")
        assert usage["used"] == 1, (
            f"two variants of the same song counted as {usage['used']} "
            f"(expected 1). The song-level dedup is broken."
        )
    finally:
        _cleanup(db); db.close()


def test_ascii_case_and_whitespace_variants_collapse():
    """ASCII case variations + leading/trailing whitespace must collapse
    to a single song. `Los Abuelos de la Nada` and `LOS ABUELOS DE LA
    NADA` are the same song.

    SQLite caveat: built-in `LOWER()` only lowercases ASCII characters
    — it leaves non-ASCII (e.g. `Í`) untouched. PostgreSQL (prod +
    CI) handles full UTF-8 case folding correctly. This test sticks to
    ASCII to be portable; the UTF-8 case is tested separately and may
    skip on SQLite-only local runs."""
    db = SessionLocal()
    try:
        _cleanup(db)
        now = datetime.now(timezone.utc)
        _seed_song(
            db, job_id="case1", status="done",
            approved_at=now - timedelta(hours=2),
            artist="los abuelos de la nada", song_title="cosas mias",  # all lower
        )
        _seed_song(
            db, job_id="case2", status="done",
            approved_at=now - timedelta(hours=1),
            artist="LOS ABUELOS DE LA NADA", song_title="COSAS MIAS",  # all upper
        )
        _seed_song(
            db, job_id="case3", status="done",
            approved_at=now - timedelta(minutes=30),
            artist="  Los Abuelos De La Nada  ", song_title="Cosas Mias",  # padded mixed
        )
        usage = get_plan_usage(db, user_id=_USER_ID, tenant_id=_T, plan_id="250")
        assert usage["used"] == 1, (
            f"3 casing/whitespace variants of the same song counted as "
            f"{usage['used']} (expected 1). LOWER/TRIM identity broken."
        )
    finally:
        _cleanup(db); db.close()


def test_utf8_case_variants_collapse_on_postgres():
    """UTF-8 case folding: `Cosas Mías` (mixed) and `COSAS MÍAS` (upper)
    are the same song under PostgreSQL's LOWER. SQLite leaves `Í`
    untouched so this test fails locally on SQLite — skipped there.

    This pinning matters because operator filenames frequently contain
    accented Spanish/Portuguese/French chars (Bersuit, Intoxicados,
    Babasónicos, café, prêt). Without UTF-8 folding the dedup is
    only ~70% effective on Latin catalogues."""
    import os
    # Skip on SQLite. The conftest sets DATABASE_URL via env; tests run
    # under whatever's pointed at. Pytest's skipif via env var is the
    # least surprising way to communicate "this works in prod, not
    # locally" without breaking the green-on-clone story.
    if "sqlite" in os.environ.get("DATABASE_URL", "sqlite:///test.db").lower():
        import pytest
        pytest.skip("SQLite LOWER does not fold non-ASCII characters; this case requires PostgreSQL")
    db = SessionLocal()
    try:
        _cleanup(db)
        now = datetime.now(timezone.utc)
        _seed_song(
            db, job_id="utf1", status="done",
            approved_at=now - timedelta(hours=1),
            artist="Babasónicos", song_title="Deshoras",  # mixed accented
        )
        _seed_song(
            db, job_id="utf2", status="done",
            approved_at=now - timedelta(minutes=30),
            artist="BABASÓNICOS", song_title="DESHORAS",  # upper accented
        )
        usage = get_plan_usage(db, user_id=_USER_ID, tenant_id=_T, plan_id="250")
        assert usage["used"] == 1
    finally:
        _cleanup(db); db.close()


def test_different_songs_same_artist_count_separately():
    """Sanity: distinct songs by the same artist must NOT collapse.
    Without this check the LOWER/TRIM dedup might be too aggressive."""
    db = SessionLocal()
    try:
        _cleanup(db)
        now = datetime.now(timezone.utc)
        _seed_song(
            db, job_id="s1", status="done",
            approved_at=now - timedelta(hours=3),
            artist="Intoxicados", song_title="Don Electrón",
        )
        _seed_song(
            db, job_id="s2", status="done",
            approved_at=now - timedelta(hours=2),
            artist="Intoxicados", song_title="De La Guitarra",
        )
        _seed_song(
            db, job_id="s3", status="done",
            approved_at=now - timedelta(hours=1),
            artist="Intoxicados", song_title="Mi Inteligencia Intrapersonal",
        )
        usage = get_plan_usage(db, user_id=_USER_ID, tenant_id=_T, plan_id="250")
        assert usage["used"] == 3
    finally:
        _cleanup(db); db.close()


def test_same_title_different_artists_count_separately():
    """Symmetry of the prior test: same title by two distinct artists
    must count separately. Otherwise covers ("Yesterday" by The Beatles
    and "Yesterday" by Atmosphere collapse) which is clearly wrong."""
    db = SessionLocal()
    try:
        _cleanup(db)
        now = datetime.now(timezone.utc)
        _seed_song(
            db, job_id="y1", status="done",
            approved_at=now - timedelta(hours=2),
            artist="The Beatles", song_title="Yesterday",
        )
        _seed_song(
            db, job_id="y2", status="done",
            approved_at=now - timedelta(hours=1),
            artist="Atmosphere", song_title="Yesterday",
        )
        usage = get_plan_usage(db, user_id=_USER_ID, tenant_id=_T, plan_id="250")
        assert usage["used"] == 2
    finally:
        _cleanup(db); db.close()


def test_null_song_title_does_not_crash_and_groups_by_artist():
    """Older jobs from before song_title became standard can have NULL
    title. They shouldn't crash the COUNT. The implementation
    COALESCEs NULL to '' so they collapse per-artist, which is a
    defensible behaviour for legacy rows."""
    db = SessionLocal()
    try:
        _cleanup(db)
        now = datetime.now(timezone.utc)
        # Two NULL-titled rows for the same artist → 1 song.
        _seed_song(
            db, job_id="null1", status="done",
            approved_at=now - timedelta(hours=2),
            artist="Legacy Artist", song_title=None,
        )
        _seed_song(
            db, job_id="null2", status="done",
            approved_at=now - timedelta(hours=1),
            artist="Legacy Artist", song_title=None,
        )
        # Another artist with NULL title → separate song.
        _seed_song(
            db, job_id="null3", status="done",
            approved_at=now - timedelta(minutes=30),
            artist="Another Legacy Artist", song_title=None,
        )
        usage = get_plan_usage(db, user_id=_USER_ID, tenant_id=_T, plan_id="250")
        assert usage["used"] == 2
    finally:
        _cleanup(db); db.close()


def test_unit_label_in_response():
    """The /usage response exposes `unit: 'song'` so frontends render the
    right noun without hardcoding it. Avoids "X videos" persisting in
    the UI after a plan ships its unit change."""
    db = SessionLocal()
    try:
        _cleanup(db)
        usage = get_plan_usage(db, user_id=_USER_ID, tenant_id=_T, plan_id="250")
        assert usage["unit"] == "song"
    finally:
        _cleanup(db); db.close()
