# División automática de frases antes del editor

Implementación: `display_segmentation.py`, política `display-phrases-v1`.

## Recorrido y alcance

La etapa corre en `_quality_gate_and_retry`, compartida por el worker RQ y las
rutas HTTP, después del formateador y de todos los candidatos de corrección.
Después de una división aceptada se vuelven a medir cobertura y ventanas sin
permitir que esa medición mueva tiempos. El veredicto final y la evidencia se
calculan sobre las tarjetas resultantes. No se vuelve a formatear ni ordenar.

Se aplica exclusivamente a una transcripción nueva: revisión cero, sin letra
guardada, sin documento abierto y sin aprobación. La identidad del audio y la
revisión se capturan antes del ASR y se contrastan al guardar bajo el bloqueo
de la canción. Abrir el editor en revisión cero también invalida la aplicación.
Los reintentos producen los mismos IDs para la misma propuesta. Dos workers
no pueden persistir dos divisiones sobre la misma transcripción nueva.

No se activa desde autosave, reanchor, render o publicación. No modifica
canciones históricas, versiones aprobadas ni publicaciones. El reemplazo de
letra y la autorización explícita de IDs bloqueados conservan sus contratos.
La reutilización de una letra aprobada tampoco se vuelve a dividir.

## Contrato

- Texto: slices ordenados de los tokens originales, con tildes, puntuación y
  paréntesis. Solo se normalizan separadores en las tarjetas nuevas; la versión
  original completa, incluido el espaciado, queda en evidencia privada.
- Tiempos: palabras actuales, una por token, finitas, de duración positiva,
  contenidas en el padre y sin superposición. No hay tolerancia fonética para
  sustituir palabras, ni distribución proporcional por caracteres/palabras.
- Los extremos exteriores del padre se conservan exactamente. Cada límite
  interior coincide con el inicio de la siguiente palabra, después del final
  de la anterior. El editor conserva su precisión existente de 0,1 ms; se
  verifica y vincula también esa representación persistida.
- Candidatos: pausas de al menos 180 ms, o puntuación/repetición contigua con
  pausa de al menos 40 ms. No se cortan paréntesis ni se deja una preposición,
  artículo o conector común al final de una tarjeta. No es un parser musical
  universal: si no hay una partición respaldada, se abstiene.
- Presupuesto: objetivo seis palabras, máximo ocho y 42 caracteres por tarjeta,
  mínimo tres palabras y 650 ms. Una nota sostenida corta no obliga a dividir.
  Estos límites no certifican el ajuste visual para todas las fuentes y looks.
- Cada hijo tiene ID determinista por fuente, ocurrencia y rango de tokens.
  Las tarjetas exteriores quedan iguales. No se copian señales analíticas o
  confianza agregada del padre como si fueran nueva evidencia de reconocimiento.
- No se modifica ninguna fila bloqueada o con evidencia obsoleta. No se parte
  una capa vocal que se superponga a otra, incluida cualquier voz anterior que
  siga activa aunque ya termine la tarjeta precedente. No se ordena para
  esconder inversiones. Una violación rechaza la aplicación completa.

`plan` es puro. `apply` vuelve a comprobar fuente completa (incluidos bloqueos
 y metadatos), hash, política, rangos y partición. Un hash recalculado por un
cliente no autoriza una propuesta arbitraria.

## Observación y activación

`DISPLAY_SEGMENTATION_MODE`:

| Modo | Resultado |
|---|---|
| `off` | La etapa nueva no modifica ni analiza las tarjetas. |
| `shadow` (predeterminado) | Calcula propuesta y pendientes; conserva todas las tarjetas. |
| `enforce` | Aplica candidatos seguros solo con autorización propia válida y canción nueva. |

El segmentador heurístico anterior deja de actuar en shadow/enforce. Sus flags
no habilitan esta política. El formateador ortográfico rechaza divisiones
inesperadas del modelo en todos los modos; no fabrica relojes proporcionales.

Enforce requiere un archivo firmado Ed25519, hash SHA-256 esperado y claves
públicas del runner de validación en:

- `DISPLAY_SEGMENTATION_AUTHORIZATION_PATH`
- `DISPLAY_SEGMENTATION_AUTHORIZATION_SHA256`
- `DISPLAY_SEGMENTATION_PUBLIC_KEYS` (objeto JSON de ID → clave pública base64)

El artefacto tiene schema `display-segmentation-authorization-v1`, política
`display-phrases-v1`, decisión `GO`, fecha `expires_at` UTC futura,
`pipeline_release` y `pipeline_config_fingerprint` iguales al runtime.

Debe declarar `checks` verdaderos para `exact_text`, `parent_bounds`,
`protected_work`, `concurrency`, `song_holdout`, `manual_boundary_review`,
`render_parity` y `recovery`. `boundary_validation` exige al menos 300 límites
revisados, límite inferior de precisión al 95% de al menos 0,99, cero cortes
dañinos y al menos 20 canciones de holdout. La cifra de precisión debe venir
de la evaluación, nunca de los tests unitarios ni de una estimación del agente.
La firma acredita al runner, no reemplaza la revisión real de esas pruebas.
La paridad de timing de `/health/ready` también compara la política, sus
parámetros, el modo y la identidad de la autorización/claves en API y todos
los workers. No se acepta una flota mezclada. Una autorización de corrección léxica o de análisis v6 no sirve para este fin.
Un artefacto ausente, vencido, inválido o de otra configuración conserva el
modo de propuesta, sin modificar letras.

No se generó un certificado de producción en esta implementación. El lote
histórico de 40 tarjetas editadas no es una muestra independiente del ASR
inicial y no se presenta como calibración de exactitud musical.

## Evidencia, recuperación y operación

El informe público no contiene la letra; incluye rangos, motivos, conteos y
hashes. La propuesta y el estado previo completo se guardan en evidencia
privada del documento, separados de `pre_auto_repair` y del audio/ASR original.
La validación de evidencia vuelve a ejecutar el contrato contra ese origen.
El reporte queda disponible aunque una reevaluación de calidad sustituya el
resumen mutable. Después de editar se muestra como anterior, sin certificar
los nuevos cambios.

El editor muestra cuántos bloques se dividieron o se propusieron, y permite
escuchar todos los tramos pendientes sin truncar la lista. No exige un clic
extra por cada corte correcto. La aprobación de letra/timing de la canción
completa sigue siendo necesaria. La división no comprueba que el ASR haya
reconocido bien cada palabra ni reemplaza el QC visual del video.

El botón existente para deshacer una mejora automática restaura una revisión
nueva únicamente mientras la salida siga intacta, sin aprobación y con el mismo
audio. Si también hubo correcciones automáticas previas, restaura el estado
anterior a esas correcciones; ambos snapshots privados permanecen separados.
No escribe esas restauraciones como correcciones humanas de entrenamiento.

El render conserva las tarjetas aprobadas; no hace otra división oculta ni
regenera fondos por esta funcionalidad. Los manifiestos y controles existentes
siguen vinculando la revisión y los bytes del archivo revisado/publicado. Un
video anterior requiere un nuevo render con el mismo fondo para mostrar las
nuevas tarjetas; modificar la letra no cambia un MP4 existente.

## Prueba local sin modificar canciones

Desde `lyricgen/backend`, con una exportación de segmentos y su audio:

```sh
python3.11 scripts/preview_display_segmentation.py \
  --input /ruta/segmentos.json \
  --audio /ruta/cancion.wav \
  --output /ruta/preview-nuevo.json
```

El comando calcula la identidad real del audio, produce propuesta y tarjetas
para previsualizar, no llama proveedores ni bases de datos y no sobrescribe un
archivo existente. `preview_only` no es aprobación humana ni autorización de
producción. Si el contrato falla conserva las tarjetas originales y declara
`contract_rejected`. Para escuchar deben usarse la mezcla original y los
relojes de `preview_segments`; no un video antiguo.

## Release y validación pendientes

1. Ejecutar CI completo desde checkout limpio, incluido PostgreSQL, navegador
   y el gate de render real. Los tests SQLite locales no prueban bloqueos de
   filas de PostgreSQL.
2. Liberar API y todos los tipos de worker con el mismo código/configuración;
   el fingerprint de backend cambia. Un único responsable coordina el release,
   migraciones existentes, despliegue y smoke. No hay migración nueva.
3. Observar transcripciones nuevas en shadow. Medir por canción y por límite:
   candidatos/aplicados/abstenciones, cortes dañinos, tiempo de revisión y
   motivos de abstención. No medir solo coincidencia con un corte humano.
4. Separar canciones de calibración y holdout. Incluir coros simultáneos,
   notas largas, frases legítimamente largas, dos tarjetas, repeticiones,
   paréntesis, bilingüismo y evidencia ausente/obsoleta. Escuchar límites
   propuestos contra la mezcla original y verificar el video con su look real.
5. Emitir el recibo firmado solo si esa evaluación pasa; entonces activar
   enforce de forma coordinada. Ante una regresión volver a shadow/off. Las
   canciones ya guardadas no se vuelven a cortar y los controles de entrega
   no se desactivan.
