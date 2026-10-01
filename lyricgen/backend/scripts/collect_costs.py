#!/usr/bin/env python
"""Entrypoint del cron de costos. Corre, rellena huecos, y sale.

Por qué es un servicio con `cronSchedule` y no un thread en `api`
------------------------------------------------------------------
`railway/api.toml` corre `numReplicas = 2` con `uvicorn --workers 2`: cuatro
procesos ejecutan el `startup` de FastAPI. Los loops de fondo de `main.py`
son `sleep(N)` desde el arranque del proceso, no un horario, así que:

* "todos los días a las 06:00" es imposible — corre a boot+N y otra vez 24 h
  después, y cada deploy de Railway resetea el reloj a una hora arbitraria;
* el `advisory lock` deduplica pero retiene una conexión del pool durante
  toda la corrida, y `pool_recycle=120` mata sesiones largas: el commit
  final revienta y se pierde el trabajo;
* si el proceso que ganó el lock muere a mitad, el día se pierde sin rastro.

Un proceso que arranca, hace su trabajo y termina no tiene ninguno de esos
problemas. Y como el backfill es guiado por huecos, una corrida perdida se
repara sola en la siguiente en vez de perderse para siempre.

Gate de entorno
---------------
Staging y producción comparten proyecto de GCP, bucket de R2 y proyecto de
Railway. Si el colector corriera en los dos, **cada panel reclamaría el
gasto entero de la cuenta como propio** y además se duplicarían las queries
a BigQuery — el panel de costos generando el costo que después muestra.
Por eso corre sólo donde `ENVIRONMENT == production`, salvo override
explícito.
"""

import logging
import os
import sys

logging.basicConfig(
    level=logging.INFO,
    format='{"ts":"%(asctime)s","level":"%(levelname)s","logger":"%(name)s","msg":"%(message)s"}',
)
logger = logging.getLogger("genly.costs.cron")


def main() -> int:
    env = os.environ.get("ENVIRONMENT", "").strip().lower()
    forzado = os.environ.get("COST_COLLECTOR_FORCE", "") == "1"
    if env != "production" and not forzado:
        logger.warning(
            "[costs] ENVIRONMENT=%r no es production; no colecto. "
            "Staging comparte proyecto GCP / bucket R2 / proyecto Railway con "
            "prod, así que colectar acá haría que ambos paneles reclamen el "
            "gasto entero. COST_COLLECTOR_FORCE=1 para forzar.", env)
        return 0

    # Import tardío: si falta una env var de DB, que falle acá con un log
    # claro y no en el import del módulo.
    from database import SessionLocal
    import cost_daily_collector

    days = int(os.environ.get("COST_BACKFILL_DAYS", "35"))
    db = SessionLocal()
    try:
        out = cost_daily_collector.run_backfill(db, days=days)
    finally:
        db.close()

    logger.info("[costs] backfill: intentados=%s ok=%s error=%s sin_config=%s",
                out["attempted"], out["ok"], out["error"], out["not_configured"])
    for e in out["errors"][:20]:
        logger.error("[costs] %s %s: %s", e["day"], e["source"], e["detail"])

    # Exit code distinto de 0 sólo si TODO falló: un proveedor caído no puede
    # marcar la corrida entera como fallida, porque el resto sí se recolectó
    # y el backfill guiado por huecos va a reintentar ese solo mañana.
    if out["attempted"] and out["ok"] == 0 and out["error"] > 0:
        logger.error("[costs] ninguna fuente respondió")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
