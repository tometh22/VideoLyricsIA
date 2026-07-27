# Comunicación a clientes durante un outage

> Plantillas listas para copy-paste. Cada minuto que pasa sin comunicación
> es un cliente convencido de que el problema es suyo, no nuestro. Mandar
> el mensaje PROACTIVO antes que se queje el primero.

## Cuándo mandar cada mensaje

| Síntoma | Mensaje | Canal |
|---|---|---|
| `api.genly-ai.up.railway.app/health` devuelve 404 + `x-railway-fallback: true` | **Plantilla A** (infra Railway) | Slack / WhatsApp / email a operadores activos |
| `api/health` responde 503 o 500 sostenido | **Plantilla B** (problema nuestro) | Mismo + status page |
| Jobs stuck en `processing` > 30 min sin causa | **Plantilla C** (recovery en curso) | Solo si el cliente preguntó |
| Outage resuelto, dar tranquilidad | **Plantilla D** (resolución) | Mismo canal donde mandaste A/B |

## Plantilla A — Outage de infraestructura (Railway)

> **Asunto:** GenLy AI — interrupción temporal del servicio
>
> Hola, te aviso que estamos experimentando una interrupción del proveedor
> de infraestructura (Railway, en la región us-east4). El equipo está al
> tanto y monitorea la recuperación.
>
> **Qué significa para tu trabajo:**
> - Tus videos en proceso están seguros — el sistema retoma automáticamente
>   cuando el servicio vuelva.
> - Los videos ya aprobados y descargados no se ven afectados.
> - No hace falta que hagas nada. Si un job quedara stuck por más de 30
>   minutos después de la recuperación, te avisamos para reprocesarlo.
>
> Tiempo estimado de recuperación: estos incidentes históricamente duran
> entre 15 min y 1 hora.
>
> Te aviso en cuanto vuelva. Disculpas por la fricción.
>
> Tomas

## Plantilla B — Problema nuestro (interno)

> **Asunto:** GenLy AI — issue detectado en el servicio
>
> Hola, detectamos un problema en el servicio que estamos resolviendo
> ahora. Es un bug nuestro (no infraestructura externa).
>
> **Estado actual:**
> - [breve descripción técnica, 1 línea: "el endpoint de creación de
>   variantes está devolviendo error 500"]
> - Tu trabajo previo (jobs aprobados, audios subidos) no se vio afectado.
> - Estimado de fix: [rango realista, ej "30-45 min"]
>
> Te aviso apenas esté resuelto. Si necesitás algo urgente, escribime
> directo.
>
> Tomas

## Plantilla C — Recovery en curso (post-outage)

> Hola, ya volvió el servicio. Estoy revisando que tus jobs hayan
> retomado bien. Si ves alguno "en procesamiento" hace más de 15 minutos,
> avisame el ID y lo reprocesamos al instante. Para el resto, no hace
> falta hacer nada.
>
> Tomas

## Plantilla D — Outage resuelto

> El servicio volvió a estar 100% operativo desde las [hora]. Causa raíz:
> [breve, 1 línea: "interrupción en Railway us-east4" o "bug en el flujo
> de retry"]. Lo que hicimos para que no vuelva a pasar: [1 línea
> concreta].
>
> Si notás algo raro en algún job, mandame el ID. Gracias por la
> paciencia.
>
> Tomas

## Checklist post-incidente

Después de cada outage > 15 min:

- [ ] Verificar que todos los jobs en `processing` se hayan recuperado
      (query: `SELECT job_id, current_step, last_progress_at FROM jobs
      WHERE status='processing' AND last_progress_at < NOW() - INTERVAL '15 minutes'`)
- [ ] Forzar `/admin/runbook/reaper-now` para recuperar huérfanos manualmente
- [ ] Mandar plantilla D al canal donde mandaste A o B
- [ ] Loggear en `docs/INCIDENT_LOG.md` (crear si no existe): fecha, duración,
      causa raíz, jobs afectados, acción correctiva
- [ ] Si fue Railway → considerar si justifica migrar/duplicar región (ver
      DEPLOY_RESILIENCE.md sección "Roadmap multi-región")
