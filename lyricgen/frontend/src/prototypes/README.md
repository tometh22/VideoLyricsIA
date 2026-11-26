# Prototipo de coordinación de equipo

Abrir `/team-prototype.html` con `npm run dev`. Entrada independiente para revisar el flujo sin login, API ni modificaciones a datos reales. Usa React, el CSS/tokens de Genly y `GenlyLogo` existentes. Estado solo en memoria; recargar o «Restablecer demo» reinicia los ejemplos. No es una implementación de autorización.

## Revisión del producto actual

- `components/CampaignsPage.jsx`: campañas, filtros, cola de revisión, revisadas por mí, atribución de aprobaciones y bloqueos de revisión simultánea.
- `components/Dashboard.jsx`: dashboard operativo centrado en producción y aprobación.
- `backend/database.py`: `BatchCampaign` delimita tenant y creador; `BatchCampaignItem` representa cada fuente, pero no tiene responsable asignado ni fecha de entrega. `User` tiene roles user/admin.
- `backend/batch_campaigns.py`: acceso delimitado por tenant; gestión por administrador o creador de la campaña (`_require_manager`).

## Recorrido

1. Mi equipo: carga por operador, avance por campaña y tabla filtrable.
2. Seleccionar videos o «Asignar campaña»: confirmar responsable, prioridad y entrega. Excluye aprobados; reasigna los restantes explícitamente.
3. Mis tareas: cambiar operador de ejemplo, iniciar trabajo, informar bloqueo con motivo o enviar a revisión final.
4. Mi equipo: revisar una tarea y aprobar o pedir correcciones. Métricas calculadas desde los mismos datos de la tabla.

Los estados de coordinación son ilustrativos: no sustituyen el estado técnico de transcripción/render. En el producto real, iniciar una tarea debe abrir el editor actual; enviar a revisión y aprobar deben derivarse de su flujo real.

## Pendiente para implementación real

Responsables persistentes por campaña/video, pertenencia al equipo y permisos de coordinación del lado servidor; asignación masiva transaccional con control de conflictos; fechas y prioridades; historial de asignaciones/actividad; estadísticas con alcance de tenant/equipo y estados existentes. Definir si una campaña asignada hereda responsable en videos nuevos y quién puede aprobar. No usar los controles de simulación como controles de permisos.
