# Recuperación de borrador: copia vacía y elección comprensible

La captura muestra LocalDraftRecovery de la rama fix/editor-visible-times.
El diálogo compara el documento remoto con una copia del navegador. No indica
por sí mismo que el video o la letra guardada se hayan perdido.

Causa reproducida: `[].every(...)` devolvía true y una copia sin líneas podía
entrar como versión distinta y válida. La UI imprimía toda la letra remota en
un pre, la copia vacía no mostraba explicación y los botones quedaban debajo
de un scroll largo. No se inspeccionó el localStorage de Agus: el origen exacto
de su copia no puede determinarse por la imagen.

Cambios:

- Copias vacías sin base válida se apartan como incompatibles. Se conservan
  bytes en :incompatible y se abre el documento remoto sin mutación de servidor.
- Copias vacías con base válida conservan la recuperación de una eliminación
  intencional. La UI dice 0 líneas y exige confirmar la eliminación antes de
  habilitar Usar borrador local. No se restaura automáticamente.
- Usar Genly archiva primero la copia en :discarded y elimina sólo el borrador
  activo; mantiene la comprobación de que otra pestaña no lo haya cambiado.
- Diálogo modal nativo con foco/Escape, cabecera y acciones fuera del scroll,
  resumen de ambas copias, diferencias acotadas, tiempos legibles e información
  de soporte plegada. Las revisiones técnicas dejan de ser la explicación principal.
- Se explica que usar el borrador inicia una nueva edición que se guarda;
  no se promete una vista previa sin guardado cuando el autosave está habilitado.

Verificación: 28 pruebas de recuperación/persistencia/undo; Chromium con
100 líneas a 1280×720 y390×844, acciones visibles aun expandiendo todas las
diferencias, confirmación explícita para copia vacía. Build e i18n verificados.
Sin acceso al navegador de Agus, merge ni deploy.

## Por qué antes podía no aparecer (historial confirmado)

- c58fd35c, 11-sep-2026: reemplazó recuperación/merge silenciosos por elección
  explícita cuando el contenido local difiere del remoto.
- 322e0800, 16-sep-2026: copias incompatibles pasan a respaldo :incompatible y
  apertura de la versión remota sin bloquear.
- 71f0e529, 6-nov-2026: quitó `draft.segments.length > 0` de la validación para
  conservar una eliminación intencional de todas las filas tras un fallo de
  guardado. Desde ese cambio, [] también entra en la comparación y dispara el
  diálogo frente a una letra no vacía, aunque ambos indiquen la misma revisión.

Estas son fechas de commits; no se verificaron fechas de despliegue.
La captura tiene revisión remota y base local 273: no demuestra una revisión
remota concurrente ni pérdida de datos. Puede ser ruido por un snapshot vacío,
o una eliminación pendiente real. `base_segments` permite comparar, pero no
prueba por sí solo intención humana. Para clasificar el caso exacto hace falta
la copia original y el enlace/ID del video para correlacionar historial/eventos.

## Actualización: investigación de los pedidos recientes

Se identificó el video de la captura y se reprodujo la creación de un borrador
vacío durante la liberación del store tras aprobar/salir. La copia generada puede
tener base válida; el filtro de incompatibles por sí solo no resuelve esa causa.
Se agregó el guard de ciclo de vida y se validó que siguen recuperándose las
eliminaciones intencionales y los cambios pendientes al desmontar.
Ver [investigación y alcance de la evidencia](DRAFT_RECOVERY_INCIDENT_2026-11-10.md).

## Entrega 1.1.166

Destino autorizado: staging. Responsable de versión, merge, despliegue y
verificación: agente principal de esta sesión. Incluye los tiempos visibles
y el ajuste acotado de ancho de PR1572; no incluye experimentos de motor.

Base previa: 3cba59f8ccd713205e790e716ec1249632e5f290. Vercel previo:
`genly-r14xcgzjc-tometh22s-projects.vercel.app` (READY, preview).
Sin migraciones ni cambios de configuración/worker. Esperar CI completo del
head final antes de fusionar. Verificar alias staging, versión/commit del
frontend y readiness de API después del deploy automático.

Rollback: crear una rama desde origin/staging, ejecutar
`git revert -m 1 <merge_sha_de_PR1572>` si se usa merge commit (sin `-m` si se
usa squash), pasar CI y fusionar el revert por PR protegido. Registrar el SHA
exacto de merge en la evidencia de despliegue antes de cerrar la entrega.
No resetear ramas compartidas ni deshacer letras de clientes.
