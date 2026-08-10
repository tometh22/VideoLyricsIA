"""Cost attribution: whose work was this dollar spent on?

The contract here is subtler than "GROUP BY tenant_id", because of three
facts about how GenLy actually runs (all verified against production data
in ago-2026):

* Managed production for UMG happens in STAGING under team accounts —
  67 of the 68 live portal deliveries have their jobs there, owned by
  `tomas@epical.digital` / `agus77` / `default` / `omg`, not by any
  `universal_*` tenant.
* `golden_render_bot` re-renders real catalogue songs for QA, so a
  song-first classification would bill CI to the client.
* A delivered song carries ~2.4 jobs (variants, re-renders, edits), so
  cost-per-job understates cost-to-deliver by the same factor.
"""

import pytest

import cost_attribution as ca


def _job(job_id, artist, title, tenant, status="done", env="staging",
         cost=0.0, calls=0, cache_hits=0):
    return ca.JobCost(
        job_id=job_id, env=env, tenant_id=tenant, status=status,
        artist=artist, title=title, key=ca.song_key(artist, title),
        created_at=None, cost=cost, billable_calls=calls,
        cache_hits=cache_hits,
    )


def _portal(*songs):
    """Minimal portal payload: one delivery per (artist, title)."""
    keys = {}
    for artist, title in songs:
        k = ca.song_key(artist, title)
        keys[k] = {"key": k, "artist": artist, "title": title,
                   "deliveries": 1, "approved": 1, "job_ids": [],
                   "first_added": None, "tenants": set()}
    return {"songs": keys, "delivery_job_ids": set(),
            "delivery_rows": len(songs)}


# ---------------------------------------------------------------------------
# song_key
# ---------------------------------------------------------------------------

def test_song_key_normalizes_case_and_whitespace():
    assert ca.song_key("Bersuit", "La  Argentinidad") == \
           ca.song_key("  bersuit ", "la argentinidad")


def test_song_key_does_not_merge_different_punctuation():
    """Conservative on purpose: 'Sube sube sube' vs 'Sube, sube, sube' is a
    data-entry problem we want visible, not silently merged."""
    assert ca.song_key("A", "Sube sube sube") != ca.song_key("A", "Sube, sube, sube")


# ---------------------------------------------------------------------------
# Tenant taxonomy
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("tenant", [
    "golden_render_bot", "preflight_staging_20260723_a36233",
    "genly_edit_smoke_ci", "e2e_matrix_1781206970",
    "e2ecustom_1785286338", "dk_d1_stars_1780457583",
])
def test_ci_tenants_are_recognized(tenant):
    assert ca.is_ci_tenant(tenant)


@pytest.mark.parametrize("tenant", [
    "universal_argentina", "universal_chile", "agus77",
    "tomas@epical.digital", "default", "omg", "genly", "",
])
def test_real_tenants_are_not_ci(tenant):
    assert not ca.is_ci_tenant(tenant)


# ---------------------------------------------------------------------------
# Classification order — the load-bearing rule
# ---------------------------------------------------------------------------

def test_render_bot_never_counts_as_client_work_even_for_a_real_song():
    """`golden_render_bot` re-renders the real catalogue for QA. If songs
    were matched before tenants, every regression run would be invoiced to
    UMG."""
    portal = _portal(("Bersuit", "La Argentinidad Al Palo"))
    umg_keys = set(portal["songs"])

    bot = _job("b1", "Bersuit", "La Argentinidad Al Palo",
               "golden_render_bot", cost=8.0, calls=10)
    real = _job("r1", "Bersuit", "La Argentinidad Al Palo",
                "agus77", cost=2.4, calls=3)

    assert ca.classify_job(bot, umg_keys) == ca.CAT_CI
    assert ca.classify_job(real, umg_keys) == ca.CAT_UMG


def test_team_account_on_a_umg_song_is_umg_production():
    """The whole point: managed production runs under team accounts."""
    portal = _portal(("Babasonicos", "Yo anuncio"))
    job = _job("j1", "Babasonicos", "Yo anuncio", "tomas@epical.digital")
    assert ca.classify_job(job, set(portal["songs"])) == ca.CAT_UMG


def test_team_account_on_an_unrelated_song_is_internal_rnd():
    job = _job("j2", "Banda Test", "Cancion De Prueba", "tomas@epical.digital")
    assert ca.classify_job(job, set()) == ca.CAT_RND


def test_movement_gallery_samples_are_internal_rnd():
    job = _job("sample1", "", "", "__internal_samples__", cost=0.8, calls=1)
    assert ca.classify_job(job, set()) == ca.CAT_RND


def test_universal_tenant_is_umg_regardless_of_song():
    """Self-service work by Universal's own users always counts, even for a
    song that never reached the portal."""
    job = _job("j3", "Quien Sea", "Lo Que Sea", "universal_chile")
    assert ca.classify_job(job, set()) == ca.CAT_UMG


def test_unknown_tenant_is_another_client():
    job = _job("j4", "Artista", "Tema", "sello_random")
    assert ca.classify_job(job, set()) == ca.CAT_OTHER_CLIENT


# ---------------------------------------------------------------------------
# build_attribution — levels 1 and 3
# ---------------------------------------------------------------------------

def test_all_jobs_of_a_song_roll_up_to_that_song():
    """A delivered song carries variants and re-renders; every one of them
    was paid for and belongs to the song's cost."""
    portal = _portal(("Bersuit", "La Argentinidad Al Palo"))
    jobs = {"staging": {
        "a": _job("a", "Bersuit", "La Argentinidad Al Palo", "agus77",
                  cost=5.0, calls=6),
        "b": _job("b", "Bersuit", "La Argentinidad Al Palo", "agus77",
                  status="rejected", cost=3.0, calls=4),
        "c": _job("c", "Bersuit", "La Argentinidad Al Palo", "agus77",
                  status="bg_preview_done", cost=1.6, calls=2),
    }}
    out = ca.build_attribution(jobs, portal)

    assert out["umg"]["songs"] == 1
    assert out["umg"]["jobs"] == 3
    assert out["umg"]["direct_cost"] == 9.6
    # The number to price against: everything it took to deliver it once.
    assert out["umg"]["direct_cost_per_song"] == 9.6
    assert out["umg"]["jobs_per_song"] == 3.0


def test_same_song_across_environments_is_one_song():
    portal = _portal(("Intoxicados", "Rodando Por Ahi"))
    jobs = {
        "staging": {"s1": _job("s1", "Intoxicados", "Rodando Por Ahi",
                               "agus77", cost=2.0, calls=2)},
        "prod": {"p1": _job("p1", "Intoxicados", "Rodando Por Ahi",
                            "universal_argentina", env="prod",
                            cost=3.0, calls=3)},
    }
    out = ca.build_attribution(jobs, portal)
    assert out["umg"]["songs"] == 1
    assert out["umg"]["direct_cost"] == 5.0
    assert out["umg"]["by_song"][0]["envs"] == ["prod", "staging"]


def test_ci_cost_is_excluded_from_umg_but_present_in_business():
    """CI is real money and must show up in the business view — it just
    must not inflate what we tell the client a video costs."""
    portal = _portal(("Bersuit", "La Argentinidad Al Palo"))
    jobs = {"staging": {
        "real": _job("real", "Bersuit", "La Argentinidad Al Palo",
                     "agus77", cost=2.0, calls=2),
        "bot": _job("bot", "Bersuit", "La Argentinidad Al Palo",
                    "golden_render_bot", cost=8.0, calls=10),
    }}
    out = ca.build_attribution(jobs, portal)

    assert out["umg"]["direct_cost"] == 2.0
    cats = {c["category"]: c for c in out["business"]["by_category"]}
    assert cats[ca.CAT_CI]["cost"] == 8.0
    assert out["business"]["total_direct_cost"] == 10.0
    assert out["business"]["umg_share_of_cost"] == 0.2


def test_portal_song_with_no_job_anywhere_is_flagged_not_zeroed():
    """Deleting a job cascades its provenance away, so the spend becomes
    unrecoverable. Reporting it as a $0 song would drag the average down
    and look like an efficiency win."""
    portal = _portal(("Fantasma", "Sin Job"))
    out = ca.build_attribution({"staging": {}}, portal)
    assert out["portal"]["songs_with_deleted_jobs"] == [
        ca.song_key("Fantasma", "Sin Job")]
    assert out["umg"]["songs"] == 0
    assert out["umg"]["direct_cost_per_song"] is None


def test_song_produced_in_another_month_is_not_reported_as_deleted():
    """The portal is loaded late — the 24 deliveries added in ago-2026 were
    jul-2026 work. Those must read as 'outside the period', not as data
    loss, or every month looks like it lost records."""
    portal = _portal(("Mercedes Sosa", "Sube Sube"))
    key = ca.song_key("Mercedes Sosa", "Sube Sube")
    out = ca.build_attribution({"staging": {}}, portal, period="2026-07",
                               all_time_song_keys={key})
    assert out["portal"]["songs_produced_outside_period"] == [key]
    assert out["portal"]["songs_with_deleted_jobs"] == []


def test_cache_hits_are_reported_but_not_charged():
    portal = _portal(("A", "B"))
    jobs = {"staging": {"j": _job("j", "A", "B", "agus77",
                                  cost=0.80, calls=1, cache_hits=5)}}
    out = ca.build_attribution(jobs, portal)
    assert out["umg"]["direct_cost"] == 0.80
    assert out["umg"]["by_song"][0]["cache_hits"] == 5


def test_conservation_umg_plus_rest_equals_total():
    """Nothing may be double-counted or silently dropped between levels."""
    portal = _portal(("A", "B"))
    jobs = {"staging": {
        "u": _job("u", "A", "B", "agus77", cost=2.0, calls=2),
        "c": _job("c", "X", "Y", "golden_render_bot", cost=3.0, calls=3),
        "r": _job("r", "P", "Q", "default", cost=1.0, calls=1),
        "o": _job("o", "M", "N", "otro_sello", cost=4.0, calls=4),
    }}
    out = ca.build_attribution(jobs, portal)
    per_cat = sum(c["cost"] for c in out["business"]["by_category"])
    assert per_cat == pytest.approx(out["business"]["total_direct_cost"])
    assert out["business"]["total_direct_cost"] == 10.0
    assert out["umg"]["direct_cost"] == 2.0


def test_job_id_collision_across_environments_is_surfaced():
    """Job ids are 12-char hex and independently generated per environment,
    so a collision is possible. Silently merging would lose a job's cost."""
    portal = _portal(("A", "B"))
    jobs = {
        "staging": {"dup": _job("dup", "A", "B", "agus77", cost=1.0)},
        "prod": {"dup": _job("dup", "A", "B", "universal_chile",
                             env="prod", cost=2.0)},
    }
    out = ca.build_attribution(jobs, portal)
    assert len(out["id_collisions"]) == 1
    # Both are still counted — we report the ambiguity, we don't drop money.
    assert out["umg"]["direct_cost"] == 3.0


# ---------------------------------------------------------------------------
# Level 2 — prorating shared infrastructure
# ---------------------------------------------------------------------------

def test_level_2_prorates_shared_infra_by_umg_share():
    portal = _portal(("A", "B"))
    jobs = {"staging": {
        "u": _job("u", "A", "B", "agus77", cost=6.0, calls=6),
        "c": _job("c", "X", "Y", "golden_render_bot", cost=4.0, calls=4),
    }}
    out = ca.build_attribution(jobs, portal)
    assert out["business"]["umg_share_of_cost"] == 0.6

    ca.add_total_cost(out, {"gcp": 200.0, "railway": 100.0, "fixed": 44.0},
                      revenue_usd=2000.0, basis="cost")
    t = out["umg_total"]
    # Direct providers (gcp) and shared ones (railway+fixed) are prorated
    # by the same share but reported apart, so the split is auditable.
    assert t["umg_direct_cost"] == pytest.approx(120.0)
    assert t["umg_shared_cost"] == pytest.approx(86.4)
    assert t["umg_total_cost"] == pytest.approx(206.4)
    assert t["total_cost_per_song"] == pytest.approx(206.4)
    assert t["gross_profit"] == pytest.approx(1793.6)


def test_basis_governs_shared_infra_only_never_the_ai_invoice():
    """`basis="jobs"` must not touch the direct AI invoice.

    AI spend is per-call and level 1 already attributed it call by call; a
    job-count share would price one Veo-heavy delivery the same as one
    whisper-only smoke test. On this fixture that is a 2.7x error applied
    to GCP — the largest line in the bill.
    """
    portal = _portal(("A", "B"))
    jobs = {"staging": {
        "u": _job("u", "A", "B", "agus77", cost=9.0, calls=9),
        "c": _job("c", "X", "Y", "golden_render_bot", cost=1.0, calls=1),
        "d": _job("d", "Z", "W", "golden_render_bot", cost=0.0, calls=0),
    }}
    out = ca.build_attribution(jobs, portal)
    ca.add_total_cost(out, {"gcp": 100.0, "railway": 100.0}, basis="jobs")
    t = out["umg_total"]

    # By cost UMG is 90%; by jobs only 33%.
    assert t["share_by_cost"] == 0.9
    assert t["share_by_jobs"] == pytest.approx(1 / 3, abs=1e-3)
    # Shared infra follows `basis`...
    assert t["share_used_for_shared_infra"] == t["share_by_jobs"]
    assert t["umg_shared_cost"] == pytest.approx(33.33, abs=0.02)
    # ...but the AI invoice always follows the cost share.
    assert t["share_used_for_direct_ai"] == 0.9
    assert t["umg_direct_cost"] == pytest.approx(90.0)


# ---------------------------------------------------------------------------
# Regressions found by adversarial review (ago-2026)
# ---------------------------------------------------------------------------

def test_denominator_counts_delivered_songs_only():
    """The defect the whole audit exists to prevent, one level up.

    A song that was worked on but never shipped still costs money, so it
    belongs in the numerator — but putting it in the denominator makes
    delivering look cheaper than it is. Measured on real jun-2026 data:
    51 songs touched vs 37 delivered, a 38% understatement.
    """
    portal = _portal(("Entregada", "Sí"), ("Abandonada", "No"))
    jobs = {"staging": {
        "d": _job("d", "Entregada", "Sí", "agus77",
                  status="done", cost=6.0, calls=6),
        # Only ever produced a discarded preview.
        "p": _job("p", "Abandonada", "No", "agus77",
                  status="bg_preview_done", cost=4.0, calls=4),
    }}
    out = ca.build_attribution(jobs, portal)

    assert out["umg"]["songs"] == 1                    # delivered
    assert out["umg"]["songs_touched"] == 2
    assert out["umg"]["songs_touched_not_delivered"] == 1
    # Both costs are in the numerator; only the delivered one divides.
    assert out["umg"]["direct_cost"] == 10.0
    assert out["umg"]["direct_cost_per_song"] == 10.0   # NOT 5.0
    assert out["umg"]["cost_of_undelivered_songs"] == 4.0


def test_blank_metadata_jobs_do_not_merge_into_one_fake_song():
    """Without a guard, `song_key("", "")` is `"|"` for every job, merging
    every metadata-less job in both databases into a single 'song' — one
    denominator slot carrying an unbounded numerator."""
    jobs = {"staging": {
        "x": _job("x", "", "", "universal_chile", cost=5.0, calls=5),
        "y": _job("y", "", "", "universal_chile", cost=7.0, calls=7),
    }}
    # Rebuild keys the way collect_jobs does (per job id).
    for jid, j in jobs["staging"].items():
        j.key = ca.song_key(j.artist, j.title, jid)

    out = ca.build_attribution(jobs, _portal())
    assert out["umg"]["songs_touched"] == 2, "no deben colapsar en una"


def test_preview_placeholder_is_not_a_song():
    """The background-preview path writes artist/title = "preview" when the
    caller has none (`body.artist or "preview"`), so every discarded
    preview in a tenant would otherwise merge into one giant fake song."""
    k1 = ca.song_key("preview", "preview", "job1")
    k2 = ca.song_key("preview", "preview", "job2")
    assert k1 != k2
    assert "__sin_metadata__" in k1


def test_period_bounds_rejects_bad_month():
    with pytest.raises(ValueError):
        ca.period_bounds("2026-13")


def test_period_bounds_wraps_december():
    start, end = ca.period_bounds("2026-12")
    assert (start.year, start.month) == (2026, 12)
    assert (end.year, end.month) == (2027, 1)
