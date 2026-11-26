# Workers en OVH

Staging y producción corren en el mismo servidor, con base, Redis y colas
separadas, y comparten la CPU. El layout de abajo es el que queda después
del 2026-12-01. Los `railway/*.toml` no crean servicios nuevos: mientras
Railway siga prendido, el worker de render sigue escuchando
`enterprise,default,canary` para no dejar `default` sin consumidor.

## Procesos

Un proceso por rol. ProRes no escucha `default`. `default` no escucha ProRes.

| Rol | `QUEUES` | Whisper al arranque |
| --- | --- | --- |
| render | `enterprise,canary` | no |
| default | `default` | no |
| prores | `prores_portal,prores_prewarm` | no |
| interactive | `transcription,bg_preview,audio_preview` | `WHISPER_PRELOAD=1` |
| batch-short | `campaign_control,transcription_batch` | `WHISPER_PRELOAD=1` |
| batch-render | `batch_render` | no |
| quality | `transcription_quality` | no |

`deploy/ovh/start-workers.sh <rol>` y `deploy/ovh/docker-compose.yml`
levantan esos procesos. El worker ProRes consume en ese orden: primero
`prores_portal`, después `prores_prewarm`. No uses round-robin.

Levantá más procesos ProRes que `PRORES_MAX_CONCURRENCY`. La descarga no
toma el permiso de encode; si cada proceso está bajando, no queda nadie
para codificar.

El API tiene que tener las mismas `PRORES_PORTAL_QUEUE`,
`PRORES_PREWARM_QUEUE`, `PRORES_SEMAPHORE_NAME` y `PRORES_SEMAPHORE_DIR`.
Ahí se escriben los marcadores de demanda del portal. El API no corre
ffmpeg.

## Prioridad y tope

Los pedidos de cliente (materialize del portal, prewarm con `force`, click
de delivery) entran en `prores_portal`. El prewarm de fondo entra en
`prores_prewarm`. El formato del `.mov` no cambia.

El tope es un flock en disco, no un lock de Redis: staging y producción no
comparten Redis. Las dos stacks usan el mismo `PRORES_SEMAPHORE_NAME` y el
mismo directorio montado en el host (`/var/lock/genly-prores`). Un marcador
en ese directorio dice que hay un portal listo para ffmpeg, o encolado
esperando el slot, en cualquiera de las dos colas. El prewarm de la otra
stack no arranca mientras el marcador exista. La descarga lo suelta: bajar
la fuente no ocupa la CPU, y un portal bajando no tiene que frenar al
prewarm.

Si `PRORES_MAX_CONCURRENCY` no está, el tope no aplica. En la cola
`enterprise` el tope tampoco aplica: un defer ahí haría girar workers que
no conocen la compuerta.

Con tope 2 o más, el prewarm puede ocupar como máximo un slot menos
(`PRORES_PREWARM_MAX_SLOTS`, default `MAXC - 1`). El slot que sobra queda
para un portal que llegue cuando los prewarm ya están encodeando: no hay
preempción, así que sin esa reserva el portal espera a que termine un
ffmpeg de fondo. El portal puede usar cualquier slot libre y prefiere el
reservado, para no sentarse en el que el prewarm sí puede tomar. Con tope
1, o sin tope, el prewarm sigue pudiendo usar todos los slots.

`PRORES_PREWARM_BORROW=1` deja que el prewarm tome también el reservado
cuando no hay marcador de portal. Lo devuelve al terminar ese encode. El
default es no prestarlo.

La descarga de R2 ocurre antes de tomar el slot. Un socket mudo corta por
inactividad. Un stream lento pero constante (el caso de ~65 KB/s) corta
por el tope total. Esos intentos (`PRORES_DOWNLOAD_MAX_ATTEMPTS`, default
2) son el presupuesto completo: un corte por tope no dispara el `Retry`
de RQ. Cuando el job ya tiene la fuente, el worker se queda con el flock
hasta que el proceso hijo termina, así dos workers no se lo arrebatan y
reencolan en loop. `inactividad × intentos + encode` tiene que entrar en
`PRORES_PREWARM_TIMEOUT_SECONDS` (900).

## Variables

Vacío significa el comportamiento de Railway de hoy, salvo
`WHISPER_PRELOAD` (apagado: el modelo se carga en el primer job).

| Variable | Default | OVH |
| --- | --- | --- |
| `PRORES_PORTAL_QUEUE` | `enterprise` | `prores_portal` |
| `PRORES_PREWARM_QUEUE` | `enterprise` | `prores_prewarm` |
| `PRORES_MAX_CONCURRENCY` | sin tope | número de ffmpeg simultáneos en el host |
| `PRORES_PREWARM_MAX_SLOTS` | `MAXC-1` si `MAXC>1`, si no sin tope | cuántos de esos slots puede ocupar el prewarm |
| `PRORES_PREWARM_BORROW` | off | `1` presta el slot reservado si no hay portal esperando |
| `PRORES_SEMAPHORE_NAME` | `prores` | el mismo valor en staging y producción |
| `PRORES_SEMAPHORE_DIR` | `/var/lock/genly-prores` | bind del host, compartido |
| `PRORES_DOWNLOAD_INACTIVITY_SECONDS` | `45` | |
| `PRORES_DOWNLOAD_TOTAL_SECONDS` | `420` | |
| `PRORES_DOWNLOAD_MAX_ATTEMPTS` | `2` | |
| `PRORES_INPUT_CACHE` | `/tmp/genly-prores-inputs` | |
| `WHISPER_PRELOAD` | off | `1` solo en interactive y batch-short |
| `QUEUES` | ver la tabla de procesos | |

`PRORES_PORTAL_QUEUE` y `PRORES_PREWARM_QUEUE` se setean juntas. Una sola
deja jobs en una cola que nadie consume.

`PRORES_PREWARM`, `PRORES_PREWARM_TIMEOUT_SECONDS` y
`PRORES_PREWARM_MAX_QUEUE_DEPTH` siguen como estaban. La profundidad se
cuenta en la cola destino (portal si es un click, prewarm si es fondo).
