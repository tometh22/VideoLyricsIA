# Corrección automática de letra y timing

El worker puede analizar una canción antes de abrir el editor y corregir un
tramo pequeño cuando una propuesta pasa cuatro filtros: snapshot vigente,
dos familias de evidencia independientes, cambios locales compatibles con la
acción autorizada y una reevaluación de calidad que mejora el resultado. Si
cualquier filtro falla, conserva la versión original y registra solo el motivo
acotado y el conteo en `transcription_quality.auto_repair`.

La independencia se evalúa por familia canónica del modelo: nombres alternativos,
variantes de interfaz y vistas del mismo proveedor no cuentan como dos fuentes.
Si falla el proveedor usado solo para proponer una reparación, el motor se
abstiene y conserva la evaluación y los segmentos originales; el fallo de esa
propuesta no se registra como fallo de la evaluación de calidad.

## Acciones que admite la primera versión

- `timing_reversible`: mover límites o tiempos de palabras sin alterar la
  letra, la cantidad de líneas ni el orden. El candidato T4 actual solo es
  elegible cuando coinciden el final acústico estable y el reloj de palabras.
- `content_reversible`: corregir el texto dentro de líneas existentes sin
  alterar tiempos, cantidad de líneas ni orden. Requiere dos familias ASR o
  equivalentes independientes en el candidato.
- Insertar, borrar, dividir, unir, ordenar líneas, cambiar estructura o
  modificar un tramo marcado para revisión queda fuera de esta versión.

Cada candidato se reconstruye sobre los segmentos originales. Los campos de
propuesta usados por el editor (`review`, `consensus_suggestion`, etc.) no se
copian a la corrección. Si el audio cambia durante el análisis o la revisión
humana avanza la revisión de segmentos, el resultado automático se descarta.

## Habilitación futura

La ruta está apagada por defecto. El modo `TRANSCRIPTION_QUALITY_MODE` no la
habilita. Para autorizarla hacen falta a la vez una autorización Ed25519
firmada y los switches por acción:

- `LYRIC_AUTO_REPAIR_ENABLED=1`
- `LYRIC_AUTO_REPAIR_TIMING_ENABLED=1` para timing
- `LYRIC_AUTO_REPAIR_CONTENT_ENABLED=1` para letra
- `LYRIC_AUTO_REPAIR_AUTHORIZATION_PATH=/ruta/auto-repair-authorization.json`
- `LYRIC_AUTO_REPAIR_AUTHORIZATION_SHA256=<sha256 del archivo exacto>`
- `LYRIC_AUTO_REPAIR_PUBLIC_KEYS={"key-id":"<clave pública Ed25519 base64>"}`

No se debe generar ni habilitar una autorización con las ocho canciones
revisadas hoy: esas ediciones sirven como casos de reproducción, no como
verdad de referencia independiente. La autorización debe cubrir corpus
etiquetado a ciegas, negativos que el motor dejó intactos, cero errores
catastróficos y al menos 539 acciones revisadas por acción con límite inferior
unilateral de Wilson del 95 % de precisión de 0,995 o más. También fija dos
familias independientes como mínimo para la autorización y el candidato.

El JSON firmado debe tener esta forma (valores ilustrativos, no una
autorización válida):

```json
{
  "kind": "lyrics_auto_repair_authorization",
  "schema": "lyrics-auto-repair-authorization-v1",
  "decision": "GO",
  "pipeline_release": "<release exacta>",
  "pipeline_config_fingerprint": "<fingerprint exacto>",
  "expires_at": "<fecha ISO-8601 con zona horaria>",
  "actions": {
    "timing_reversible": {
      "enabled": true,
      "reviewed": 539,
      "precision_lower_95": 0.995,
      "catastrophic": 0,
      "minimum_independent_source_families": 2
    },
    "content_reversible": {
      "enabled": true,
      "reviewed": 539,
      "precision_lower_95": 0.995,
      "catastrophic": 0,
      "minimum_independent_source_families": 2
    }
  },
  "attestation": {
    "algorithm": "Ed25519",
    "key_id": "<clave aprobada>",
    "payload_sha256": "<hash calculado al firmar>",
    "signature": "<firma Ed25519>"
  }
}
```

El artefacto expira, queda ligado a release/config y se vuelve inválido si
cambia cualquiera de los modelos, umbrales o switches incluidos en el
fingerprint. La ruta de autorización y su hash no forman parte de ese
fingerprint para evitar una dependencia circular.

## Experiencia y auditoría

El caso normal no agrega un paso al usuario. El worker aplica únicamente la
corrección aceptada y entrega el resto de la canción; las dudas locales siguen
como propuestas de revisión actuales. El registro persistido no copia letra ni
propuestas: guarda estado, cantidad y acciones aplicadas, hashes del snapshot
y revisiones de audio/segmentos. Una propuesta no mejora el puntaje y reduce
al menos una ventana insegura, se revierte en memoria.

Por ahora el panel del editor no muestra un control de deshacer específico para
esta corrección. La mutación permanece desactivada hasta que exista la
autorización calibrada y la UX de deshacer pueda integrarse en el editor con su
responsable actual.
