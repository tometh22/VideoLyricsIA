# Coordinación del equipo

La implementación agrega **Mi equipo** (`/team`) y **Mis tareas** (`/my-tasks`) al menú de producción cuando están habilitadas las campañas. Conserva la revisión de letra/timing, los bloqueos del editor y la aprobación final existentes.

## Activación

1. Aplicar la migración Alembic `b8c0d2e4f6a8` antes de iniciar la versión nueva del backend (`alembic upgrade head`, con la conexión del entorno correspondiente).
2. Mantener habilitado `BATCH_CAMPAIGN_ENABLED` y autorizar el tenant en `BATCH_CAMPAIGN_SCOPES`, como para las campañas actuales.
3. Un administrador entra a **Mi equipo**, selecciona el tenant correcto y activa **Puede coordinar este equipo** en el usuario de Agus. No se asigna por nombre ni se convierte a Agus en administradora. Recargar su sesión/página para actualizar el menú.
4. Los operadores deben ser usuarios activos del mismo tenant. Un usuario común solo consulta sus asignaciones; la coordinación consulta y reparte todo el equipo. El administrador puede seleccionar otros tenants.

Los equipos coinciden con tenants, no con billing groups. No se comparten tareas entre Argentina y Chile por pertenecer a una cuenta de facturación común.

## Comportamiento

- Asignación de videos seleccionados o de los videos actuales de una campaña (hasta 1000 por transacción). Incluye responsable, prioridad y fecha; permite dejar sin asignar. Los videos nuevos que ingresen después no heredan responsable automáticamente.
- Excluye aprobados/descartados y campañas cerradas al guardar; informa cuántos se omitieron.
- Guarda notas, inicio de tarea y motivo de bloqueo. La nota y el bloqueo se conservan al reasignar para no perder contexto.
- La asignación no impide que otro miembro autorizado por los permisos existentes revise la campaña. No modifica ni elude los locks del editor.
- Los estados técnicos se derivan de `Job`: procesamiento automático separado del trabajo humano, revisión final para `pending_review`, aprobado solo para `done`. Los descartados no entran al denominador del avance.
- Dashboard y bandeja se actualizan cada 30 segundos cuando la página está visible y no hay un diálogo abierto. La tabla tiene filtros y páginas de 50 videos. Las métricas abarcan todo el equipo (o mis tareas), no solo la página/los filtros de la tabla.
- **Abrir revisión de letra** y **Abrir video** llevan a las pantallas actuales; las aprobaciones siguen pasando por sus validaciones reales.

## Persistencia y concurrencia

`team_coordinators` almacena permisos por tenant/usuario; `team_assignments` guarda coordinación por item y versión; `team_activity` registra autor y fecha de asignaciones, notas, bloqueos y cambios de permisos. El historial de aprobaciones sigue en los mecanismos existentes.

Las escrituras verifican tenant, pertenencia activa del destinatario, permiso y versión esperada. Los locks del item serializan incluso la primera asignación concurrente. Un conflicto devuelve 409 y revierte todo el lote. Cerrar el diálogo y reabrirlo tras actualizar permite revisar los valores nuevos antes de reintentar.

## Verificación

- Backend: `python -m pytest tests/team -q` desde `lyricgen/backend` con una base de pruebas aislada. La prueba de concurrencia real requiere PostgreSQL.
- UI: `npm test -- --run src/components/TeamPage.test.jsx src/lib/reviewerNavigation.test.js`.
- Navegador: `npx playwright test e2e/team-coordination.spec.js --workers=1` (shell real, API de prueba, escritorio/móvil).
- `npm run check` y `npm run build`.

La demo aprobada permanece en `/team-prototype.html` con `npm run dev`; sus datos son ficticios y no se envían a la API. La entrada de demo no se agrega al build de producción.
