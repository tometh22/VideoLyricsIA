# Muestra de revisión acústica de Agus

Las versiones guardadas por Agus sirven para ubicar tramos que merecen escucha.
El extractor compara cada versión con el checkpoint inicial de la máquina solo
cuando la evidencia de máquina, el audio y la revisión vigente coinciden. No
declara correcta la versión de Agus ni cuenta sus cambios como calibración.

## Extracción privada

Ejecutar `scripts/build_operator_calibration_queue.py` en un entorno confiable
con acceso de lectura a la base de staging y `QUALITY_LEARNING_HMAC_KEY`. Por
defecto solo imprime un resumen agregado. Para guardar la cola privada:

```sh
python3.11 lyricgen/backend/scripts/build_operator_calibration_queue.py \
  --operator-id 62 --tenant universal_music \
  --write --output-dir .context/agus-calibration
```

La transacción es `REPEATABLE READ, READ ONLY`. Los tres archivos resultantes
son exclusivos, con permisos `0600`, dentro de `.context`:

- `blind_queue.jsonl`: trabajo, línea, recorte de audio, tipo de tarea, grupo y
  partición. No contiene letra ni la respuesta de la máquina o de Agus.
- `sealed_provenance.jsonl`: vínculo privado a versiones, revisiones, identidad
  del audio y tiempos comparados, para cotejar después de la escucha.
- `summary.jsonl`: cantidades y motivos de exclusión, sin IDs de canciones.

Para preparar una sesión de escucha, el comando siguiente vuelve a verificar
la identidad del audio en una transacción de lectura y firma enlaces GET que
vencen en cuatro horas. El archivo y la página contienen enlaces privados:

```sh
python3.11 lyricgen/backend/scripts/sign_operator_calibration_audio.py \
  --queue .context/agus-calibration/blind_queue.jsonl \
  --provenance .context/agus-calibration/sealed_provenance.jsonl \
  --output .context/agus-calibration/audio_urls.json
python3.11 lyricgen/backend/scripts/build_operator_calibration_preview.py \
  --queue .context/agus-calibration/blind_queue.jsonl \
  --audio-urls .context/agus-calibration/audio_urls.json \
  --output .context/agus-calibration/reviewer/index.html
python3.11 lyricgen/backend/scripts/serve_reviewer_shadow_preview.py \
  --directory .context/agus-calibration/reviewer --port 8767
```

El servidor escucha solo en `127.0.0.1` y expone únicamente la carpeta del
revisor, sin el archivo de procedencia. La página tampoco expone el tipo de
cambio, si un recorte es control, el modelo, el artista o la partición. Guarda
las respuestas en el navegador y permite descargarlas. Estas respuestas son
borradores privados:
todavía no equivalen a etiquetas adjudicadas ni a una aprobación del motor.

Un audio reemplazado, un checkpoint de máquina sin validar o una versión de
Agus ya superada quedan fuera de la cola. Los cambios estructurales se
convierten en recortes ciegos `structure_diagnosis` tomados de la versión
editada: no se emparejan con una línea de máquina ni se usan para medir deltas
o autorizar acciones. Cuando la máquina no guardó IDs de línea y el editor los
agregó después, solo se admiten pares con igual cantidad de líneas. Se marcan
`index_diagnostic`: el índice sirve para localizar el recorte, pero no prueba
que sea la misma ocurrencia ni autoriza una etiqueta automática. Se agregan
hasta cuatro líneas intactas por canción como
controles. Las frases repetidas se señalan para muestreo difícil; todas las
canciones del mismo artista o grabación quedan en la misma partición. Las líneas
que el operador bloqueó siguen disponibles para escuchar y se marcan
`human_protected`; el motor no puede modificarlas automáticamente.

## Escucha y etiquetas

La interfaz de revisión debe abrir el recorte sin mostrar la letra ni los
tiempos de ninguna de las dos versiones. El revisor marca lo que oye, la
ocurrencia de la frase y los intervalos aceptables de inicio y fin. Puede
responder **ambiguo**. Solo después de guardar esa primera respuesta se
pueden revelar las versiones para diagnóstico. Un segundo revisor verifica de
forma independiente las posibles aplicaciones automáticas y los controles;
las discrepancias se adjudican sin copiar la respuesta de Agus.

Para finales de línea, las etiquetas adjudicadas se convierten al contrato
`timing-endpoint-gold-v1`, medido sobre el video renderizado. Las tareas de
letra se contrastan con transcripciones ciegas independientes y la
clasificación lexical/vocalización existente en `corpus.py`. Un cambio de
letra junto con cambio de tiempo se registra como diagnóstico conjunto y no
certifica ninguna acción local reversible por sí solo.

## Decisión de motor

Primero se prueban candidatos del motor en modo sombra contra etiquetas
independientes, incluyendo líneas intactas y coros repetidos. La partición de
prueba permanece cerrada hasta fijar modelo, umbrales y selección. El informe
separa letra de timing, errores por canción y daños a controles. La política
de aplicación exige, para cada acción, al menos 539 aplicaciones revisadas,
límite inferior unilateral del 95 % de precisión de 0,995, cero errores
catastróficos y dos familias de evidencia independientes. Una cola, una
anotación o un informe offline jamás habilitan el switch de runtime ni firman
una autorización.

La revisión normal del editor debe seguir concentrada en los pocos tramos que
el motor no pueda resolver. Las correcciones futuras conservarán el snapshot
original y tendrán una reversión visible antes de activar un canario.
