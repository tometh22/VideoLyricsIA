# Evaluación de correcciones aprobadas

Cada referencia queda fijada a una versión aprobada del editor y a los bytes
del audio de entrada. Un trabajo `done` con un autoguardado posterior sin
aprobar no es una referencia válida. El exportador anterior de benchmark
ahora exige que trabajo, documento y versión aprobada coincidan.

La evaluación es de desarrollo: una aprobación operativa no equivale a una
anotación acústica independiente. No es un conjunto reservado para demostrar
generalización, no habilita entrenamientos y no promueve modelos.

## Congelar la muestra

Desde la raíz del repositorio, con Railway vinculado al proyecto existente:

```sh
railway run --no-local -e staging -s Postgres -- python3.11 lyricgen/backend/scripts/snapshot_approved_evaluation.py --out .context/evaluation-new --job-id 244558ff99aa --latest 8
railway run --no-local -e staging -s api -- python3.11 lyricgen/backend/scripts/snapshot_approved_evaluation.py --out .context/evaluation-new --download-audio
```

La primera etapa usa una transacción de PostgreSQL de solo lectura. La segunda
verifica el SHA-256 del audio contra la identidad registrada. Cada ejecución
usa un directorio nuevo; los snapshots existentes no se reemplazan. Letras,
audio y respuestas de proveedores quedan en almacenamiento privado ignorado
por git, con archivos `0600` y directorios `0700`.

`baseline_output.json` contiene el resultado histórico previo a la edición
humana, no una ejecución del motor actual. La referencia es
`ground_truth.json`, proveniente de la versión aprobada que identifica
`metadata.json`. Los estados intermedios nunca se seleccionan como objetivo.

## Ejecutar el worker actual

```sh
railway run --no-local -e staging -s api -- python3.11 lyricgen/backend/scripts/run_approved_benchmark.py --dataset .context/evaluation-new --job-id 244558ff99aa
```

Se ejecuta `transcription_worker.run_transcription_job`, incluidos sus
postprocesos y control de calidad inline. Las escrituras quedan en SQLite
local. Se suprime el envío a la cola de calidad externa para no encolar
trabajos de investigación en la flota del producto. Por eso el resultado
mide **la salida inicial del worker**, antes de eventuales reparaciones
asíncronas. Las llamadas normales a proveedores pueden tener costo y usar
los cachés de stems y límites de concurrencia existentes.

El proceso solo lee el audio y metadatos de inferencia. No lee la letra
aprobada, sus tiempos ni los snapshots de correcciones. Guarda release,
huella de configuración, hashes del código y audio, contexto de ejecución y
duración. Conserva la entrada y salida del formateador para atribuir pérdidas
con evidencia. No reemplaza intentos anteriores; `--run-name NOMBRE` permite
una nueva medición explícita. Un intento fallido permanece registrado.

Los metadatos permiten `language`, `live`, `reference_required` y
`workload_class`. Sus valores por defecto son español, estudio y campaña con
referencia requerida. Es necesario revisar esos valores antes de mezclar
grabaciones en vivo u otros idiomas. La etiqueta de vivo no se certifica
automáticamente por el nombre de archivo.

## Comparar

```sh
python3.11 lyricgen/backend/scripts/evaluate_approved_corrections.py --snapshot-dir .context/evaluation-new --out .context/evaluation-new/report.json
```

Se comprueban versión aprobada, snapshot y audio antes de medir:

- Error de palabras (WER), conservando tildes y normalizando puntuación.
- Recuperación de ocurrencias de líneas mediante asociación monótona que
  contempla splits y merges. Una repetición recuperada no cuenta por varias.
- Diferencia de inicio y fin de los carteles, solo en líneas asociadas.
  Sin asociación el timing es desconocido, no error cero.

Para una referencia donde los paréntesis representen segundas voces, se
puede registrar `inline_parentheses_are_backing: true` en sus metadatos. La
evaluación separa el texto completo de la voz principal. Los tiempos del
cartel compartido no certifican el onset de cada segunda voz; ese timing no
se puntúa como si hubiese una anotación independiente.

WER es una distancia de edición contra el texto aprobado, no el porcentaje
de intervalos acústicos incorrectos. El recall de líneas es una asociación
aproximada de presentación. Los tiempos aprobados representan la elección
editorial del revisor. Hay que presentar estas métricas junto con la
cobertura de asociaciones y el estado del control de calidad.

Antes de afirmar una mejora general se necesitan más canciones, un conjunto
independiente por canción/artista y una comparación con las mismas
condiciones de inferencia. Reutilizar la misma canción para desarrollar y
evaluar una reparación no demuestra generalización.
