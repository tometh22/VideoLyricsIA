# Entrenamiento con audio UMG: decisión de uso y egreso

Estado: **no autorizado / no ejecutado**. Este análisis no es asesoramiento
legal; enumera las diferencias técnicas y contractuales que deben quedar por
escrito antes de habilitar el flag.

## Qué se preparó

El paquete local contiene 498 recortes de hasta 25 segundos derivados de 65
entregas, con audio, texto aprobado, idioma, artista y split por canción. Nada
de ese paquete se subió. Los paths locales no se incluyen en artefactos
publicados y ninguna credencial se serializa.

## Por qué no equivale a la separación existente

El flujo actual envía audio al proveedor de separación para una inferencia
puntual y recibe un stem. Un entrenamiento externo en RunPod agregaría:

- persistencia de cientos de recortes y letras durante múltiples épocas;
- checkpoints, logs, caches y un adaptador derivado del catálogo;
- posible almacenamiento temporal fuera de la región acordada;
- una nueva finalidad: mejorar un modelo, no producir una entrega individual.

Aunque se entrenara enteramente en esta Mac y no hubiera egreso, seguiría
existiendo una decisión distinta: autorización para usar material del cliente
como datos de entrenamiento. La política publicada actualmente afirma que
GenLy no hace fine-tuning con datos del cliente, por lo que “local” no elimina
esa contradicción.

## Condiciones mínimas para habilitarlo

1. Confirmación contractual de que audio, letras y timings pueden usarse para
   mejora interna del motor y definición de si se admite entrenamiento por
   cliente o solo un modelo común.
2. DPA y alta de RunPod (o del proveedor elegido) como subprocesador, con
   región, cifrado, controles de acceso y notificación de incidentes.
3. Retención máxima, borrado verificable de datasets/caches/checkpoints y
   exclusión explícita de entrenamiento por parte del proveedor.
4. Titularidad y uso permitido del adaptador derivado; portabilidad y borrado
   al terminar el contrato.
5. Registro de versión del dataset, canciones incluidas/excluidas, finalidad,
   responsable que autorizó y fecha de vencimiento de la autorización.

## Recomendación cerrada

**No habilitar todavía el entrenamiento con las 498 muestras UMG.** Autorizar
solo cuando las cinco condiciones anteriores estén documentadas. Mientras
tanto, validar el ejecutor LoRA con audio de investigación y mantener el
paquete UMG detrás de `ALLOW_UMG_TRAINING=0`. El usuario deberá decidir después
por separado si permite entrenamiento local y si permite egreso a GPU externa.
