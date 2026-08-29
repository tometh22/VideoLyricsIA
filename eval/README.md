# UMG golden-set harness

Este directorio mide variantes locales sin modificar staging, producción ni
las bases de datos. La extracción de base de datos está limitada por código a
sentencias `SELECT`; los audios se descargan de R2 y se verifican por SHA-256.

## Estado del corpus confirmado

- 65 entregas aprobadas: 62 originadas en staging, 3 en producción.
- 23 crudos `exact`, 18 `reconstructed`, 16 `estimated`, 8 `none`.
- Las métricas históricas se publican en dos cohortes: 41
  `exact+reconstructed` y 57 incluyendo `estimated`.
- Las 8 sin crudo permanecen en el gold con `has_raw: false`; su baseline local
  futuro llevará `historical: false`.
- ISRC es `null`; duración sale de `ffprobe`; idioma se detecta desde la letra
  aprobada y queda marcado `derived: true`.

## Comandos

```bash
python3 -m pip install -r eval/requirements.txt
make eval-test
make eval-extract
make eval-finalize VERIFICATION=.context/portal-verification.json
make eval VARIANT=prod_raw
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
5. Cinco canciones de la muestra determinista comparadas manualmente con el
   portal antes de marcar la extracción como cerrada.

Los outputs pesados (`golden`, `runs`, `cache`) están ignorados por Git. Cada
corrida guarda matriz de alineación, matches, métricas y errores auditables.
Una coincidencia solo textual sin solapamiento temporal se veta cuando está a
más de 10 segundos: evita confundir ocurrencias distintas del mismo estribillo
y contaminar las métricas de timing.
