# Campañas masivas

## Activación

La funcionalidad queda apagada por defecto. En API y workers configurar:

```text
BATCH_CAMPAIGN_ENABLED=1
BATCH_ART_TRACK_ENABLED=1  # kill-switch independiente; unset sigue al anterior
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

## Campañas paralelas de Art Track

Las campañas existentes conservan `kind=lyric_video` y su recorrido de
transcripción/revisión de letras. Para el flujo paralelo crear una campaña
con `kind=art_track` y `destination_portal=argentina|chile`. Desde su tarjeta
se seleccionan los WAV/MP3 y JPG/PNG juntos (o se usa el cargador local con
`--covers`). El backend registra assets deduplicados, asocia por ruta/código/
nombre o cover de carpeta y deja los conflictos como `ambiguous`/`missing`.

Después de confirmar las asociaciones, **Confirmar asociados y generar** crea
jobs batch mediante outbox y llama al renderer Art Track existente: no ejecuta
ASR, Demucs, letras ni fondos IA. La cola está limitada por
`BATCH_RENDER_WINDOW` y `BATCH_FINAL_REVIEW_LIMIT`; cada resultado queda en
revisión humana aunque `REQUIRE_REVIEW` esté apagado. Sólo las aprobaciones
vigentes entran en la previsualización y operación durable de envío. El estado
de la operación se consulta en `/batch/art-track-delivery-operations/{id}` y
se reanuda desde el worker tras una caída.

El destino AR/CL se guarda en la operación. Cuando el contrato `Delivery`
incluye `portal_id`, el publisher lo usa para mantener entregas independientes
en `umg.genly.pro` y `umgchile.genly.pro`; hasta integrar esa migración no se
debe probar con material real de clientes.
