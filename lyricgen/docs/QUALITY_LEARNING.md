# Aprendizaje por correcciones

Este subsistema compara la transcripción inicial con la revisión aprobada y
persiste únicamente estadísticas, hashes HMAC y features acotadas. Las letras
y el audio siguen viviendo en sus stores operativos autorizados; no se copian a
las tablas analíticas ni se devuelven por las APIs de aprendizaje.

## Garantías

- La versión `transcription` se crea en la misma transacción que persiste los
  segmentos y recibe un linaje hash-only de audio, release, configuración,
  timing y evidencia.
- Una aprobación se analiza en `transcription_quality`; una caída de esa cola
  nunca bloquea la aprobación del operador.
- Una edición o pedido posterior de letra/sincronización invalida la señal.
- La invalidación incrementa un epoch bloqueado en `jobs`; un análisis RQ
  anterior queda obsoleto aunque todavía no hubiese creado una observación.
- `observed` madura a `trusted` después de 14 días sin una revisión posterior.
- Los patrones globales exigen 10 canciones, 3 tenants, 3 artistas y asociación
  con IC 95%; reuploads del mismo audio se cuentan una sola vez.
- Validar ejecuta solamente un benchmark/ablation firmado sin render ni Veo.
- Aprobar crea `ready_for_implementation`; no cambia configuración ni letras.
- El modelo interpretable requiere 500 señales trusted y 100 positivos por
  categoría, comienza en shadow y sólo sugiere rutas de análisis.
- Los minutos usados por minería, SLA y costo provienen únicamente de intervalos
  contiguos entre heartbeats timestampeados por el servidor. El valor declarado
  por el navegador se conserva sólo como telemetría y nunca alimenta gates.

## Despliegue seguro

1. Aplicar la migración expand-only con todos los flags apagados.
2. Verificar que `quality-worker` tenga una réplica, concurrencia uno y la cola
   exclusiva `transcription_quality`. CPU y memoria se limitan en Railway para
   ese servicio a 2 vCPU y 4 GiB. Railway configura estos límites en Deploy →
   Replica Limits; el startup `require_quality_worker_resources.py` lee el
   cgroup y rechaza el deploy si faltan o exceden esos máximos.
3. Ejecutar `python backend/scripts/quality_learning_backfill.py` para contar el
   backfill. Sólo usar `--apply` tras revisar el dry-run; los casos históricos
   quedan `legacy_unverified` y nunca entrenan.
4. Definir `QUALITY_LEARNING_HMAC_KEY` y habilitar captura en staging.
5. Auditar durante al menos 14 días que observaciones/logs/APIs no contienen
   texto ni rutas de audio. Luego habilitar minería.
6. Entrenar offline con `train_quality_learning_model.py`, guardar el artefacto
   firmado en un path inmutable y configurar únicamente la clave pública en el
   worker. Habilitar shadow después de validar calibración y holdout.
7. No habilitar propuestas/ablations hasta reunir el gold adjudicado, ejecutar
   el benchmark baseline/candidato/ROTOR y verificar los gates p50/p90, costo,
   WER e integridad temporal. El código no sustituye esa evidencia operativa.

Kill switches independientes:

- `QUALITY_LEARNING_CAPTURE_ENABLED`
- `QUALITY_LEARNING_MINING_ENABLED`
- `QUALITY_LEARNING_MODEL_SHADOW_ENABLED`
- `QUALITY_LEARNING_ABLATIONS_ENABLED`
- `QUALITY_LEARNING_PROPOSALS_ENABLED`
- La validación de propuestas requiere además ambos switches anteriores y un reporte explícito en
  `QUALITY_LEARNING_BENCHMARK_REPORT_PATH`; sin él falla cerrado.

`QUALITY_LEARNING_HMAC_KEY_ID` es obligatorio y se rota junto con la clave. Los
identificadores se normalizan con Unicode NFKC, case-fold y whitespace canónico;
la minería nunca mezcla generaciones de HMAC.
Los deltas de corpus guardan ese `key_id`. Para seguir verificándolos después de
una rotación, conservar las claves anteriores en
`QUALITY_LEARNING_HMAC_KEYRING_JSON` como un objeto JSON `key_id → secreto`;
esta keyring se usa sólo para verificación privada y nunca se registra ni exporta.

## Operación y privacidad

El panel super-admin se encuentra en Insights → Aprendizaje. Expone únicamente
agregados, patrones k-anónimos, ablations y artefactos firmados. Las mutaciones
requieren motivo, versión optimista e idempotency key y quedan en `AuditLog`.

Las métricas objetivo continúan siendo corrección p50 menor a 5 minutos y p90
menor a 10 minutos, junto con los gates v5 de WER, timing, integridad temporal y
costo neto favorable.
