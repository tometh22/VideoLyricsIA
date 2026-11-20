# Coordinación del equipo

La implementación agrega **Mi equipo** (`/team`) y **Mis tareas** (`/my-tasks`) al menú de producción cuando están habilitadas las campañas. Conserva la revisión de letra/timing, los bloqueos del editor y la aprobación final existentes.

## Activación

1. Aplicar la migración Alembic `e3f621bbc22f` antes de iniciar la versión nueva del backend (`alembic upgrade head`, con la conexión del entorno correspondiente).
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

## Dar acceso a campañas a usuarios existentes

Agus (con rol administrador) entra a **Administración → Gestión → Usuarios**, busca al operador y abre **Acceso a campañas** debajo de su nombre (también disponible en **⋯**). Puede buscar por campaña o equipo y usar **Dar acceso**. Para revocar, usa **Quitar acceso → Confirmar revocación**. El cambio queda guardado inmediatamente y auditado; el operador puede recargar Campañas.

El permiso permite ver y revisar esa campaña de letras; no mueve al usuario de equipo ni le da administración. Los accesos por pertenecer al equipo o por ser administrador se indican como heredados y no se pueden quitar desde este panel. Un permiso adicional redundante sí se puede quitar, sin quitar el acceso heredado. Las campañas cerradas con permisos adicionales siguen visibles para poder revocarlos.

Se conservan las habilitaciones existentes: `BATCH_CAMPAIGN_ENABLED` y el equipo/grupo autorizado en `BATCH_CAMPAIGN_SCOPES`. Si la cuenta está inactiva o su equipo no tiene campañas habilitadas, el panel lo informa y bloquea nuevas altas; permite revocaciones. No cambia flags ni habilita otros equipos implícitamente.

**Acceso no es asignación de trabajo.** Para asignar responsables y ver avance se usa Mi equipo; sus miembros siguen siendo usuarios activos del mismo equipo.
