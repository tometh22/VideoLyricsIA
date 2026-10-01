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
from cost_attribution import is_ci_tenant


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
    "studio_123456789",
])
def test_real_tenants_are_not_ci(tenant):
    assert not ca.is_ci_tenant(tenant)


def test_cliente_con_sufijo_numerico_no_se_clasifica_como_ci():
    job = _job(
        "numeric-client", "Artista", "Tema", "studio_123456789",
    )
    assert ca.classify_job(job, set()) == ca.CAT_OTHER_CLIENT


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


@pytest.mark.parametrize("tenant", ["universality", "universalmedia"])
def test_universal_prefix_without_boundary_is_not_umg(tenant):
    job = _job("j-lookalike", "Quien Sea", "Lo Que Sea", tenant)
    assert ca.classify_job(job, set()) == ca.CAT_OTHER_CLIENT

    attribution = ca.build_attribution(
        {"prod": {job.job_id: job}}, _portal(),
    )
    assert attribution["umg"]["songs"] == 0


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


# ---------------------------------------------------------------------------
# Tenants de laboratorio que escapaban al filtro de CI
# ---------------------------------------------------------------------------
#
# Medido en ago-2026 contra las bases de producción Y staging: 30 tenants
# generados por scripts caían en `otros_clientes`, el bucket de clientes que
# PAGAN. El patrón original era `^dk_d1_` — literalmente una sola de las
# tandas del mismo experimento.
#
# Por qué importa más de lo que parece: el costo por video se calcula
# dividiendo la factura del proveedor por las canciones entregadas. Meter
# barridos de CI en ese denominador lo infla y el $/video sale a la mitad. Y
# ese es justo el número que alguien copia para cotizarle a un sello.
#
# La regla es el SUFIJO DE EPOCH, no el prefijo: los scripts pegan
# `int(time.time())` al final. Enumerar prefijos ya falló una vez —la
# primera versión se dejó `rt2_` afuera— y vuelve a fallar con la próxima
# tanda. Ningún humano nombra una cuenta con un epoch de 10 dígitos.

CI_ESCAPADOS = [
    # Las tandas dk_d2..dk_d4 que `^dk_d1_` no cubría.
    "dk_d2_1780457650", "dk_d2_stars_1780457583",
    "dk_d3_1780457719", "dk_d4_mixed_1780457845",
    # Barridos de matriz.
    "mx_a_1780448038", "mx_f_1780448038",
    "val_v1_solid_1780454342", "val_v4_1780454511",
    "vf_a_1780487123", "vf_d_1780487321",
    # Casos con el color en el nombre: rompen el regex de labels de GCP y
    # aun así se contaban como cliente.
    "cc_a_snow_upper_#33ccff_medium_1.0_1780446180",
    "cc_d_rain_lower_#ff0066_slow_0.5_1780446512",
    # Pruebas de carga / drenaje de cola.
    "long_1780459048", "drain_1780499412",
    # El que la enumeración de prefijos se dejó afuera. Su único job está en
    # `pending_review`, que cuenta como entregado: era el único "cliente"
    # que staging aportaba en junio-2026.
    "rt2_1780431884",
    # Un prefijo que todavía no existe. Este es el punto de la regla.
    "prefijo_que_no_inventamos_1780500000",
]


@pytest.mark.parametrize("tenant", CI_ESCAPADOS)
def test_tenants_de_laboratorio_cuentan_como_ci(tenant):
    assert is_ci_tenant(tenant), f"{tenant} se cuenta como cliente que paga"


# La otra mitad del test importa igual: un patrón demasiado goloso factura
# menos de lo real. Estos son los 9 tenant_id que NO son de laboratorio en
# las dos bases, más nombres cercanos que un prefijo goloso se tragaría.
NO_SON_CI = [
    "universal_chile", "universal_argentina", "umg_archive",
    "genly", "agus77", "omg", "default", "tomas@epical.digital",
    "__internal_samples__",
    "longplay_records",     # empieza con "long" pero sin epoch
    "valparaiso_music",     # empieza con "val" pero sin epoch
    "mx_records", "ccm_estudio", "vfx_studio", "dk_music",
    "sello_2026",           # dígitos al final, pero no un epoch de 10
]


@pytest.mark.parametrize("tenant", NO_SON_CI)
def test_no_se_traga_clientes_reales(tenant):
    assert not is_ci_tenant(tenant), f"{tenant} es un cliente y se descartó"


def test_ci_no_depende_de_la_capitalizacion():
    # La columna es String(100) libre, sin constraint de normalización.
    assert is_ci_tenant("MX_A_1780448038")
    assert is_ci_tenant("  drain_1780499412  ")


# ---------------------------------------------------------------------------
# `clave_facturable` — el denominador del costo por video
# ---------------------------------------------------------------------------

UMG_KEYS = {"los bunkers|nada nuevo bajo el sol", "coti|nada fue un error"}


def test_produccion_gestionada_bajo_cuenta_del_equipo_es_facturable():
    """El bug más caro que tuvo este código.

    Las entregas del portal de UMG corren bajo `agus77`, `default`, `omg`,
    `genly` y `tomas@epical.digital` — no bajo `universal_*`. Filtrar por
    "no está en TEAM_TENANTS" descartaba el 100% de las entregas reales:
    medido en ago-2026, dejaba 23 canciones de las ≥77 entregadas, y el
    costo por canción salía 3,35x arriba del real.
    """
    for cuenta in ("agus77", "default", "omg", "genly", "tomas@epical.digital"):
        clave = ca.clave_facturable(
            cuenta, "los bunkers|nada nuevo bajo el sol", UMG_KEYS)
        assert clave is not None, f"{cuenta} descartada como interna"
        # Todas contra el MISMO contrato: si cada cuenta fuera su propia
        # clave, la misma canción contaría cinco veces.
        assert clave == ("__umg_gestionada__",
                         "los bunkers|nada nuevo bajo el sol")


def test_la_misma_cuenta_haciendo_id_interno_no_es_facturable():
    # Lo que distingue una cosa de la otra es el PORTAL, no el tenant.
    assert ca.clave_facturable("agus77", "prueba|experimento", UMG_KEYS) is None


def test_ci_nunca_es_facturable_aunque_nombre_una_cancion_del_portal():
    # El render bot re-renderiza el catálogo para QA: nombra las mismas
    # canciones. Por eso CI se chequea ANTES que el portal.
    assert ca.clave_facturable(
        "golden_render_bot", "los bunkers|nada nuevo bajo el sol",
        UMG_KEYS) is None


def test_dos_sellos_con_el_mismo_tema_son_dos_entregas():
    """Colisión real medida en producción, jun-2026.

    "La Mosca / Para no verte más" fue entregada por `universal_argentina`
    Y por `universal_chile`. Sin el tenant en la clave, dos entregas
    facturables a dos clientes distintos cuentan como una y el costo por
    canción sale al doble.
    """
    tema = "la mosca|para no verte mas"
    a = ca.clave_facturable("universal_argentina", tema, UMG_KEYS)
    b = ca.clave_facturable("universal_chile", tema, UMG_KEYS)
    assert a is not None and b is not None
    assert a != b
    assert len({a, b}) == 2


def test_jobs_sin_metadata_no_colapsan_en_una_sola_cancion():
    """31 jobs entregados en prod tienen artista y título vacíos.

    `song_key` cae al job_id justamente para esto: la clave `"|"` haría
    que todos ellos sean UNA canción que se lleva la factura entera. En un
    probe contra el endpoint real, 10 jobs sin título dieron $1.000/canción
    en vez de $100.
    """
    claves = {
        ca.clave_facturable("cliente_x", ca.song_key(None, None, jid), UMG_KEYS)
        for jid in ("job-a", "job-b", "job-c")
    }
    assert len(claves) == 3


def test_las_variantes_de_un_mismo_tema_son_una_sola_cancion():
    # Lo contrario del test anterior: una canción entregada arrastra ~2,87
    # jobs entre variantes, re-renders y ediciones. Ésos SÍ colapsan.
    claves = {
        ca.clave_facturable(
            "cliente_x", ca.song_key("Los Bunkers", "Nada Nuevo", jid), UMG_KEYS)
        for jid in ("job-a", "job-b", "job-c")
    }
    assert len(claves) == 1
