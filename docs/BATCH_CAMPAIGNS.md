# Campañas masivas

## Activación

La funcionalidad queda apagada por defecto. En API y workers configurar:

```text
BATCH_CAMPAIGN_ENABLED=1
BATCH_CAMPAIGN_SCOPES=<tenant_id o billing_group autorizado>
```

El rol `admin` puede acceder cuando el flag global está prendido. Los demás
usuarios sólo cuando su tenant o billing group está en `BATCH_CAMPAIGN_SCOPES`.

Crear dos servicios Railway adicionales usando los archivos versionados:

- `BatchShortWorker`: `railway/batch-short-worker.toml`, 2 réplicas,
  colas `campaign_control,transcription_batch`.
- `BatchWorker`: `railway/batch-worker.toml`, 2 réplicas, cola `batch_render`.

No modificar los servicios existentes: ShortWorker conserva 3 réplicas y
Worker conserva 7. Con el flag habilitado, `/health/ready` exige que los dos
pools batch estén presentes y anuncien sus colas.

Variables ajustables (los valores listados son los defaults):

```text
BATCH_CAMPAIGN_ITEM_LIMIT=1000
BATCH_TRANSCRIPTION_WINDOW=30
BATCH_LYRICS_READY_LIMIT=50
BATCH_RENDER_WINDOW=10
BATCH_FINAL_REVIEW_LIMIT=50
BATCH_RECONCILE_SECONDS=30
DEMUCS_MAX_CONCURRENT=2
DEMUCS_BATCH_MAX_CONCURRENT=1
```

## Operación

1. Crear la campaña en `/campaigns` y guardar el preset compartido.
2. Generar un código temporal en la tarjeta **Cargador local**.
3. Ejecutar el comando que muestra el panel desde la raíz del repositorio.
4. Si se corta, generar otro código y repetir el mismo comando. El manifiesto
   deduplica por SHA-256/código y multipart consulta R2 para omitir partes ya
   recibidas.
5. Corregir archivos marcados **Falta metadata** desde el panel.
6. Empezar con **Tomar siguiente**. **Generar y seguir** espera el guardado
   durable, encola exactamente una generación batch y toma otra letra.

Pausar evita nuevas promociones, claims, generaciones, ediciones y reintentos,
pero no mata workers que ya estaban ejecutándose. Cancelar es irreversible y
tampoco destruye objetos o jobs en curso.

## Canary y rollback

Antes de una campaña real, correr 5–10 canciones y luego 30. Verificar:

- `queue_depth.transcription_batch`, `batch_render` y `campaign_control`;
- counters de campaña y métricas `batch_*` en ops;
- que `transcription`, `enterprise` y `default` no aumenten por el canary;
- dos pestañas con la misma cuenta toman jobs distintos;
- una campaña con 50 letras listas no promueve otra transcripción;
- no existe un job de fondo/render antes de aprobar la letra.

Rollback: pausar campañas, poner `BATCH_CAMPAIGN_ENABLED=0` y dejar drenar las
colas batch. No hace falta revertir la migración; las columnas nuevas son
compatibles y todos los jobs históricos tienen `workload_class=interactive`.
