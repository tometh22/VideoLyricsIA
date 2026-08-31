"""Chequeo semanal: producción sigue guardando el crudo exacto?

POR QUE IMPORTA
---------------
La cohorte de metricas es `raw_quality == 'exact'`, y hoy son solo 23 de 65.
La UNICA via para que crezca es que cada job nuevo persista su
`editor_documents.original_segments` — el snapshot inmutable del motor, que
`transcription_worker` congela ANTES de que el editor pueda autoguardar.

Medido el 2026-08-30: produccion esta al 100% desde el 25-ago (27/27
consecutivos). Antes de esa fecha la cobertura era erratica (0-67%), que es
exactamente por que 42 de las 65 del gold set no tienen crudo confiable.

Si esto se rompe, el sintoma es invisible: los jobs se aprueban igual, nadie
ve un error, y meses despues descubris que la cohorte limpia no crecio.

Uso:  python -m eval.check_raw_coverage [--days 7] [--min-pct 100]
Env:  DATABASE_URL (staging) y DELIVERIES_DATABASE_URL (produccion)
Sale con codigo 1 si la cobertura cae por debajo del umbral.
"""
from __future__ import annotations

import argparse
import os
import sys

# Tenants que NO cuentan: bots de smoke test que nunca llegan al editor.
TENANTS_EXCLUIDOS = ("golden_render_bot", "default")
# Estados que todavia no pasaron por transcripcion.
ESTADOS_EXCLUIDOS = ("awaiting_upload", "transcription_failed", "rejected")

# Antes de esta fecha la persistencia del crudo era erratica (0-67%): el
# `get_or_create_document` al persistir segments no estaba en el camino del
# worker. Medir jobs previos solo produce falsos positivos — ya sabemos que
# no tienen crudo y no se puede hacer nada al respecto.
DESDE = "2026-08-25"

SQL = """
SELECT count(*) AS jobs,
       count(*) FILTER (WHERE ed.original_segments IS NOT NULL) AS con_crudo
FROM jobs j
LEFT JOIN editor_documents ed ON ed.job_id = j.job_id
WHERE j.segments_json IS NOT NULL
  AND j.created_at > greatest(now() - make_interval(days => %s), %s::timestamptz)
  AND j.tenant_id <> ALL(%s)
  AND j.status <> ALL(%s)
"""


def revisar(url: str, etiqueta: str, dias: int, min_pct: float) -> tuple[bool, str]:
    import psycopg2
    conn = psycopg2.connect(url)
    conn.set_session(readonly=True)
    try:
        cur = conn.cursor()
        cur.execute(SQL, (dias, DESDE, list(TENANTS_EXCLUIDOS),
                          list(ESTADOS_EXCLUIDOS)))
        jobs, con_crudo = cur.fetchone()
    finally:
        conn.close()
    if not jobs:
        return True, f"{etiqueta}: sin jobs en {dias}d (nada que verificar)"
    pct = 100.0 * con_crudo / jobs
    ok = pct >= min_pct
    marca = "OK " if ok else "FALLA"
    return ok, f"{marca} {etiqueta}: {con_crudo}/{jobs} con crudo exacto ({pct:.0f}%)"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--min-pct", type=float, default=100.0)
    args = ap.parse_args(argv)

    objetivos = [("produccion", os.environ.get("DELIVERIES_DATABASE_URL")),
                 ("staging", os.environ.get("DATABASE_URL"))]
    faltan = [n for n, u in objetivos if not u]
    if faltan:
        print(f"ERROR: faltan credenciales para: {', '.join(faltan)}", file=sys.stderr)
        return 2

    print(f"Cobertura de crudo exacto — ultimos {args.days} dias, "
          f"desde {DESDE} (umbral {args.min_pct:.0f}%)\n")
    todo_ok = True
    for etiqueta, url in objetivos:
        ok, linea = revisar(url, etiqueta, args.days, args.min_pct)
        print("  " + linea)
        todo_ok = todo_ok and ok

    if not todo_ok:
        print("\n  Un job sin `original_segments` NUNCA va a poder entrar a la\n"
              "  cohorte limpia: el crudo no se puede reconstruir despues (el\n"
              "  rewind de diffs quedo refutado el 2026-08-30). Revisar que\n"
              "  `transcription_worker` siga llamando a `get_or_create_document`\n"
              "  al persistir los segments.")
        return 1
    print("\n  La cohorte limpia puede seguir creciendo.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
