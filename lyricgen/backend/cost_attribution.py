"""Attribute AI spend to what it was actually produced for.

`provenance.py` answers "how much did we spend"; this module answers "on
whose behalf". The two are different questions and the second one is what
you need to price a contract.

Why this is not a one-line GROUP BY tenant_id
---------------------------------------------
Three facts about how GenLy actually operates make naive attribution wrong,
all of them verified against production data in ago-2026:

1. **Managed production for UMG runs in STAGING, under team accounts.**
   67 of the 68 live deliveries in the `umg.genly.pro` portal have their
   jobs in the staging database, owned by `tomas@epical.digital`, `agus77`,
   `default` and `omg` — not by any `universal_*` tenant. Attributing by
   `tenant_id` alone misses essentially all of the managed work.

2. **Staging is not a free environment.** Staging and production share one
   GCP project, one R2 bucket and one Railway project, so the invoices
   cannot be split by provider. The split has to come from the databases.

3. **The billable unit is the song, not the job.** A delivered song carries
   2.87 jobs on average (variants, re-renders, edits). Cost per job flatters
   the number; cost per delivered song is what a client pays for.

So attribution keys off a **song identity** (`artist|title`, normalized) and
unions across both environments.

Classification order matters
----------------------------
A `golden_render_bot` job can carry the same song title as a real delivery —
the render bot re-renders the catalogue for QA. Classifying by tenant first
would fold CI cost into UMG; classifying by song first would fold CI cost
into UMG too. The resolution is that CI tenants are checked FIRST and can
never be UMG, no matter what song they name. See `classify_job`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone

from sqlalchemy import and_, func, or_

from database import AIProvenance, Job
from provenance import (
    CACHE_HIT_PREFIX,
    DELIVERED_STATUSES,
    billable_filter,
    cost_for_record,
    delivered_job_filter,
    job_was_delivered,
)

# ---------------------------------------------------------------------------
# Tenant taxonomy
# ---------------------------------------------------------------------------

# Automated tenants: CI, smoke tests, the golden-render regression bot,
# staging pre-flight sweeps. These spend real money and must appear in the
# business-wide view, but they are never client work. Checked before
# anything else — the render bot deliberately re-renders real catalogue
# songs, so a song-first check would misfile it as UMG production.
CI_TENANT_PATTERNS = (
    re.compile(r"^golden_render_bot$"),
    re.compile(r"^preflight_"),
    re.compile(r"_smoke_|^genly_edit_smoke"),
    re.compile(r"^e2e"),
    # Barridos de laboratorio: el sufijo de EPOCH es la señal.
    #
    # `^dk_d1_` estaba solo y 29 tenants con la misma forma caían en
    # `otros_clientes`, el bucket de clientes que PAGAN. La primera versión
    # de este arreglo enumeró los prefijos que existían ese día
    # (`mx_`, `val_v`, `cc_`, `vf_`, `long_`, `drain_`) y se dejó `rt2_`
    # afuera — la siguiente tanda con un prefijo nuevo volvía a colarse sin
    # que nada avisara.
    #
    # Lo que TODOS comparten no es el prefijo sino cómo se generan: el
    # script les pega `int(time.time())` al final. Ningún humano nombra una
    # cuenta con un epoch de 10 dígitos, así que esa es la regla y los
    # prefijos dejan de importar. Verificado contra los 47 tenant_id
    # distintos de las dos bases: matchea los 30 de laboratorio y ningún
    # cliente real.
    re.compile(r"_\d{10}$"),
    # Se conserva por linaje: cubre un `dk_d…` sin sufijo de epoch.
    re.compile(r"^dk_d\d+[_-]"),
)

# Accounts the GenLy team operates. Their jobs are managed production when
# the song is in the UMG universe, and internal R&D otherwise.
TEAM_TENANTS = frozenset({
    "genly", "default", "omg", "agus77", "tomas@epical.digital",
    "golden_render_bot",   # also CI-matched; listed for completeness
    "__internal_samples__",  # platform-owned movement gallery generations
})

UMG_TENANT_RE = re.compile(r"universal(?:[_-][a-z0-9_-]+)?")

# Categories emitted by `classify_job`.
CAT_UMG = "umg_produccion"
CAT_OTHER_CLIENT = "otros_clientes"
CAT_CI = "automatizacion_ci"
CAT_RND = "id_interno"


def is_ci_tenant(tenant_id: str | None) -> bool:
    # `.lower()` igual que `is_umg_tenant`: la columna es String(100) libre,
    # sin constraint de normalización, así que `MX_A_178…` entraba como
    # cliente que paga sólo por la mayúscula.
    t = (tenant_id or "").strip().lower()
    return any(p.search(t) for p in CI_TENANT_PATTERNS)


def is_team_tenant(tenant_id: str | None) -> bool:
    """Cuenta operada por el equipo. Normaliza igual que las otras dos."""
    return (tenant_id or "").strip().lower() in TEAM_TENANTS


def is_umg_tenant(tenant_id: str | None) -> bool:
    """Match Universal account IDs without swallowing lookalike tenants."""
    tenant = (tenant_id or "").strip().lower()
    return bool(UMG_TENANT_RE.fullmatch(tenant))


# Placeholder song metadata written by the background-preview path when the
# caller has no artist/title yet (`main.py` uses `body.artist or "preview"`).
# These are not songs and must never merge into one.
_PLACEHOLDER_TITLES = frozenset({"preview", "untitled", "sin titulo", "sin título"})


def song_key(artist: str | None, title: str | None,
             job_id: str | None = None) -> str:
    """Normalized song identity, used to join across environments.

    Collapses case and whitespace runs. Deliberately conservative: it does
    NOT strip punctuation or accents, because "Sube sube sube" and
    "Sube, sube, sube" being separate rows is a data-entry issue we would
    rather see than silently merge.

    **Degenerate identities fall back to the job id.** A blank artist AND
    title would otherwise produce the key `"|"`, silently merging every
    metadata-less job in both databases into a single enormous fake song —
    one row in the denominator carrying an unbounded numerator. Same for
    the `"preview|preview"` placeholder the background-preview path writes.
    Falling back to `job_id` keeps each one distinct (and, having no
    delivered job, they are excluded from the per-song denominator anyway).
    """
    a = re.sub(r"\s+", " ", (artist or "").strip().lower())
    t = re.sub(r"\s+", " ", (title or "").strip().lower())
    degenerate = (not a and not t) or (t in _PLACEHOLDER_TITLES and
                                       a in _PLACEHOLDER_TITLES | {""})
    if degenerate:
        return f"__sin_metadata__|{job_id or 'desconocido'}"
    return f"{a}|{t}"


# ---------------------------------------------------------------------------
# Per-environment extraction
# ---------------------------------------------------------------------------

@dataclass
class JobCost:
    """One job, with its billable AI spend resolved."""
    job_id: str
    env: str
    tenant_id: str
    status: str
    artist: str
    title: str
    key: str
    created_at: datetime | None
    completed_at: datetime | None = None
    editing_started_at: datetime | None = None
    # None means an all-time report. For a monthly report this is computed
    # from the terminal timestamp, so a job that merely incurs spend in the
    # month does not also inflate that month's delivery denominator.
    delivered_in_period: bool | None = None
    cost: float = 0.0
    billable_calls: int = 0
    cache_hits: int = 0
    by_tool: dict = field(default_factory=dict)
    # Gasto por FUENTE DE FACTURA (gcp / openai / replicate). Es lo que
    # permite prorratear cada factura directa con su propia proporción de uso
    # en vez de con una mezcla: trabajo interno pesado en Replicate movía la
    # proporción aplicada a la factura de GCP aunque el uso de GCP de UMG no
    # hubiera cambiado, y el error salía reportado como costo de UMG.
    by_source: dict = field(default_factory=dict)

    @property
    def delivered(self) -> bool:
        if self.delivered_in_period is not None:
            return self.delivered_in_period
        return job_was_delivered(
            self.status, self.completed_at, self.editing_started_at)


def invoice_source_of(tool_provider: str | None) -> str:
    """Fuente de factura de un proveedor de provenance.

    Las tres directas se facturan por llamada, así que cada una tiene su
    propia proporción de uso y hay que prorratearlas por separado.
    """
    return {
        "google_vertex": "gcp",
        "openai": "openai",
        "replicate": "replicate",
    }.get((tool_provider or "").lower(), "otros")


def period_bounds(period: str) -> tuple[datetime, datetime]:
    """"2026-07" -> aware UTC datetimes [start, end_exclusive)."""
    if (len(period) != 7 or period[4] != "-"
            or not period[:4].isdigit() or not period[5:].isdigit()):
        raise ValueError(f"período inválido: {period!r}; se espera YYYY-MM")
    year, month = (int(x) for x in period.split("-", 1))
    if not 1 <= month <= 12:
        raise ValueError(f"período inválido: {period!r}")
    start = datetime(year, month, 1, tzinfo=timezone.utc)
    end = (datetime(year + (month == 12), (month % 12) + 1, 1,
                    tzinfo=timezone.utc))
    return start, end


def collect_jobs(db, env: str, period: str | None = None,
                 rates: dict[str, float] | None = None) -> dict[str, JobCost]:
    """All jobs in one environment with their billable spend attached.

    `period` bounds `AIProvenance.created_at`; None takes the whole history.
    Bounding the provenance side matters: a job created on 30-jun that
    re-rolls scenes into july would otherwise put july's spend in june's
    bucket, and june is then compared against june's invoice. The two sides
    have to cover the same window or the reconciliation is meaningless.

    The job side is a UNION, not a filter: jobs delivered in the period
    (the denominator, spend or no spend) **plus** any job that incurred spend
    inside it. Filtering by `created_at` dropped a june job whose background
    was re-generated through `run_edit_pipeline` in july; using it as the
    delivery date also assigned june-30 uploads completed on july-2 to the
    wrong denominator.

    `rates` are the invoice-derived per-call rates. They MUST be passed in
    when reading more than one environment: the calibration snapshot lives
    in whichever database `/cost/calibrate-rates` was run against, so a
    peer session would silently fall back to list price and value identical
    calls differently inside a single report.

    Returns a dict keyed by job_id so the caller can merge environments —
    job_ids are 12-char hex and collide across environments only by
    accident, which `merge_environments` reports rather than silently
    resolving.
    """
    bounds = period_bounds(period) if period else None

    q = db.query(Job.job_id, Job.tenant_id, Job.status, Job.artist,
                 Job.song_title, Job.created_at, Job.completed_at,
                 Job.editing_started_at)
    if bounds:
        con_gasto = [r[0] for r in
                     db.query(AIProvenance.job_id)
                       .filter(AIProvenance.created_at >= bounds[0],
                               AIProvenance.created_at < bounds[1])
                       .distinct().all()]
        entregado_en = func.coalesce(Job.completed_at, Job.created_at)
        entregado_en_periodo = and_(
            delivered_job_filter(),
            entregado_en >= bounds[0],
            entregado_en < bounds[1],
        )
        q = (q.filter(or_(entregado_en_periodo, Job.job_id.in_(con_gasto)))
             if con_gasto else q.filter(entregado_en_periodo))

    jobs: dict[str, JobCost] = {}
    for (job_id, tenant_id, status, artist, title, created_at,
         completed_at, editing_started_at) in q.all():
        delivered_in_period = None
        if bounds:
            delivered_at = completed_at or created_at
            # SQLite (tests/local) drops timezone information even for
            # timezone-aware columns; production Postgres preserves it.
            if delivered_at is not None and delivered_at.tzinfo is None:
                delivered_at = delivered_at.replace(tzinfo=timezone.utc)
            delivered_in_period = bool(
                job_was_delivered(status, completed_at, editing_started_at)
                and delivered_at is not None
                and bounds[0] <= delivered_at < bounds[1]
            )
        jobs[job_id] = JobCost(
            job_id=job_id, env=env, tenant_id=tenant_id or "",
            status=status or "", artist=artist or "", title=title or "",
            key=song_key(artist, title, job_id), created_at=created_at,
            completed_at=completed_at,
            editing_started_at=editing_started_at,
            delivered_in_period=delivered_in_period,
        )

    if not jobs:
        return jobs

    # Tarifas derivadas de la factura del período. Sin esto el costo sale con
    # precio de lista y se va ~25% arriba en Veo. El caller las pasa una sola
    # vez para TODOS los entornos: la calibración vive en la base donde se
    # corrió `/cost/calibrate-rates`, y leerla por sesión valuaba las mismas
    # llamadas a precios distintos según el entorno.
    if rates is None:
        rates = {}
        if period:
            from rate_calibration import load_applied_rates
            rates = load_applied_rates(db, period)

    # Billable spend per job/tool. Cache hits are counted separately (never
    # charged) so the panel can show how much the cache actually saved.
    prov = (
        db.query(
            AIProvenance.job_id,
            AIProvenance.tool_name,
            AIProvenance.tool_provider,
            func.count(AIProvenance.id),
        )
        .filter(AIProvenance.job_id.in_(list(jobs)))
        .filter(billable_filter())
    )
    if bounds:
        prov = prov.filter(AIProvenance.created_at >= bounds[0],
                           AIProvenance.created_at < bounds[1])
    rows = prov.group_by(AIProvenance.job_id, AIProvenance.tool_name,
                         AIProvenance.tool_provider).all()

    for job_id, tool_name, tool_provider, calls in rows:
        job = jobs.get(job_id)
        if job is None:
            continue
        cost = calls * cost_for_record(tool_name, tool_provider, rates)
        job.cost += cost
        job.billable_calls += calls
        job.by_tool[tool_name] = job.by_tool.get(tool_name, 0.0) + cost
        src = invoice_source_of(tool_provider)
        job.by_source[src] = job.by_source.get(src, 0.0) + cost

    # Acotada al período igual que la de gasto. Desde que los jobs viejos con
    # gasto adentro entran al informe, dejar esta query sin acotar traía
    # TODOS los cache hits históricos de ese job al mes reportado — la
    # métrica de ahorro por caché acumulaba la vida entera del job.
    hits_q = (
        db.query(AIProvenance.job_id, func.count(AIProvenance.id))
        .filter(AIProvenance.job_id.in_(list(jobs)))
        # Non-billable is broader than cache hit: cache-only misses, budget
        # rejections and pending reservations are all free, but none reused an
        # artifact or saved a generation. Count only an actual served hit.
        .filter(AIProvenance.response_summary.like(f"{CACHE_HIT_PREFIX}%"))
    )
    if bounds:
        hits_q = hits_q.filter(AIProvenance.created_at >= bounds[0],
                               AIProvenance.created_at < bounds[1])
    hit_rows = hits_q.group_by(AIProvenance.job_id).all()
    for job_id, hits in hit_rows:
        if job_id in jobs:
            jobs[job_id].cache_hits = int(hits)

    return jobs


def collect_song_keys(db) -> set[str]:
    """Every song identity present in an environment, ignoring any period.

    Cheap (no provenance join). Used to tell apart the two very different
    reasons a portal song shows no cost inside a period: its jobs ran in a
    *different* month (normal — the portal is loaded late), or its jobs no
    longer exist at all (the job was deleted and provenance cascaded away,
    so the spend is unrecoverable). Reporting both as "unmeasurable" would
    make ordinary month-boundary spill look like data loss.
    """
    return {
        song_key(artist, title, job_id)
        for job_id, artist, title in
        db.query(Job.job_id, Job.artist, Job.song_title).all()
    }


def collect_portal_songs(db_prod) -> dict:
    """The songs UMG actually asked for, from the deliveries portal.

    Reads `artist_snapshot`/`song_title_snapshot` rather than joining to
    `jobs`: the portal is in production but its rows point at STAGING job
    ids, and deleting a job cascades its provenance away. The snapshot
    columns survive that, so a delivery whose job is gone still defines a
    song we owe — it just cannot be costed. `orphan_job_ids` reports those
    instead of letting them read as $0.

    Only live deliveries (`removed_at IS NULL`) count; a removed delivery
    was retracted.
    """
    from database import Delivery  # local import: portal is prod-only

    rows = (
        db_prod.query(
            Delivery.job_id, Delivery.artist_snapshot,
            Delivery.song_title_snapshot, Delivery.tenant_snapshot,
            Delivery.added_at, Delivery.approved_at,
        )
        .filter(Delivery.removed_at.is_(None))
        .all()
    )
    keys: dict[str, dict] = {}
    job_ids: set[str] = set()
    for job_id, artist, title, tenant, added_at, approved_at in rows:
        k = song_key(artist, title, job_id)
        job_ids.add(job_id)
        entry = keys.setdefault(k, {
            "key": k, "artist": artist, "title": title,
            "deliveries": 0, "approved": 0, "job_ids": [],
            "first_added": added_at, "tenants": set(),
        })
        entry["deliveries"] += 1
        entry["approved"] += 1 if approved_at else 0
        entry["job_ids"].append(job_id)
        entry["tenants"].add(tenant or "")
        if added_at and entry["first_added"] and added_at < entry["first_added"]:
            entry["first_added"] = added_at
    return {"songs": keys, "delivery_job_ids": job_ids,
            "delivery_rows": len(rows)}


# ---------------------------------------------------------------------------
# Cross-environment merge + classification
# ---------------------------------------------------------------------------

def classify_key(tenant_id: str | None, key: str, umg_keys: set[str]) -> str:
    """Bucket, a partir de tenant + identidad de canción. El orden importa.

    Un job de `golden_render_bot` puede nombrar la misma canción que una
    entrega real —el bot re-renderiza el catálogo para QA—, así que CI se
    chequea PRIMERO y nunca puede ser UMG, diga la canción que diga.
    """
    if is_ci_tenant(tenant_id):
        return CAT_CI
    if is_umg_tenant(tenant_id):
        return CAT_UMG
    # La producción gestionada corre bajo cuentas del EQUIPO: el portal es
    # el que dice que esa canción se entregó y se cobró. Este chequeo va
    # antes que TEAM_TENANTS, si no se descarta como I+D interno.
    if key in umg_keys:
        return CAT_UMG
    if is_team_tenant(tenant_id):
        return CAT_RND
    return CAT_OTHER_CLIENT


def classify_job(job: JobCost, umg_keys: set[str]) -> str:
    """Bucket a job. Order is load-bearing — see the module docstring."""
    return classify_key(job.tenant_id, job.key, umg_keys)


# Categorías que le facturan a alguien. `CAT_CI` es infraestructura de
# pruebas y `CAT_RND` es I+D interno: ninguna de las dos genera un video
# vendido, así que ninguna puede estar en el denominador del costo por video.
CATEGORIAS_FACTURABLES = frozenset({CAT_UMG, CAT_OTHER_CLIENT})


def clave_facturable(tenant_id: str | None, key: str,
                     umg_keys: set[str]) -> tuple[str, str] | None:
    """Identidad de la unidad que se factura, o `None` si no se factura.

    Deduplica canciones para el denominador de "costo por video". Dos
    decisiones que costaron dinero cuando estuvieron mal:

    * **El tenant forma parte de la clave.** Dos sellos distintos pueden
      entregar el mismo tema — pasó en jun-2026 con "La Mosca / Para no
      verte más", entregada por `universal_argentina` Y `universal_chile`.
      Sin el tenant las dos entregas facturables cuentan como una y el
      costo por canción sale al doble.

    * **La producción gestionada de UMG colapsa en un solo comprador.**
      Esas canciones se entregan desde varias cuentas del equipo
      (`agus77`, `default`, `omg`, …) contra un único contrato, y encima
      pueden tener jobs en los dos entornos. Contarlas por cuenta las
      duplicaría.
    """
    cat = classify_key(tenant_id, key, umg_keys)
    if cat not in CATEGORIAS_FACTURABLES:
        return None
    if cat == CAT_UMG and not is_umg_tenant(tenant_id):
        return ("__umg_gestionada__", key)
    return ((tenant_id or "").strip().lower(), key)


def build_attribution(jobs_by_env: dict[str, dict[str, JobCost]],
                      portal: dict,
                      period: str | None = None,
                      all_time_song_keys: set[str] | None = None) -> dict:
    """The whole analysis: levels 1 and 3 (level 2 needs invoice totals).

    `jobs_by_env` maps env name -> output of `collect_jobs`.
    `all_time_song_keys` (from `collect_song_keys`, unfiltered) lets the
    report distinguish portal songs whose jobs simply ran in another month
    from those whose jobs were deleted outright.
    """
    all_jobs: list[JobCost] = []
    id_collisions = []
    seen: dict[str, str] = {}
    for env, jobs in jobs_by_env.items():
        for job_id, job in jobs.items():
            if job_id in seen:
                id_collisions.append({"job_id": job_id,
                                      "envs": [seen[job_id], env]})
            seen[job_id] = env
            all_jobs.append(job)

    # --- UMG universe -------------------------------------------------
    # Portal songs (managed production) + anything under a universal_*
    # tenant (self-service). A song qualifies regardless of which
    # environment or account produced it.
    umg_keys: set[str] = set(portal["songs"])
    for job in all_jobs:
        if is_umg_tenant(job.tenant_id) and job.delivered:
            umg_keys.add(job.key)

    # --- classify ------------------------------------------------------
    by_category: dict[str, dict] = {}
    # Gasto medido por fuente de factura, para UMG y para el total. Cada
    # factura directa se prorratea después con SU proporción, no con la mezcla.
    umg_by_source: dict[str, float] = {}
    total_by_source: dict[str, float] = {}
    for job in all_jobs:
        cat = classify_job(job, umg_keys)
        for _src, _c in job.by_source.items():
            total_by_source[_src] = total_by_source.get(_src, 0.0) + _c
            if cat == CAT_UMG:
                umg_by_source[_src] = umg_by_source.get(_src, 0.0) + _c
        agg = by_category.setdefault(cat, {
            "category": cat, "jobs": 0, "cost": 0.0,
            "billable_calls": 0, "cache_hits": 0,
            "delivered_jobs": 0, "songs": set(),
        })
        agg["jobs"] += 1
        agg["cost"] += job.cost
        agg["billable_calls"] += job.billable_calls
        agg["cache_hits"] += job.cache_hits
        agg["delivered_jobs"] += 1 if job.delivered else 0
        agg["songs"].add(job.key)

    # --- level 1: per-song UMG cost ------------------------------------
    per_song: dict[str, dict] = {}
    for job in all_jobs:
        if classify_job(job, umg_keys) != CAT_UMG:
            continue
        entry = per_song.setdefault(job.key, {
            "key": job.key, "artist": job.artist, "title": job.title,
            "jobs": 0, "cost": 0.0, "billable_calls": 0, "cache_hits": 0,
            "delivered_jobs": 0, "envs": set(), "tenants": set(),
            "in_portal": job.key in portal["songs"],
        })
        # Prefer a non-empty artist/title for display; CI jobs sometimes
        # carry blanks.
        if not entry["artist"] and job.artist:
            entry["artist"] = job.artist
        if not entry["title"] and job.title:
            entry["title"] = job.title
        entry["jobs"] += 1
        entry["cost"] += job.cost
        entry["billable_calls"] += job.billable_calls
        entry["cache_hits"] += job.cache_hits
        entry["delivered_jobs"] += 1 if job.delivered else 0
        entry["envs"].add(job.env)
        entry["tenants"].add(job.tenant_id)

    # Portal songs that produced no measured cost in this window. Two very
    # different causes, so they are reported separately — see
    # `collect_song_keys`.
    missing = set(portal["songs"]) - set(per_song)
    if all_time_song_keys is None:
        outside_period, deleted = [], sorted(missing)
    else:
        outside_period = sorted(missing & all_time_song_keys)
        deleted = sorted(missing - all_time_song_keys)

    songs = sorted(per_song.values(), key=lambda s: -s["cost"])
    for s in songs:
        s["envs"] = sorted(s["envs"])
        s["tenants"] = sorted(s["tenants"])
        s["delivered"] = s["delivered_jobs"] > 0

    umg_cost = sum(s["cost"] for s in songs)

    # THE denominator. Only songs that actually shipped can be invoiced, so
    # only they may divide the cost. Songs that were touched but produced
    # nothing (previews the operator abandoned, jobs that ended rejected)
    # still contribute their spend to the numerator — someone paid for
    # them — but adding them to the denominator would understate the cost
    # of delivering. Measured jun-2026: 51 songs touched, 37 delivered;
    # dividing by 51 understated cost per song by 38%.
    #
    # This is the same defect the whole audit started from ("divide by jobs
    # created instead of delivered"), one level up. It is easy to
    # reintroduce, hence the explicit split and the test that pins it.
    delivered_songs = [s for s in songs if s["delivered"]]
    umg_songs = len(delivered_songs)
    umg_songs_touched = len(songs)

    categories = []
    total_cost = 0.0
    total_jobs = 0
    for agg in by_category.values():
        agg["songs"] = len(agg["songs"])
        agg["cost"] = round(agg["cost"], 4)
        total_cost += agg["cost"]
        total_jobs += agg["jobs"]
        categories.append(agg)
    categories.sort(key=lambda c: -c["cost"])

    return {
        "period": period,
        "environments": sorted(jobs_by_env),
        "portal": {
            "delivery_rows": portal["delivery_rows"],
            "distinct_songs": len(portal["songs"]),
            # Jobs exist, just not in this period — the portal is often
            # loaded a month late. Normal, not data loss.
            "songs_produced_outside_period": outside_period,
            # No job in any environment: deleted, provenance cascaded.
            # The money was spent and is unrecoverable from the DB.
            "songs_with_deleted_jobs": deleted,
        },
        # --- Level 1 ---
        "umg": {
            # `songs` = DELIVERED songs. Anything divided by it is a real
            # unit cost; `songs_touched` is a diagnostic, never a divisor.
            "songs": umg_songs,
            "songs_touched": umg_songs_touched,
            "songs_touched_not_delivered": umg_songs_touched - umg_songs,
            "jobs": sum(s["jobs"] for s in songs),
            "direct_cost": round(umg_cost, 4),
            "direct_cost_per_song": (
                round(umg_cost / umg_songs, 4) if umg_songs else None
            ),
            # Spend on songs that never shipped, carried by the ones that
            # did. Sizing the prize for fixing the waste.
            "cost_of_undelivered_songs": round(
                sum(s["cost"] for s in songs if not s["delivered"]), 4),
            "jobs_per_song": (
                round(sum(s["jobs"] for s in songs) / umg_songs, 2)
                if umg_songs else None
            ),
            "by_song": songs,
        },
        # --- Level 3 ---
        "business": {
            "total_direct_cost": round(total_cost, 4),
            "total_jobs": total_jobs,
            "by_category": categories,
            "umg_share_of_cost": (
                round(umg_cost / total_cost, 4) if total_cost else None
            ),
            "umg_share_of_jobs": (
                round(sum(s["jobs"] for s in songs) / total_jobs, 4)
                if total_jobs else None
            ),
            # Proporción POR PROVEEDOR. Un solo share agregado repartía mal
            # las facturas directas cuando UMG y el resto usan GCP, OpenAI y
            # Replicate en proporciones distintas.
            "umg_share_by_source": {
                src: round(umg_by_source.get(src, 0.0) / tot, 4)
                for src, tot in sorted(total_by_source.items()) if tot
            },
            "cost_by_source": {
                src: round(tot, 4)
                for src, tot in sorted(total_by_source.items())
            },
            "umg_cost_by_source": {
                src: round(c, 4)
                for src, c in sorted(umg_by_source.items())
            },
        },
        "id_collisions": id_collisions,
    }


# ---------------------------------------------------------------------------
# Level 2 — total cost to serve UMG, including infrastructure
# ---------------------------------------------------------------------------

# Providers whose spend is per-call and therefore already attributed job by
# job in level 1. Everything else (compute, storage, subscriptions) is
# shared and has to be prorated.
DIRECT_INVOICE_SOURCES = frozenset({"gcp", "openai", "replicate"})


def split_gcp_invoice(amount: float, breakdown: list[dict] | None) -> tuple[float, float]:
    """Return ``(direct_ai, shared_infra)`` from a GCP invoice snapshot.

    The billing export groups Vertex AI, Cloud Storage and networking under
    the same provider. Only Vertex AI has per-job provenance; treating the
    whole provider total as direct assigns storage/network spend using the AI
    call mix instead of the selected shared-infrastructure basis.

    Old/manual snapshots may not carry a breakdown. Preserve their legacy
    behaviour rather than silently moving a known invoice to a different
    bucket; every snapshot produced by ``fetch_gcp`` includes service+SKU.
    """
    if not breakdown:
        return float(amount), 0.0
    direct_ai = sum(
        float(row.get("cost") or 0.0)
        for row in breakdown
        if "vertex ai" in str(row.get("service") or "").lower()
    )
    # Use the snapshot amount as the source of truth. Breakdown entries are
    # rounded to four decimals, so subtraction keeps the two buckets adding
    # back to the invoiced total exactly.
    return direct_ai, float(amount) - direct_ai


def add_total_cost(attribution: dict, invoices: dict[str, float],
                   revenue_usd: float | None = None,
                   basis: str = "cost",
                   invoice_breakdowns: dict[str, list[dict]] | None = None) -> dict:
    """Level 2: direct UMG cost + UMG's share of shared infrastructure.

    `invoices` maps source -> USD for the period (the real bill, e.g.
    ``{"gcp": 199.53, "railway": 126.02, "r2": 18.84, "fixed": 24.0}``).

    Shared infra is prorated by `basis`:
      * ``"cost"``  — UMG's share of measured AI spend (default; tracks
        actual resource use, since a job that burns more Veo also renders
        longer)
      * ``"jobs"``  — UMG's share of job count (simpler, defensible in a
        negotiation because it does not depend on our own rate table)

    Both shares are always reported so the choice is visible rather than
    buried in a single number.

    The invoiced direct total replaces the modeled one when available: the
    model is calibrated but the invoice is the truth, and mixing a modeled
    numerator with an invoiced denominator produces a number that is
    neither.
    """
    share_by_cost = attribution["business"]["umg_share_of_cost"] or 0.0
    share_by_jobs = attribution["business"]["umg_share_of_jobs"] or 0.0
    shared_share = share_by_cost if basis == "cost" else share_by_jobs

    invoice_breakdowns = invoice_breakdowns or {}
    direct_amounts: dict[str, float] = {}
    shared_amounts: dict[str, float] = {}
    for source, raw_amount in invoices.items():
        amount = float(raw_amount)
        if source == "gcp":
            direct_ai, shared_infra = split_gcp_invoice(
                amount, invoice_breakdowns.get(source))
            direct_amounts[source] = direct_ai
            if shared_infra:
                shared_amounts["gcp_infrastructure"] = shared_infra
        elif source in DIRECT_INVOICE_SOURCES:
            direct_amounts[source] = amount
        else:
            shared_amounts[source] = amount
    direct_invoiced = sum(direct_amounts.values())
    shared_invoiced = sum(shared_amounts.values())

    # Direct providers are ALWAYS split by cost share, never by `basis`.
    # Their spend is per-call and level 1 already attributed it call by
    # call; a job-count share would price one Veo-heavy delivery the same
    # as one whisper-only smoke test, a ~2.7x error on the largest line.
    # `basis` only governs the shared infrastructure, where no per-job
    # measurement exists.
    #
    # And each direct invoice gets ITS OWN usage share, not the blended one.
    # UMG and internal work use GCP, OpenAI and Replicate in very different
    # proportions: Replicate-heavy internal work moved the share applied to
    # the GCP invoice even when UMG's GCP usage had not changed, and the
    # error surfaced as UMG cost and margin. Falls back to the blended share
    # for a source with no measured usage (nothing better exists).
    share_by_source = (attribution["business"].get("umg_share_by_source")
                       or {})
    umg_direct = 0.0
    direct_detail: dict[str, float] = {}
    for src, amount in direct_amounts.items():
        s = share_by_source.get(src)
        if s is None:
            s = share_by_cost
        attributed = amount * s
        direct_detail[src] = round(attributed, 2)
        umg_direct += attributed
    umg_shared = shared_invoiced * shared_share
    umg_total = umg_direct + umg_shared
    songs = attribution["umg"]["songs"]

    result = {
        "basis": basis,
        "share_used_for_shared_infra": round(shared_share, 4),
        "share_used_for_direct_ai": round(share_by_cost, 4),
        "share_by_cost": round(share_by_cost, 4),
        "share_by_jobs": round(share_by_jobs, 4),
        "invoices_total": round(direct_invoiced + shared_invoiced, 2),
        "invoiced_direct_sources": round(direct_invoiced, 2),
        "invoiced_shared_sources": round(shared_invoiced, 2),
        "invoiced_shared_cost_by_source": {
            k: round(v, 2) for k, v in sorted(shared_amounts.items())
        },
        "umg_direct_cost": round(umg_direct, 2),
        "umg_direct_cost_by_source": direct_detail,
        "share_by_source": {k: round(v, 4) for k, v in
                            sorted(share_by_source.items())},
        "umg_shared_cost": round(umg_shared, 2),
        "umg_total_cost": round(umg_total, 2),
        "songs": songs,
        "total_cost_per_song": round(umg_total / songs, 4) if songs else None,
        # The modeled figure from level 1, kept alongside so drift between
        # the rate table and the invoice stays visible.
        "modeled_direct_cost": attribution["umg"]["direct_cost"],
    }
    if revenue_usd is not None:
        result["revenue_usd"] = revenue_usd
        result["gross_profit"] = round(revenue_usd - umg_total, 2)
        result["margin_pct"] = (
            round((revenue_usd - umg_total) / revenue_usd, 4)
            if revenue_usd else None
        )
        result["revenue_per_song"] = (
            round(revenue_usd / songs, 4) if songs else None
        )
    attribution["umg_total"] = result
    return attribution
