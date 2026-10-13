# Primer paquete de reducción de costos

Estado: código preparado; configuración de Railway pendiente de aplicación.
Base de análisis: septiembre de 2026, fuentes disponibles USD 557,98; Railway
estimado 377,76. No sumar escenarios superpuestos ni presentar objetivos como
ahorro ya capturado.

## Código del primer paquete

- El ZIP de entregables se construye en disco y se entrega por bloques. Sus
  temporales permanecen hasta terminar la respuesta y se eliminan también si
  falla el envío. El trabajo bloqueante corre en el thread pool de FastAPI.
  Los fuentes descargados de R2 se liberan al incorporarse al ZIP; los
  entregables locales originales se conservan.
- Las consultas de semáforos seleccionan en SQL el último evento de cada job
  pedido. Conservan la selección por ID entre versiones v1 y v2.
- El heartbeat del editor consulta solo la última fila de su sesión, job y
  usuario. Conserva el control de secuencia y la autorización existente.

Esto elimina cargas de memoria innecesarias. El efecto mensual debe medirse
después del despliegue; no se reducen réplicas en este paquete.
Observar espacio temporal y descargas concurrentes: durante el armado pueden
coexistir los fuentes y el ZIP, con pico próximo al doble del tamaño del bundle.
Al servir queda solamente el ZIP temporal. La caché de archivos del sistema
puede seguir consumiendo memoria; medir facturación y no solo memoria Python.

## Canary de caché, solo en staging

El diagnóstico readonly del 03/10 encontró 2.976 entradas FEATURES ocupando
1,746 GB, aproximadamente 97% de la memoria usada por Redis staging. Las claves
tienen TTL; no es una acumulación de jobs sin vencimiento. El costo calendarizado
de RAM de ese Redis fue aproximadamente USD 25,99. Los contadores históricos de
FEATURES mostraron 2,67% de aciertos; ese porcentaje no representa una ventana
controlada de carga comparable.

1. Registrar la configuración anterior del servicio `quality-worker` en
   `staging`, memoria/RSS de Redis, contadores de cache hit/miss/write y tiempos
   de análisis. Verificar trabajo activo y la estrategia de drenaje antes de
   reiniciar el worker.
2. Configurar **solo ese servicio de staging** con
   `QUALITY_CACHE_FEATURES_TTL_SECONDS=86400`. La opción ya existe en
   `quality_cache.py`; no cambia el algoritmo de análisis ni el TTL de CTC,
   N-best o boundaries. Aplicarla requiere que el proceso reciba la variable;
   editar el dashboard sin reiniciar no cambia el entorno de un proceso vivo.
3. Observar 24–48 horas con carga comparable: tiempos de análisis, CPU,
   cantidad de trabajos, errores, cache hits/misses y RAM. Las entradas previas
   mantienen sus vencimientos originales; la caída del stock puede tardar hasta
   siete días. Verificar memoria realmente facturada, no solamente bytes lógicos.
4. Revertir al valor anterior si la recomputación empeora tiempos o el costo
   neto. Si antes no existía override, eliminarlo y volver al default de siete
   días; no inventar un valor anterior. El rollback afecta nuevas escrituras,
   no extiende el TTL de claves ya creadas con 24 horas.

No ejecutar FLUSH ni activar expulsión indiscriminada: Redis también sostiene
colas y locks. No colocar el TTL corto en el TOML compartido con producción.
La preparación de este paquete no modifica ninguna variable de Railway.

## Segunda etapa

- Si la retención corta no alcanza, mover únicamente los blobs FEATURES a R2
  conservando digest, checksum, expiración y tratamiento de fallos. Comparar
  latencia de lectura y costo total. Objetivo exploratorio: USD 20–25/mes de RAM
  evitada, restando operaciones, uploads y recomputación; no sumarlo nuevamente
  a otro escenario de reducción global de RAM.
- Automatizar capacidad batch por demanda, conservando throughput en campañas.
  El override regional de BatchShortWorker mantiene ocho réplicas pese al TOML
  con dos. Antes de apagar consumidores, revisar started, scheduled, retries,
  outbox y requisitos de `/health/ready`, además de profundidad de cola.
- Añadir reutilización de ProRes por fingerprint y coordinación entre réplicas.
  Staging ya tiene prewarm apagado; esa configuración no es ahorro nuevo.
- Evaluar frontend estático en Cloudflare Pages antes de cancelar Vercel, y
  dimensionar renders en Hetzner contra Railway ya optimizado.

## Validación y publicación

Las pruebas enfocadas de `tests/test_cost_reduction.py` usan SQLAlchemy/SQLite y
respuesta ASGI real con datos temporales. Cubren elección del último semáforo,
aislamiento/secuencia del heartbeat, envío de ZIP por bloques y limpieza normal,
en desconexión y en error antes de responder. También verifican liberación de
fuentes R2 y conservación de los entregables locales. Se ejecutan sin importar
el renderer ni inicializar una DB de aplicación:

```sh
python3 -m pytest --noconftest lyricgen/backend/tests/test_cost_reduction.py -q
make check-backend
```

Estas pruebas no reemplazan CI ni una comparación de rendimiento en staging.
Para publicar una release, actualizar VERSION, package.json y CHANGELOG juntos
y cumplir CI en checkout limpio y el circuito de promoción del repositorio.
