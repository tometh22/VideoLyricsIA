#!/usr/bin/env python3
"""Cost attribution report: what a UMG song really costs, across both envs.

Context
-------
GenLy's managed production for Universal runs in STAGING under team accounts
(`tomas@epical.digital`, `agus77`, `default`, `omg`), while Universal's own
self-service work runs in PRODUCTION under `universal_*` tenants. The
deliveries portal (`umg.genly.pro`) lives in the production database but its
rows point at STAGING job ids.

On top of that, staging and production share one GCP project, one R2 bucket
and one Railway project — so no invoice can be split by environment. The only
way to know what UMG actually costs is to attribute from both databases and
prorate the shared bill.

This script does that and prints three levels:

  1. DIRECT   — AI spend on the songs UMG asked for, per song
  2. TOTAL    — level 1 plus UMG's prorated share of shared infrastructure
  3. BUSINESS — every dollar, split by what it was for (client work, CI, R&D)

READ-ONLY. Issues SELECTs and nothing else.

Usage
-----
    # Both environments (the only way the numbers are right)
    export DATABASE_URL_PROD='postgresql://...'      # prod
    export DATABASE_URL_STAGING='postgresql://...'   # staging
    python scripts/umg_cost_report.py --period 2026-07 \
        --invoices '{"gcp":199.53,"railway":126.02,"r2":18.84,"fixed":44,
                     "openai":4.18,"replicate":6.10}' \
        --revenue 2000

    # JSON instead of markdown
    python scripts/umg_cost_report.py --period 2026-07 --json

Without --invoices the report still prints levels 1 and 3 (both come from the
databases); level 2 is skipped, because prorating a bill we were not given
would mean inventing it.
"""

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from sqlalchemy import create_engine                     # noqa: E402
from sqlalchemy.orm import sessionmaker                  # noqa: E402

import cost_attribution as ca                            # noqa: E402


def _session(url: str):
    """Read-only-by-convention session. `future=True` keeps 2.0 semantics."""
    engine = create_engine(url, pool_pre_ping=True, future=True)
    return sessionmaker(bind=engine, expire_on_commit=False)()


def _fmt_usd(v) -> str:
    return "—" if v is None else f"${v:,.2f}"


def render_markdown(a: dict, top_n: int) -> str:
    out: list[str] = []
    w = out.append

    period = a["period"] or "todo el histórico"
    w(f"# Costo real por canción — {period}\n")
    w(f"Entornos analizados: **{', '.join(a['environments'])}**\n")

    p = a["portal"]
    w(f"Portal `umg.genly.pro`: **{p['delivery_rows']} entregas vigentes** → "
      f"**{p['distinct_songs']} canciones distintas**\n")
    if p["songs_produced_outside_period"]:
        w(f"> ℹ️ {len(p['songs_produced_outside_period'])} canción(es) del "
          f"portal se produjeron **fuera de este período** (el portal se "
          f"carga con retraso). Su costo está en el mes en que corrieron.\n")
    if p["songs_with_deleted_jobs"]:
        w(f"> ⚠️ {len(p['songs_with_deleted_jobs'])} canción(es) del portal "
          f"**sin job en ninguna base** (job borrado → provenance en "
          f"cascada). Se gastó plata que ya no se puede medir; se reporta, "
          f"no se asume en $0.\n")
    if a["id_collisions"]:
        w(f"> ⚠️ {len(a['id_collisions'])} job_id repetido entre entornos — "
          f"revisar antes de confiar en el total.\n")

    # ---------------- Level 1 ----------------
    u = a["umg"]
    w("\n## 1 · Costo DIRECTO de IA por canción de UMG\n")
    w("| | |\n|---|---|")
    w(f"| **Canciones ENTREGADAS** | **{u['songs']}** |")
    w(f"| Canciones tocadas sin entregar | {u['songs_touched_not_delivered']} "
      f"({_fmt_usd(u['cost_of_undelivered_songs'])} que igual se pagó) |")
    w(f"| Jobs asociados | {u['jobs']} ({u['jobs_per_song']} por entregada) |")
    w(f"| Costo IA directo | **{_fmt_usd(u['direct_cost'])}** |")
    w(f"| **Costo directo por canción entregada** | **{_fmt_usd(u['direct_cost_per_song'])}** |")
    w("\n> El divisor son las canciones **entregadas**. Las que se tocaron y "
      "no salieron consumieron plata igual, así que suman al numerador pero "
      "no al denominador — meterlas abajo abarataría el costo de entregar.")

    songs = u["by_song"]
    if songs:
        w(f"\n### Las {min(top_n, len(songs))} canciones más caras\n")
        w("| Canción | Entregada | Jobs | Llamadas | Costo | Entorno |")
        w("|---|---|---|---|---|---|")
        for s in songs[:top_n]:
            name = f"{s['artist']} — {s['title']}".strip(" —") or "(sin título)"
            w(f"| {name[:48]} | {'sí' if s['delivered'] else '**no**'} | "
              f"{s['jobs']} | {s['billable_calls']} | {_fmt_usd(s['cost'])} | "
              f"{'/'.join(s['envs'])} |")

        # Statistics over DELIVERED songs only: mixing in abandoned ones
        # drags the median down, and they sit at the cheap end by
        # construction (they stopped before rendering).
        dcosts = [s["cost"] for s in songs if s["delivered"]]
        if dcosts:
            ordered = sorted(dcosts)
            n = len(ordered)
            mid = (ordered[n // 2] if n % 2
                   else (ordered[n // 2 - 1] + ordered[n // 2]) / 2)
            w(f"\nSobre las {n} entregadas — mediana **{_fmt_usd(mid)}** · "
              f"promedio **{_fmt_usd(sum(dcosts) / n)}** · "
              f"máx **{_fmt_usd(max(dcosts))}** · mín **{_fmt_usd(min(dcosts))}**")
            w("\n> Para cotizar usá el **promedio**, no la mediana: la plata "
              "que sale es la suma, y la cola de canciones re-generadas "
              "muchas veces es real. La mediana sirve para ver el caso "
              "típico, no para fijar precio.")

    # ---------------- Level 2 ----------------
    t = a.get("umg_total")
    if t:
        w("\n## 2 · Costo TOTAL de servir a UMG (con infraestructura)\n")
        w(f"IA directa repartida por costo (**{t['share_used_for_direct_ai']:.1%}**); "
          f"infra compartida por **{t['basis']}** "
          f"(**{t['share_used_for_shared_infra']:.1%}**).")
        w(f"(share por costo: {t['share_by_cost']:.1%} · "
          f"por jobs: {t['share_by_jobs']:.1%})\n")
        w("| Concepto | Monto |")
        w("|---|---|")
        w(f"| Facturas del período | {_fmt_usd(t['invoices_total'])} |")
        w(f"| — proveedores directos (IA) | {_fmt_usd(t['invoiced_direct_sources'])} |")
        w(f"| — infra compartida | {_fmt_usd(t['invoiced_shared_sources'])} |")
        w(f"| **Parte de UMG — directa** | **{_fmt_usd(t['umg_direct_cost'])}** |")
        w(f"| **Parte de UMG — infra** | **{_fmt_usd(t['umg_shared_cost'])}** |")
        w(f"| **COSTO TOTAL UMG** | **{_fmt_usd(t['umg_total_cost'])}** |")
        w(f"| **Costo total por canción** | **{_fmt_usd(t['total_cost_per_song'])}** |")
        if "revenue_usd" in t:
            w("\n### Contra lo que pagan\n")
            w("| | |\n|---|---|")
            w(f"| Ingreso | {_fmt_usd(t['revenue_usd'])} |")
            w(f"| Canciones entregadas | {t['songs']} |")
            w(f"| **Precio real por canción** | **{_fmt_usd(t['revenue_per_song'])}** |")
            w(f"| Costo total | {_fmt_usd(t['umg_total_cost'])} |")
            w(f"| **Ganancia neta** | **{_fmt_usd(t['gross_profit'])}** |")
            if t.get("margin_pct") is not None:
                w(f"| **Margen** | **{t['margin_pct']:.1%}** |")
        w(f"\n> Modelado desde la tabla de tarifas: "
          f"{_fmt_usd(t['modeled_direct_cost'])}. Si se aleja mucho de la "
          f"parte directa facturada, `COST_PER_CALL` quedó vieja.")

    # ---------------- Level 3 ----------------
    b = a["business"]
    w("\n## 3 · A qué se fue cada dólar del negocio\n")
    w(f"Costo directo de IA, todos los entornos: **{_fmt_usd(b['total_direct_cost'])}** "
      f"en {b['total_jobs']} jobs\n")
    w("| Categoría | Jobs | Canciones | Llamadas | Costo | % |")
    w("|---|---|---|---|---|---|")
    labels = {
        ca.CAT_UMG: "Producción UMG",
        ca.CAT_OTHER_CLIENT: "Otros clientes",
        ca.CAT_CI: "Automatización / CI",
        ca.CAT_RND: "I+D interno",
    }
    total = b["total_direct_cost"] or 1
    for c in b["by_category"]:
        w(f"| {labels.get(c['category'], c['category'])} | {c['jobs']} | "
          f"{c['songs']} | {c['billable_calls']} | {_fmt_usd(c['cost'])} | "
          f"{c['cost'] / total:.1%} |")
    w(f"\nUMG es el **{(b['umg_share_of_cost'] or 0):.1%}** del gasto de IA y el "
      f"**{(b['umg_share_of_jobs'] or 0):.1%}** de los jobs.")
    w("\n> Lo que NO es producción vendible (CI + I+D) es costo de operar el "
      "negocio, no costo de bienes vendidos. No cargarlo al precio del cliente.")

    return "\n".join(out)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--period", default=None,
                    help="YYYY-MM; omitir = todo el histórico")
    ap.add_argument("--invoices", default=None,
                    help='JSON {"gcp":199.53,"railway":126.02,...} — habilita el nivel 2')
    ap.add_argument("--revenue", type=float, default=None,
                    help="ingreso del período para calcular margen (ej. 2000)")
    ap.add_argument("--basis", choices=("cost", "jobs"), default="cost",
                    help="base de prorrateo de la infra compartida")
    ap.add_argument("--top", type=int, default=15,
                    help="cuántas canciones listar en el detalle")
    ap.add_argument("--json", action="store_true", help="salida JSON cruda")
    args = ap.parse_args()

    prod_url = os.environ.get("DATABASE_URL_PROD", "").strip()
    staging_url = os.environ.get("DATABASE_URL_STAGING", "").strip()
    if not prod_url:
        print("ERROR: falta DATABASE_URL_PROD (el portal de entregables vive "
              "en producción).", file=sys.stderr)
        return 2
    if not staging_url:
        print("AVISO: sin DATABASE_URL_STAGING. La producción gestionada de "
              "UMG corre en STAGING, así que el costo va a quedar MUY "
              "subestimado.", file=sys.stderr)

    if args.period:
        try:
            ca.period_bounds(args.period)
        except ValueError as e:
            print(f"ERROR: {e}", file=sys.stderr)
            return 2

    db_prod = _session(prod_url)
    sessions = {"prod": db_prod}
    if staging_url:
        sessions["staging"] = _session(staging_url)

    try:
        portal = ca.collect_portal_songs(db_prod)
        # La calibración se lee UNA vez (de prod) y se aplica a los dos
        # entornos: staging no tiene el snapshot y caería a precio de lista,
        # valuando el mismo Veo a dos tarifas dentro de un solo informe.
        rates = {}
        if args.period:
            from rate_calibration import load_applied_rates
            rates = load_applied_rates(db_prod, args.period)
        jobs_by_env = {
            env: ca.collect_jobs(db, env, period=args.period, rates=rates)
            for env, db in sessions.items()
        }
        # Unfiltered song identities, so a portal song whose jobs ran in
        # another month isn't misreported as deleted.
        all_time_keys: set[str] = set()
        if args.period:
            for db in sessions.values():
                all_time_keys |= ca.collect_song_keys(db)
        attribution = ca.build_attribution(
            jobs_by_env, portal, period=args.period,
            all_time_song_keys=all_time_keys if args.period else None,
        )

        if args.invoices:
            try:
                invoices = json.loads(args.invoices)
            except json.JSONDecodeError as e:
                print(f"ERROR: --invoices no es JSON válido: {e}",
                      file=sys.stderr)
                return 2
            ca.add_total_cost(attribution, invoices,
                              revenue_usd=args.revenue, basis=args.basis)

        if args.json:
            print(json.dumps(attribution, indent=2, ensure_ascii=False,
                             default=str))
        else:
            print(render_markdown(attribution, args.top))
        return 0
    finally:
        for db in sessions.values():
            db.close()


if __name__ == "__main__":
    raise SystemExit(main())
