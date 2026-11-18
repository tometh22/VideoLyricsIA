# Modelo local de Genly: piloto de canción completa

Implementado y ejecutado el 18/11/2026 sobre Apple M1 Pro, 16 GB de memoria
compartida. No se contrató infraestructura ni se utilizaron APIs pagas de
inferencia. Las consultas al catálogo fueron exclusivamente de lectura.

El piloto conecta el editor de staging con un procesador local: solicitar el
análisis completo, reconocer el audio, proponer una candidata, escucharla y
aceptar los cambios desde las sugerencias existentes. No modifica una canción
sin aceptación humana ni reemplaza el pipeline de transcripción de producción.

El modelo compuesto combina un selector propio entrenado con correcciones
históricas, Qwen3-ASR 1.7B cuantizado en MLX, comparación entre mezcla y voz
separada, alineación CTC y una política conservadora de cambios por palabra.
Los pesos de Qwen son preentrenados; no se presentan como un reconocedor creado
desde cero ni como una adaptación nuestra. Los tiempos se conservan por defecto;
las extensiones requieren evidencia de continuidad vocal y reloj verificado.
La alineación de una hipótesis no demuestra que su texto sea correcto.

## Uso en staging

Abrir una canción sin aprobar en el editor y usar **Modelo Genly · piloto →
Analizar canción completa**. El panel muestra el estado, la candidata completa
y las líneas propuestas. Escuchar cada cambio y aceptarlo desde las sugerencias
del editor. Guardar genera una nueva revisión. Si la canción cambió durante el
análisis, el resultado anterior no se puede incorporar. Los resultados parciales
mantienen sus fragmentos pendientes; no se presentan como escucha completa.

La inferencia depende de esta Mac encendida y del procesador local conectado.
El panel informa cuando está desconectado. No requiere GPU alquilada ni llamadas
a proveedores de reconocimiento. Un resultado sin cambios también queda visible.

## Ejecutor local

Requiere el Python local con PyTorch/CTC/ffmpeg y un entorno separado con
`mlx==0.32.4`, `mlx-metal==0.32.4`, `mlx-audio==0.6.6`, `transformers==5.23.0`.
El checkpoint probado es `mlx-community/Qwen3-ASR-1.7B-4bit`, revisión
`78a389c776a5483b2d0d4ea5494e11012e0d6159`. Reconocimiento y alineación corren
en procesos consecutivos para liberar memoria entre modelos.

```sh
python3 lyricgen/backend/scripts/run_genly_model_worker.py \
  --auth-file .context/local-genly/staging-auth.json \
  --selector .context/local-genly/selector/model.json \
  --model qwen --separate-vocals \
  --qwen-python .context/local-genly/mlx-venv/bin/python \
  --qwen-checkpoint /ruta/al/checkpoint/local \
  --stem-manifest .context/vocal-continuity-2026-11-04/stems-expanded-private.json \
  --output .context/local-genly/staging-worker
```

El archivo de autenticación contiene `{"token": "..."}`, tiene permisos 0600 y
se renueva mediante la sesión revocable de staging. No se guarda la contraseña.
El token nunca se envía a la URL firmada del audio. El archivo original se verifica
por SHA-256 y los resultados quedan ligados al audio, la revisión y la letra fuente.

La API se activa únicamente en staging con
`GENLY_LOCAL_MODEL_CANDIDATES_ENABLED=1`. La aceptación manual reutiliza
`QUALITY_OPERATOR_SUGGESTIONS_ENABLED=1` y
`QUALITY_TIMING_OPERATOR_SUGGESTIONS_ENABLED=1`. La cola usa Redis; la candidata
privada usa `REVIEWER_CANDIDATE_STORAGE=r2`. Los endpoints `/admin/genly-model/*`
requieren administrador; `/editor/{job_id}/genly-model/*` comprueba acceso al job.
La recepción de candidatas exige administrador y, en trabajos encolados, un lease
vigente. Las canciones aprobadas y las líneas intervenidas se preservan.

La opción `--separate-vocals` usa únicamente el checkpoint htdemucs ya presente
en caché. Ejecuta la separación en un proceso CPU limitado y verifica el reloj
del stem antes de usarlo. Si no logra una voz utilizable, conserva el análisis
sobre la mezcla e informa que falta esa corroboración.

## Calidad medida y límites

- Whisper base con LoRA se entrenó realmente en esta Mac: 612 pasos, 196.608
  parámetros adaptados, cuatro checkpoints y selección por validación. Mejoró
  en validación, pero empeoró el replay de desarrollo: WER micro 78,81% → 88,08%.
  **Ese adaptador no es la versión activada.**
- Qwen 0.6B sobre los mismos 24 recortes de replay dio 39,07% de WER micro.
  Es una comparación entre reconocedores sobre recortes, no contra Genly completo.
- En la canción completa Es Tan Fácil (223 s), Qwen 1.7B con consenso local
  corrigió dos errores frente al original del pipeline: 13 → 11 errores sobre
  190 palabras, WER 6,84% → 5,79%, dos líneas mejoradas y ninguna empeorada en
  esa comparación. Las letras aprobadas se usaron después, para evaluar el
  resultado congelado; no se suministraron al reconocedor ni a la selección.
- En Viaje Sin Rumbo, 217 s, se conservaron las 134 discrepancias sobre 479
  palabras comparables (WER 27,97%): cero mejoras y cero regresiones. Un
  fragmento no obtuvo alineación válida; el resultado permanece parcial.
- Estos ejemplos son desarrollo, no un holdout nuevo ni una prueba de rendimiento
  humano. La coincidencia mezcla/voz proviene de la misma familia de modelo y
  se registra como evidencia correlacionada. No justifica aceptación automática.

## Resultados comprobados

- Catálogo: 660 entregas activas aprobadas, 483 Argentina y 177 Chile.
  Se localizaron 638 jobs fuente en staging: 638 con clave de audio, 311 con
  SHA registrado, 329 con originales no vacíos y 319 con alguna versión del
  editor aprobada. Esto no demuestra correspondencia entre esa versión y el
  render aprobado. El contador de publicación no es la revisión del editor.
- Corpus local: 33 originales verificados por SHA; 1.433 muestras de texto y
  audio; 1.211 comparaciones históricas, 613 con tiempos de línea comparables.
  Entrenamiento: 13 canciones/612 recortes/~29 minutos. Calibración: 14/583.
  Replay de desarrollo: 6/238. No es una evaluación nueva independiente.
- Selector: regresión logística CPU sobre 26 características anteriores a la
  edición. Usa 1.169 grupos con fuente; excluye 42 inserciones sin fuente.
  Aprende riesgo de edición editorial, no la corrección de una alternativa.
- LoRA pequeño: un paso real de Whisper tiny FP32 sobre seis segundos de audio
  de entrenamiento. 73.728 parámetros entrenables, 48 tensores modificados,
  gradientes finitos. Paso: 1,83 s; proceso: 11,26 s; RSS máximo: 528 MB.
  Memoria del driver MPS al terminar: 1,18 GB. Se conserva el fallo previo en
  FP16, que no produjo un adaptador utilizable.
- Whisper large-v3-turbo está en caché, pero su prueba se detuvo antes de
  cargarlo: ~1,85 GiB recuperables frente al mínimo conservador de 6 GiB.
  Ese mínimo es un presupuesto de seguridad, no una medición de consumo.

En el replay de desarrollo, Brier promedio por canción (menor es mejor):

| Objetivo del selector | Modelo | Constante aprendida en train |
|---|---:|---:|
| Cualquier edición | 0,1076 | 0,1165 |
| Texto/estructura | 0,1670 | 0,2543 |
| Tiempos | 0,2248 | 0,2231 |

La señal temporal no mejora. No habilita cambios de tiempos. Estas métricas
no son WER, precisión de propuestas ni evidencia de ahorro de edición.

## Ejecución local

Desde la raíz del workspace, preparar los datos ya descargados:

```sh
python3 lyricgen/backend/scripts/prepare_local_genly_corpus.py \
  --examples .context/approved-corrections-2026-11-04/dataset/examples-private.jsonl \
  --audio-map .context/vocal-continuity-2026-11-04/stems-expanded-private.json \
  --registry lyricgen/backend/data/song_roles.json \
  --output .context/local-genly/corpus

python3 lyricgen/backend/scripts/train_local_correction_selector.py train \
  --corpus .context/local-genly/corpus/deltas.jsonl \
  --output .context/local-genly/selector
```

Consultar `scripts/probe_local_lora.py --help` para ejecutar el preflight y la
prueba acotada. Sin `--execute`, no carga pesos ni entrena. La prueba exige
manifest y audio verificados, una muestra de train, pesos ya descargados,
memoria disponible y un watchdog. Nunca lanza una GPU remota como alternativa.

Revisar tres líneas con el adaptador pequeño ya generado:

```sh
python3 lyricgen/backend/scripts/run_local_genly_review.py \
  --snapshot .context/active-audio-three-song/9effe04b862f/snapshot.json \
  --audio .context/vocal-continuity-2026-11-04/9effe04b862f-original \
  --selector .context/local-genly/selector/model.json \
  --model tiny --adapter .context/local-genly/probe-tiny-fp32/adapter \
  --device mps --max-lines 3 \
  --output .context/local-genly/review-new-run --execute
```

Usar un directorio nuevo para cada revisión. Sin `--execute` se ejecuta sólo
el preflight. El reconocedor no recibe letra del editor ni referencias; usa
audio mono de 16 kHz y conserva los tiempos originales. El modelo de riesgo
ordena el trabajo. La salida registra cobertura parcial, modelo, adaptador,
hashes, fallos y propuestas de baja confianza que requieren escucha humana.
Whisper tiny puede alucinar en pausas; el número de propuestas no mide aciertos.

## Artefactos y reproducibilidad

Los artefactos privados permanecen en `.context/local-genly/`: catálogo,
`corpus/`, `selector/`, `asr-base-v1/`, los ensayos Qwen y `staging-worker/`.
Los pesos, audios, letras completas y credenciales no entran en Git ni en el
paquete de despliegue. Los informes incluyen hashes, cobertura, fallos, cambios
retenidos, hipótesis acústicas y una identidad del modelo compuesta con la
implementación que produjo el resultado.

Las pruebas cubren aislamiento por canción/artista/audio, procedencia del corpus,
artefactos alterados, revisiones obsoletas, autorización, expiración de leases,
privacidad de descarga, preservación de líneas humanas, resultados parciales y
el ciclo de publicación/aceptación/guardado del editor. La verificación de release
incluye el conjunto CI desde checkout limpio y un recorrido real en staging.

Para ampliar el entrenamiento falta identificar la revisión exacta que produjo
cada entrega aprobada: tener una publicación y una versión aprobada no demuestra
que sean la misma. El inventario deja esa incertidumbre explícita. Una siguiente
evaluación de generalización requiere canciones nuevas separadas antes de elegir
políticas o checkpoints; no se recategoriza el replay como holdout.
