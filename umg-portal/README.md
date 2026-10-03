# Portal de entregables UMG

Build estático compartido por `umg.genly.pro` y `umgchile.genly.pro`.
El dominio determina el país; la API valida el token y el destino de cada fila.

Todos los videos publicados ofrecen **Generar y descargar** en ProRes,
incluidos lyrics y art tracks históricos sin master guardado. Un click prepara
el archivo, muestra **Preparando…** y descarga automáticamente al terminar.
Los masters existentes se descargan directamente. La primera preparación puede
tardar minutos según la duración y la cola; no se generan masters al abrir el
portal ni al publicar una campaña.

La generación usa el MP4 de la última versión publicada en ese portal,
preserva resolución/FPS y exporta ProRes 422 HQ con audio PCM 24 bits/48 kHz
estéreo. No usa archivos temporales ni masters de un Job que pueda contener
correcciones sin publicar. Una corrección publicada durante la preparación
cambia la identidad de la descarga y el portal sigue la nueva versión. Las publicaciones con un puntero al último render conservan ese modo: el caché se vincula al ETag del MP4, sin fijar una copia que deje de seguir correcciones.

Las tres rutas del flujo usan el mismo backend `/api/deliveries/`:

- `POST /{id}/download/{file_type}` verifica el archivo vigente y entrega una
  URL temporal, o indica que necesita preparación.
- `POST /{id}/prepare-prores` encola una exportación de la fuente publicada;
  deduplica solicitudes simultáneas y permite reintentar fallos.
- `GET /{id}/prepare-prores` consulta estado sin iniciar un render.

Los archivos grandes se transfieren directamente desde R2. El worker elimina
sus temporales locales; el master queda como caché en R2 y se puede regenerar
si expira. Este cambio no modifica políticas de retención ni borra material.

Para generar el shell, ejecutar `gen_page.py` con `DELIVERY_PASSWORD` o
`PORTAL_SHELL_SOURCE` apuntando al HTML vigente para conservar su acceso.
No guardar contraseñas ni `dist/` en Git. El template se incorporó desde la
versión existente del portal en `origin/main`; los cambios de esta rama afectan
el recorrido de descarga. El proyecto Vercel existente tiene su directorio
local en `/Users/tomi/genly-deliveries`.

Desplegar primero API y workers que incluyan `portal_prores.py`, verificar el
flujo en el entorno de prueba y después publicar el shell. Requiere ambos
deploys; cambiar sólo el HTML no habilita el backend. No necesita migraciones.

Validación de JavaScript: `node --test umg-portal/tests/download.test.cjs`.
