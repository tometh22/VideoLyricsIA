# UMG golden-set harness

Este directorio mide variantes locales sin modificar staging, producción ni
las bases de datos. La extracción de base de datos está limitada por código a
sentencias `SELECT`; los audios se descargan de R2 y se verifican por SHA-256.

## Estado certificado (`baseline-2026-08-29`)

- 65 entregas aprobadas: 62 originadas en staging, 3 en producción.
- 23 crudos `exact`, 18 `reconstructed`, 16 `estimated`, 8 `none`.
- Las métricas históricas se publican en dos cohortes: 41
  `exact+reconstructed` y 57 incluyendo `estimated`.
- Las 8 sin crudo permanecen en el gold con `has_raw: false`; su baseline local
  Whisper large-v3-turbo lleva `historical: false`, se reporta por separado y
  nunca entra a las métricas históricas 41/57.
- ISRC es `null`; duración sale de `ffprobe`.
- Idioma: Whisper sobre un stem HTDemucs local + confirmación de Qwen 3.5
  local + desempate Lingua. Los 65 tienen resultado, sin enviar audio ni letra
  a un proveedor externo.
- Los cinco controles adversariales pasan contra la API read-only exacta que
  alimenta `umg.genly.pro`; el baseline está ligado a hashes del manifiesto,
  audios y snapshots aprobados.

## Comandos

```bash
python3 -m pip install -r eval/requirements.txt
make eval-test
make eval-extract
make eval-verify-portal PORTAL_PAYLOAD=.context/portal-items.json VERIFICATION=.context/portal-verification.json
make eval-finalize VERIFICATION=.context/portal-verification.json
make eval-language-id
make eval VARIANT=prod_raw
make eval-autopsy
make eval-freeze AUTOPSY_41=.context/autopsy-41.json AUTOPSY_57=.context/autopsy-57.json
make eval-t4-learned
make eval-error-predictor
make eval-lora-prep
make eval-nonhistorical
make eval VARIANT=baseline HYPOTHESIS_ROOT=/ruta/a/hipotesis
```

`make eval-extract` exige las credenciales por variables de entorno. No acepta
secretos como argumentos ni los escribe al dataset. Deja el resultado en
`eval/golden.partial`. Solo `make eval-finalize` promueve a `eval/golden`, y
rechaza hacerlo hasta que los cinco controles del portal estén confirmados.

## Contrato de cierre de extracción

1. `raw_quality` y `job_origin` válidos en los 65 `meta.json`.
2. SHA-256 calculado y verificado para cada audio; duración derivada.
3. Todas las versiones completas y eventos `lyrics.segments_diff` exportados.
4. Conteo explícito de eventos legacy que aún conservan `prev_text/new_text`.
5. Cinco canciones de la muestra determinista verificadas contra el feed vivo
   del portal antes de marcar la extracción como cerrada.

`eval-lora-prep` genera splits por canción y leave-artist-out, pero deja la
ejecución bloqueada: la política publicada afirma que no se entrena con datos
del cliente y que el audio no sale de la infraestructura. Esa autorización se
resuelve antes de cualquier upload a RunPod.

La taxonomía se puede ejecutar enteramente con Ollama. El subcomando externo
`eval.classify_errors submit` está bloqueado además por código y exige
`ALLOW_EXTERNAL_CLIENT_TEXT_BATCH=1`, que solo se habilita después de una
autorización explícita de egreso de texto del cliente.

Los outputs pesados (`golden`, `runs`, `cache`, `hypotheses`) están ignorados por Git. Cada
corrida guarda matriz de alineación, matches, métricas y errores auditables.
Una coincidencia solo textual sin solapamiento temporal se veta cuando está a
más de 10 segundos: evita confundir ocurrencias distintas del mismo estribillo
y contaminar las métricas de timing.
