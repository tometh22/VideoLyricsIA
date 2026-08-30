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
make eval-lora-research-prep
make eval-taxonomy-ensemble
make eval-runtime-replay
make eval-t7-prep
make eval-phase2-status
make eval-publish-diagnostic
make eval-ztlr
make eval-final-text-realign
make eval-hierarchical-realign
make eval-report-hierarchical
make eval-post-realign-review
make eval-flag-union
make eval-mss-alt
make eval-publish-zero-touch
make eval-agent-prepare CANDIDATES=/ruta/a/candidatos.jsonl EXTRACT_CLIPS=1
ALLOW_EXTERNAL_CLIENT_AUDIO_AGENT_REPLAY=1 make eval-agent-run AGENT_LIMIT=10
make eval-agent-score ADJUDICATIONS=/ruta/a/tres-jueces.jsonl
make eval-agent-policy ACTIVATED_AT=2026-08-29T12:00:00Z
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

`eval-lora-research-prep` descarga tres canciones en español de JamendoLyrics
y solo acepta licencias BY/BY-SA/CC BY/CC BY-SA; excluye marcadores NC y ND.
El ejecutor `eval.train_whisper_lora` exige un identificador de licencia por
muestra de investigación. Para UMG, además exige `ALLOW_UMG_TRAINING=1`; ese
flag no se configura hasta que la autorización quede registrada.

`eval-runtime-replay` carga el selector de timing directamente del objeto Git
desplegado y registra el SHA-256 de su fuente. Solo usa stems `mdx_extra`; si
faltan, el resultado queda como piloto incompleto. `eval-stems-local` regenera
los faltantes en la Mac, sin egreso, y permite reanudar por canción.

`eval-ztlr` publica la métrica norte operacional: una línea es zero-touch solo
si conserva texto, inicio y final desde el snapshot prehumano. El denominador
incluye líneas agregadas y eliminadas. El corpus histórico no contiene un timer
fiable de actividad del editor, por lo que no inventa minutos retroactivos.

`eval-final-text-realign` alinea el texto aprobado sin entregar a los modelos
ningún timestamp aprobado. Compara el CTC actual fijado por commit, MMS_FA y un
XLSR fonético con eSpeak. MMS_FA queda marcado como investigación no comercial;
el reporte usa bootstrap por canción y decide el gate p90 < 250 ms.
El backend `xlsr_ipa` requiere `espeak-ng` instalado localmente además de las
dependencias Python; nunca envía texto ni audio a un servicio externo.
La variante opcional `current_xlsr_anchored` usa el timing crudo únicamente
como prior de ocurrencia por bloques; su piloto de cinco canciones permanece
separado y `NO_GO`, por lo que no forma parte del replay predeterminado.

`current_xlsr_hierarchical` encuentra líneas únicas y n-gramas raros con una
cadena monotónica, encierra cada ocurrencia en ventanas locales y combina CTC
global/local con abstención explícita. El scorer compara siempre cohortes
idénticas y distingue el techo ZTLR del resultado medido. La variante
`current_xlsr_hierarchical_acoustic` localiza primero las anclas con CTC global;
permanece como experimento separado porque su stress test no pasó el gate.

`eval-post-realign-review` expresa el residuo en líneas, no solo en spans de
audio. Usa el gold únicamente para descomponer retrospectivamente texto,
timing y falsos flags; su salida no es un selector desplegable.

`eval-flag-union` combina únicamente probabilidades out-of-fold y busca el
umbral con recall de líneas corregidas ≥95% que minimiza la cola. Reporta falsos
flags y segundos reales de audio que el revisor debería escuchar.

`eval-mss-alt` implementa el replay de arXiv:2506.15514: calcula RMS-VAD sobre
el stem `mdx_extra`, pero transcribe el mix original. Siempre genera un control
nativo con el mismo Whisper y persiste ambas familias para replays futuros.

## Capa D: agente corrector

`eval-agent-prepare` construye dos artefactos físicamente separados: el pedido
que puede ver el agente y el gold de Agus que solo puede leer el scorer. Un
candidato entra únicamente si lo respaldan al menos dos familias acústicas
independientes. Whisper v2/v3/faster-whisper se colapsan a una sola familia;
Gemini se excluye como fuente porque es el agente que decide. También se
rechaza cualquier candidato derivado del texto o timing aprobado.

El contrato JSONL de candidatos es:

```json
{"zone_id":"song-id:12","proposals":[{"candidate_id":"p-123","category":"text","value":{"text":"línea candidata"},"supporting_families":[{"name":"whisper-large-v3"},{"name":"qwen3-asr"}]}]}
```

Una eliminación verificada se representa como `"value":{"delete":true}`;
también debe tener dos familias independientes. No se trata como generación
libre.

`eval-agent-run` no permite egreso accidental: además de credenciales Gemini
exige `ALLOW_EXTERNAL_CLIENT_AUDIO_AGENT_REPLAY=1`. El agente solo puede elegir
un candidato, editarlo mínimamente o abstenerse. El scorer habilita una
categoría únicamente con al menos 50 decisiones en 10 canciones, acuerdo
funcional ≥80%, falsos resueltos <3% y los desacuerdos requeridos juzgados por
tres familias no-Gemini. Vivo queda excluido siempre.

`eval-agent-policy` produce solo una política de elegibilidad. Nunca activa
runtime, nunca promueve a auto y exige un certificado firmado más una decisión
explícita de Tomi. La auditoría determinista es 20% durante 14 días y 10%
después. `eval.agent_tiers dashboard` resume participación auto/agente/Agus,
minutos humanos por canción y reversiones.

La taxonomía estricta requiere unanimidad de Qwen, Gemma y Mistral. Las filas
disputadas no se convierten en verdad por mayoría; `eval.export_taxonomy_clips`
genera la cola local con audio para adjudicación.

`eval-t7-prep` prepara pares sintéticos correcto/corrupto y registra el split
por canción, pero no entrena: el ejecutor UMG permanece detrás de la misma
autorización cerrada que LoRA.

La taxonomía se puede ejecutar enteramente con Ollama. El subcomando externo
`eval.classify_errors submit` está bloqueado además por código y exige
`ALLOW_EXTERNAL_CLIENT_TEXT_BATCH=1`, que solo se habilita después de una
autorización explícita de egreso de texto del cliente.

Los outputs pesados (`golden`, `runs`, `cache`, `hypotheses`) están ignorados por Git. Cada
corrida guarda matriz de alineación, matches, métricas y errores auditables.
Una coincidencia solo textual sin solapamiento temporal se veta cuando está a
más de 10 segundos: evita confundir ocurrencias distintas del mismo estribillo
y contaminar las métricas de timing.
