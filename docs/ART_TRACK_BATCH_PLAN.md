# Plan: campañas de 300–500 art tracks y entrega masiva UMG

Fecha: 2026-09-09. Estado: propuesta de implementación, basada en auditoría de código, workspaces, PRs y coordinación. No se implementó ni desplegó esta funcionalidad durante la planificación.

## Objetivo y experiencia

Desde Campañas, crear una campaña de tipo **Art tracks**, seleccionar el portal de destino y cargar de una vez 300–500 audios junto con sus portadas. El sistema identifica las parejas, muestra las excepciones, genera en segundo plano, permite revisar/corregir cada resultado y publica en lote únicamente las versiones aprobadas.

Flujo: **Crear campaña → Cargar audios y covers → Confirmar asociaciones → Generar → Revisar → Enviar aprobados al portal → Consultar resultado por canción**.

“De una vez” significa una selección/importación para todo el lote. Las transferencias y renders tienen concurrencia limitada; no se abren 500 uploads o renders simultáneos. Cuando todos los archivos están confirmados en R2, el procesamiento continúa con la computadora apagada.

Destinos explícitos: `argentina` → `umg.genly.pro`; `chile` → `umgchile.genly.pro`. El destino no se deduce del nombre de un archivo ni cambia el tenant dueño del audio. Cada campaña usa un destino predeterminado; publicar también en el otro portal será una operación explícita, con permisos y resultado independientes.

## Lo revisado y qué podemos reutilizar

| Área / procedencia | Evidencia observada | Consecuencia para este plan |
| --- | --- | --- |
| Miami: campañas | PR [#1232](https://github.com/tometh22/VideoLyricsIA/pull/1232), mergeado a staging; plan en `miami/.context/plans/campa-as-masivas-carga-correcci-n-y-generaci-n-des.md`; `docs/BATCH_CAMPAIGNS.md` | Reutilizar campañas tenant-scoped, manifiesto durable, pairing, multipart R2, deduplicación, pausa, reintentos, reconciliador y pools batch. |
| Riyadh: pipeline y revisión de campañas | Staging incluye etapas de preparación/transcripción, aprobación ligada a audio/revisión y cola de revisión; PRs #1271, #1276, #1284, #1286, #1290, #1298–#1301 | Mantener esos controles para lyric videos. Art tracks necesita su propio recorrido, sin etapas ficticias de letra o timing. |
| Singapore-v1: navegación del revisor | `REVIEWER-UI-HANDOFF.md`, `REVIEWER-STAGING-DEPLOY.md`; handoffs posteriores documentan integración de la UI 1.1.4 | Reutilizar filtros en URL, retorno al ítem, cards/contadores estables, carga progresiva y refresco visible sin solicitudes superpuestas. |
| Motor de art tracks | `main.py` (`/generate`, `/jobs/{id}/edit-art-track`), `pipeline.py`, `art_track_wave.py`, `ArtTrackEditPanel.jsx` y pruebas existentes | Ya hay composición cover + fondo + waveform, master/short/thumbnail, edición y feature gate. No hace falta crear otro renderer. |
| Entregas existentes | `main.py` (`/admin/deliveries/from-job/{job_id}`), `Delivery`, `get_deliveries_db` | Publicación individual exige `done` y `approved_at`; comprueba archivos R2 y puede preparar ProRes faltante. Extraer servicio reutilizable para el lote. |
| Nagoya: AR/CL | Worktree `tometh22/umg-chile-portal`, cambios locales en `database.py`, `main.py` y migración `f2a4b6c8d0e2_deliveries_portal_id.py`; notice de ownership | Hay trabajo en curso para `Delivery.portal_id`, filtros/credenciales por portal, UI Editar y retención. Integrarse con ese contrato; no implementar otra separación de portales. |
| Frontend de portales | `/Users/tomi/genly-deliveries/index.template.html` y `gen_page.py`, fuera de este repo | El listado actual se obtiene de la API. Su README describe un mecanismo anterior con URLs al build y está desactualizado; no usarlo como contrato vigente. |

Al consultar el remoto, el target de Conductor `origin/claude/build-lyricgen-app-g8gYT` y este workspace estaban en `f9bc00aa`, mientras staging estaba en `7e219f6e` (PR [#1307](https://github.com/tometh22/VideoLyricsIA/pull/1307), release 1.1.6 según el handoff de Riyadh). **Este workspace no representa todo lo que hoy tiene staging.** La implementación deberá integrar la base vigente y pasar revisión del diff; este plan no cambia la rama ni propone desplegar el HEAD viejo. El target de PR configurado en Conductor se conserva hasta que se defina el recorrido de integración a staging.

La auditoría verificó fuentes y estado de PRs. Los resultados de smoke/deploy citados de otros agentes son evidencia reportada por ellos, no una nueva prueba E2E ejecutada aquí. Los cambios locales de Nagoya son trabajo en curso, no prueba de despliegue de Chile.

## Brechas concretas

1. `BatchCampaign` no distingue lyric video de art track. `_promote_campaign` crea trabajo de transcripción y el uploader acepta audios, no pares audio/cover.
2. En staging, `enforce_render_capacity` llama a `require_prebackground_approval`, que exige referencia, letras y timings aprobados. Pasar solamente `art_track=true` no adapta correctamente campañas.
3. `/generate` individual recibe la portada como multipart. El camino masivo debe materializar audio y cover desde claves R2 validadas, sin pasar los 500 archivos por la memoria de la API.
4. Falta un registro durable de asociaciones, assets de covers reutilizables y sus revisiones.
5. `REQUIRE_REVIEW` puede llevar un render a `done` sin revisión. La campaña de art tracks debe exigir revisión humana independientemente del valor global.
6. Falta una operación de publicación masiva que sobreviva cierres de pestaña, caída de workers y fallos parciales. “Job terminado” y “entregado al portal” son estados diferentes.
7. El publisher puede escribir en una DB de entregas externa, incluso desde staging. Hay que distinguir ambiente de generación y destino real de publicación, y probar en un destino aislado.
8. La política de retención en desarrollo debe preservar audio, covers compartidos y versión publicada mientras sean necesarios para edición o descarga.

## 1. Importación y asociación de archivos

### Interfaz de carga

- Nueva campaña: nombre, tipo `art_track`, cantidad esperada, destino permitido y preset de entrega/estilo. Límite inicial de producto: 500 canciones; conservar compatibilidad con el límite genérico de 1000.
- Seleccionar carpeta(s) o arrastrar audios y covers desde el panel. Importar estructura de subcarpetas y conservar rutas relativas para desambiguar. WAV/MP3 y JPG/JPEG/PNG en la primera versión, alineados con el motor actual.
- Extender también `scripts/campaign_uploader.py` para usar el mismo manifiesto y contrato de assets. Mantenerlo como alternativa robusta para carpetas grandes; no hacer del uso de terminal el único flujo de la nueva pantalla.
- Cargas directas a R2, progreso por archivo y por bytes, 4 transferencias simultáneas iniciales configurables, hash incremental y memoria acotada. Reanudación por partes; al reabrir el navegador, pedir reseleccionar la carpeta si sus permisos locales no persisten, verificar hashes y subir sólo lo faltante.
- Diferenciar “registrado”, “subiendo” y “almacenado/verificado”. No informar “cargado” sólo porque se creó el manifiesto. El token de carga sólo puede operar assets de esa campaña.

### Orden de matching propuesto

1. **Asignación explícita** en planilla opcional CSV/XLSX o corrección manual. Columnas: `audio_file`, `cover_file`, `title`, `artist`, `technical_code`/`isrc` opcional, `album_id` opcional y `label_line` opcional. Rutas relativas exactas; validar celdas como datos.
2. **Identificador exacto y único compartido**, cuando existe: código técnico o ISRC. No limitar Chile al parser actual de códigos ARF/ARUM de Argentina.
3. **Nombre base exacto y único normalizado**: diferencias de extensión, mayúsculas y normalización Unicode; conservar versión/mix y ruta. No eliminar arbitrariamente palabras que distingan canciones.
4. **Cover de álbum/carpeta**: sólo con regla explícita confirmada para esa carpeta o grupo. Soportar un cover para muchos audios; almacenar una sola copia del asset por campaña/hash.
5. Si falta una relación inequívoca, dejar **“Falta cover”** o **“Asociación ambigua”**. Las coincidencias aproximadas pueden sugerirse, pero no habilitan render automáticamente.

La pantalla muestra miniatura, audio, título/artista, código, regla usada y conflictos. Ofrece asignar cover a una selección, resolver uno a uno y descargar el reporte de errores. Dos imágenes distintas con el mismo código son conflicto; un archivo duplicado idéntico no lo es. Un cover sin audio queda reportado, no crea una canción. Un audio repetido con otro cover no crea otra canción silenciosamente: el operador decide si es corrección o una versión intencional.

Confirmar las asociaciones inequívocas en bloque y resolver las excepciones antes de promocionarlas. Los ítems válidos pueden avanzar aunque otros estén bloqueados, mediante una acción explícita “Generar N válidos”; mostrar cuántos quedan afuera. No usar portada genérica ni buscar imágenes externas automáticamente.

### Validación de entrada

Backend: MIME real/decodificación, tamaño, duración y checksum del audio; decodificación, tamaño de archivo y límite de píxeles de la imagen; normalización EXIF/color y miniatura. Definir umbrales de resolución según el preset; imágenes no cuadradas requieren preview de ajuste y confirmación, sin recorte destructivo oculto. Un nombre válido no reemplaza verificar el archivo recibido. Validar pertenencia de cada asset y evitar rutas locales absolutas o claves R2 ajenas en las solicitudes.

## 2. Modelo durable y contratos

Nombres nuevos orientativos; los endpoints existentes de lyrics mantienen compatibilidad.

| Entidad | Cambio propuesto |
| --- | --- |
| `BatchCampaign` | `kind=lyric_video|art_track` con default legacy `lyric_video`; destino predeterminado validado; preset versionado. Tipo inmutable una vez iniciada la importación. |
| `BatchCampaignAsset` nueva | Campaña/tenant, rol audio o cover, nombre/ruta relativa, hash, MIME, tamaño, dimensiones/duración, clave R2, estado y sesión multipart. Deduplicación por campaña/rol/hash. Introducir de forma aditiva sin obligar a migrar todos los audios históricos. |
| `BatchCampaignItem` | Referencias a assets, estado/método de asociación, confirmación de asociación, errores estructurados y revisión de entrada. Mantener el vínculo único item→job existente. |
| Job / intento de render | Snapshot de audio hash, cover hash, metadata, preset y revisión; tipo validado del lado servidor y `workload_class=batch`. Identificador de versión de render separado de la revisión de letras. |
| Aprobación de art track | Usuario, fecha, versión de render y fingerprint de inputs/preset. Cambiar audio/cover/textos/configuración invalida aprobación y elegibilidad de envío. |
| `DeliveryBatch` / `DeliveryBatchItem` nuevas | Operación durable de envío, actor, campaña, portal, snapshot de selección, versión aprobada, idempotency key, estado, intentos, error y receipt `delivery_id`. |
| `Delivery` | Reutilizar `portal_id` de Nagoya. Referencia verificable a versión publicada y tipo de contenido. Definir unicidad de versión/destino para resolver carreras del publisher. |

API propuesta:

- Extender creación/manifiesto/items con tipo, assets y asociaciones; validar en páginas de hasta 100 entradas, conservando una importación lógica única de 500.
- Tickets y finalización por **asset**, con reanudación independiente para audio y cover. Mantener rutas legacy durante la transición.
- Preview/confirmación de matching y edición por selección con revisión esperada.
- Acción `start-rendering` idempotente para los ítems confirmados; el reconciliador promueve gradualmente.
- Cola de revisión art track paginada, claim por pestaña, aprobar/rechazar/editar y avanzar, ligados a versión esperada.
- `POST /batch/campaigns/{id}/deliveries/preview`: valida selección/destino y devuelve elegibles, ya enviados y bloqueados con motivo. Sin publicar ni iniciar conversiones como efecto oculto de consultar.
- `POST /batch/campaigns/{id}/deliveries`: crea operación con snapshot explícito e idempotency key; responde `202` y `operation_id`.
- `GET /batch/delivery-operations/{id}` y reintento de fallos. “Seleccionar todos los aprobados” abarca todo el filtro/campaña, no sólo las 50 filas visibles.

Autorización en servidor: acceso a campañas + art tracks + tenant; para publicar, conservar inicialmente el permiso admin existente. Revisor y publicador son capacidades diferentes. Revalidar permisos y versiones al ejecutar, no sólo al mostrar botones.

## 3. Generación autónoma y capacidad

Estado de producción: **esperando assets → asociación pendiente → listo → en cola → renderizando → pendiente de revisión → aprobado**. Error/rechazo son recuperables por ítem. Estado de publicación separado: **no enviado → preparando entrega → enviando → enviado / fallo**.

- Elegir pipeline por `campaign.kind` persistido. Un art track exige audio+cover verificados y asociación confirmada; llama al pipeline existente con `art_track=True`, sin ASR, Demucs, letras, Veo ni fondos IA. Los controles de aprobación de lyrics siguen aplicando a `lyric_video`; no aceptar un flag arbitrario como bypass.
- Crear job e intención `pipeline.enqueue` en la misma transacción usando `transactional_outbox.create_pipeline_outbox_event`. Reusar reintentos del outbox y aislamiento `batch_render`. El helper observado crea una dedupe key con UUID nuevo: añadir idempotencia de la operación por ítem/revisión y transición bajo lock; invocarlo dos veces no garantiza por sí solo un único render.
- Materializar audio y cover en el worker desde las referencias verificadas. Persistir `input_r2_key`, cover y render params que necesitan edición y retry.
- Partir del techo batch existente de 10 renders y buffer de revisión 50, configurables tras medir. El límite debe reservar espacio para todos los renders activos: `room = min(límite_render - activos, límite_revisión - pendientes - activos)`. No admitir 10 nuevos si 49 están pendientes.
- Reservas atómicas por tenant/pool y control global compartido entre campañas, con turno equitativo; un lock de una sola campaña no evita sobrepasar el límite con dos campañas simultáneas. Mantener CPU/RAM/disco para interactive y para campañas lyric en el mismo pool.
- Cuando la revisión está llena, pausar promociones automáticamente y explicar el motivo en UI. Todos los audios pueden permanecer subidos aunque no estén renderizados. Ediciones/reintentos usan el mismo presupuesto batch.
- Forzar revisión final para este tipo de campaña aunque `REQUIRE_REVIEW=false`. Nunca publicar desde el callback de render.
- Preservar política de créditos existente; añadir preflight y reserva idempotente para impedir que 500 promociones simultáneas excedan el saldo o se cobren dos veces al reintentar. El plan no cambia precios.
- Pausa/cancelación detienen trabajo nuevo y dejan terminar lo ya iniciado. Cancelar no borra fuentes ni retira automáticamente versiones publicadas. Separar “producción completada” de “todas las entregas completadas”.
- Agrupar notificaciones por campaña para evitar 500 emails de finalización. Medir transferencias, tiempos render/ProRes, backlog, bytes y minutos de revisión; no prometer throughput a partir del comentario de duración del renderer individual.

## 4. Revisión antes de enviar

- Reutilizar estructura de navegación del revisor vigente con una vista específica de art tracks: reproducción, cover, título, artista, línea legal, formato y resultado de QC.
- Acciones “Aprobar y siguiente”, “Corregir” y “Rechazar”; edición con `ArtTrackEditPanel`, regreso al mismo filtro y posición. No mostrar checklist de letras/timing.
- Claim con TTL y sesión por pestaña para evitar que dos revisores aprueben/corrijan a la vez. Mutaciones con revisión esperada; una aprobación vieja no aprueba un nuevo render.
- Aprobar deja receipt del render revisado. La publicación masiva selecciona sólo aprobaciones vigentes. No considerar la mera reproducción o el estado `done` como aprobación.
- QC técnico específico: audio correcto y completo, duración A/V, resolución/fps, archivos decodificables, cover aplicado y presencia de entregables del preset. Las pruebas automáticas complementan la revisión humana de asociación, imagen y textos.
- Persistir contadores por etapa y destino. Página de 50 filas, búsqueda y filtros del servidor; miniaturas lazy, sin precargar 500 reproductores.

## 5. Envío masivo a Argentina o Chile

1. El operador elige aprobados y destino. Preview: “Enviar 287 art tracks a UMG Chile; 8 ya enviados; 5 requieren corrección”, con causas verificables. Mostrar hostname real y si escribe en el portal de cliente desde staging.
2. Al confirmar ese conjunto, guardar operación y selección exacta. El worker revalida permiso, aprobación, versión, assets y disponibilidad del destino antes de cada envío. Una aprobación posterior no entra por sorpresa en la selección.
3. Preparar sólo los derivados requeridos por el preset. Si el publisher pide ProRes, transicionar a `preparing`; no tratar su `409` de preparación como fallo final ni reencolar conversiones sin límite. Portales y presets son conceptos separados; el usuario no tiene que entender colas ni variables de entorno.
4. Extraer lógica del endpoint individual a un servicio compartido de publicación, conservando verificaciones de aprobación y R2. No disparar 500 llamadas desde el navegador ni reemplazar verificaciones por inserciones directas en DB.
5. Commit idempotente en DB de entregas y receipt local reconciliable. No hay una transacción SQL única entre DB de jobs y DB externa de deliveries: si cae el worker después del commit remoto, consultar por clave estable y recuperar receipt, sin duplicar ni resetear aprobación del cliente en un reintento.
6. Un fallo de una canción no revierte las otras. Reintentar sólo fallidas o pendientes. Concurrencia conservadora inicial de 2 publishers, configurable; conversiones ProRes respetan capacidad propia y no compiten libremente con renders.
7. Mostrar enviados/pendientes/fallidos por portal, enlace a la entrega y reporte descargable. Cerrar la pestaña no detiene la operación. La aprobación posterior de UMG en el portal es distinta de la aprobación interna previa al envío.

### Contrato AR/CL que debe cerrarse con Nagoya

- Reutilizar `portal_id=argentina|chile` y `X-Portal-Id`. La fuente local de Nagoya filtra también por tenants: Chile usa por defecto `universal_chile`, mientras Argentina conserva compatibilidad legacy. Un job de `universal_music` enviado a Chile podría quedar invisible si no se habilita explícitamente ese origen. El preflight debe comprobar **publicable y visible para el destino**; no arreglarlo cambiando el tenant ni quitando filtros.
- Definir allowlist de tenant origen → destino permitido; validar en publisher y en todos los endpoints de lectura, aprobación, cambios y baja. Mantener separados aprobación y pedidos de cambios por portal.
- La fuente observada permite fallback de tokens al secreto legacy. Verificar configuración efectiva y aislamiento con el dueño del portal antes de habilitar masivos; conocer un ID de entrega de otro portal no debe permitir operarla.
- Comprobar unicidad de publicación bajo concurrencia, preservación de deliveries legacy como Argentina y lectura/visibilidad de una misma versión en ambos destinos sin duplicarla accidentalmente en uno.
- El listado actual resuelve metadata de objetos R2 durante la consulta. Para 500 canciones, persistir tamaños/metadata al publicar, paginar/listar sin miles de HEAD por refresco y firmar descargas bajo demanda. Ajustar API y frontend juntos, conservando compatibilidad del portal existente.
- Agregar tipo de contenido en tarjetas/filtros y en el agrupamiento de versiones: un art track y un lyric video de la misma canción no deben aparecer como una corrección intercambiable.

### Versiones publicadas y retención

No permitir que re-renderizar sobre claves R2 estables cambie silenciosamente el archivo ya aprobado/publicado. Usar objetos inmutables por versión o snapshot de publicación y guardar sus claves en el receipt. Una nueva versión requiere nueva revisión y envío explícito; la anterior permanece disponible hasta el reemplazo definido.

Integrar el trabajo de retención de Nagoya (60 días según ownership, pendiente de verificación final): retener sources necesarios para reedición, proteger covers compartidos por referencias y no borrar un asset si algún job/publicación vigente lo usa. Proteger también operaciones de envío activas. Expiración debe ser visible; una publicación expirada no cuenta como vigente al reenviar y un retry no prolonga su vida accidentalmente.

## 6. Secuencia de implementación y aceptación

| Entrega | Alcance | Criterio para avanzar |
| --- | --- | --- |
| 0. Base y contrato | Integrar staging vigente y acordar contrato de portales con Nagoya; identificar dueño de release | Diff sin regresión de revisión/campañas; destino, permisos, versión publicada y retención definidos. |
| 1. Modelo/importador | Tipo de campaña, assets, matching, carga browser + cargador local | Manifiesto de 500 canciones completo; covers compartidos; interrupción/reanudación; ambiguos no avanzan. |
| 2. Orquestación art tracks | Outbox, materialización R2, controles de recursos, aprobación obligatoria | Un job por ítem, cero llamadas ASR/Demucs/IA, límites respetados bajo dos reconciliadores/campañas. |
| 3. Revisión | Cola específica, edición, claims y aprobación por versión | Dos pestañas no toman el mismo ítem; cambiar cover invalida aprobación; volver conserva contexto. |
| 4. Publisher masivo | Operación durable, preflight, ProRes, AR/CL, versiones/receipts | Reintentos sin duplicados y aislamiento entre destinos, incluso caída tras commit remoto. |
| 5. Integración/capacidad | Portal paginado, métricas, retención y pruebas staging | Canary y carga objetivo con reporte; operación completa revisable y recursos interactivos preservados. |

Pruebas necesarias al implementar:

- Matching: códigos AR/CL, Unicode, nombres iguales en carpetas distintas, live/remix, planilla que referencia archivo ausente, cover repetido/compartido, dos covers para un código, duplicado de audio con metadata conflictiva y corrección manual persistida.
- Upload: 500 audios y covers, corte a mitad de multipart, token vencido/renovado, re-selección de carpeta, archivo modificado entre sesiones, checksum incorrecto, imagen inválida o excesiva. Recuperar sin repetir bytes confirmados.
- Orquestación: doble start, doble reconciliador, caída entre commit y dispatch, cuota agotada, pausa, cancelación, buffer lleno, recuperación y fair share con lyric/interactive.
- Aprobación: `REQUIRE_REVIEW=false` no permite saltarla; cover/revisión vieja/doble clic no publica; cambio de permisos bloquea ejecución; rejected no es elegible.
- Delivery: faltante real vs derivado preparándose, error parcial, timeout, caída tras commit externo, token/destino incorrecto, mismo job en ambos portales, filtro de tenant incompatible y nueva versión pendiente sin reemplazar la publicada.
- Portal y retención: 500+ filas sin avalancha de HEAD, descarga vigente de versión correcta, cero lectura/mutación cruzada AR/CL, retiro en un portal no borra el otro, cover compartido y source de reedición protegidos.
- Regresión: campañas lyric conservan aprobación de letra/timing; art track individual, retry, edición, QC y publicación individual existentes siguen operativos.

Validación escalonada: **5–10 canciones → 30 → 100 → 500**. Fixtures sintéticos para consistencia/concurrencia; luego archivos autorizados para render real. La prueba de 500 debe ejercitar carga completa, drenaje mediante revisión, publicación y fallos recuperables; probar sólo 500 filas no acredita capacidad de render o entrega. Medir p95/errores de interactive con baseline comparable y fijar umbrales operativos antes del ensayo. No se promete duración o costo total sin esa medición.

Usar un destino de entregas aislado en las pruebas; staging puede apuntar a la DB de portales reales. El primer envío de cliente debe ser una selección concreta de versiones aprobadas y tener su owner de verificación.

## Coordinación y release

Worcester es dueño de este plan. Nagoya mantiene ownership de implementación de portales AR/CL y retención; Riyadh tiene el handoff más reciente de release staging. No se asigna a ninguno una nueva ejecución de deploy por este documento.

Antes de implementar o integrar: registrar/listar agentes, leer mensajes y notices de `tometh22/VideoLyricsIA`, acordar dueño único de migraciones, versiones, orden de merges, deploy y smoke. Releases actualizan `VERSION`, `lyricgen/frontend/package.json` y `CHANGELOG.md` juntos y pasan CI desde checkout limpio. No ejecutar builds, browser, DB ni coverage competitivos en el host compartido. Releer inbox antes del merge y cerrar el handoff al terminar.

Feature flag nuevo propuesto `BATCH_ART_TRACK_ENABLED`, además de permisos existentes. Rollback operativo: desactivar nuevas promociones/publicaciones, dejar drenar trabajo iniciado y preservar fuentes/receipts; no revertir destructivamente migraciones ni borrar entregas ya publicadas. Publicación y generación deben poder pausarse por separado.

## Supuestos pendientes de validar con archivos reales

- Se consultó al usuario cómo llegan los covers. Mientras se confirma, el diseño contempla nombre/código compartido, cover por carpeta/álbum y planilla opcional sin hacer obligatorio un CSV.
- Preset inicial recomendado: composición art track existente, un cover por canción con reutilización explícita para álbum; requisitos de master/short/thumbnail/ProRes se fijan con el preset UMG vigente, sin presumir que todos los lotes necesitan cinco archivos.
- La habilitación de publicación cruzada entre tenants AR/CL y el estado final desplegado de Chile dependen del contrato de Nagoya. Son gates de esa entrega, no impedimentos para desarrollar importación y revisión.
