# Plan de entrega por campaña y cuenta

La vista vive en **Campañas → Cuentas · Plan de entrega** y en la pestaña
**Plan de entrega** de cada campaña de lyric videos. La cuenta es el cliente
del portal (`argentina` / `chile`), nunca el tenant del operador.

## Saldo

El objetivo es 50% Veo y 50% foto fija con efecto. La tabla distingue:

- publicaciones vigentes del portal;
- videos generados que aún no están publicados;
- canciones en generación;
- audios cargados pendientes de generar, con o sin fondo asignado.

Los Art Tracks quedan aparte. Los formatos MP4/ProRes/vertical no multiplican
el conteo. Las variantes son videos distintos; las filas duplicadas de un
mismo job y portal se deduplican. Los descartados y campañas canceladas no
entran en las pendientes. Una selección de cuenta corresponde al acumulado
de ese portal: no implica que cada campaña individual quede también 50/50.

El fondo generado sale de `campaign_render_evidence` y, para trabajos
anteriores, de generaciones exitosas en `AIProvenance`. Los fondos de preview
se resuelven por su clave de caché exacta. `background_ai_generated` **no**
distingue Veo de Imagen. Una versión publicada cuyo fingerprint difiere del
render actual queda sin verificar, con enlace a sus entregables.

Las entregas se leen de `get_deliveries_db`; nunca se suma el espejo histórico
de staging. Para cuentas, `peer_session()` aporta jobs y campañas del otro
entorno. Las campañas remotas son de consulta: no se escriben ni generan
desde el entorno actual. Si falta el peer, o hay fondos generados/publicados
sin verificar, el reparto automático de cuenta queda bloqueado.

## Asociación y períodos

Las campañas nuevas seleccionan el portal tanto para lyrics como para Art
Tracks. Las existentes pueden asociarse desde su plan. Un destino ya guardado
no se puede cambiar por esta pantalla. Las campañas legacy con publicaciones
en un único portal pueden atribuirse a esa cuenta en lectura; las ambiguas se
muestran como pendientes de asociación. No se interpreta el nombre de la
campaña ni el tenant como una asociación contractual.

El filtro mensual usa la primera publicación registrada en la auditoría,
con `added_at` como respaldo y horario local del portal. Los pendientes y
generados sin entregar se filtran por `default_render_params.delivery_plan_period`,
que se asigna explícitamente desde la campaña. Los períodos faltantes se
advierten en la cuenta; **Todo el acumulado** no necesita ese dato.

## Asignación y generación

**Revisar reparto** propone cantidades y canciones concretas para compensar
el saldo. Respeta elecciones fijadas, videos existentes, trabajos en curso y
sesiones abiertas en el editor. Conserva tipografías y ajustes ajenos al fondo;
foto conserva un efecto compatible o usa Bokeh. Los totales impares dejan una
foto adicional y se explican en la vista previa.

**Aplicar asignaciones** revalida el saldo completo, estados y versiones;
bloquea campañas, jobs, items y documentos locales; y guarda todo en una única
transacción. Un cambio desde el preview exige recalcular. Los reintentos son
idempotentes. El objetivo se conserva en `delivery_plan_target` y cada operación
queda en `AuditLog` (`delivery_plan.*` y `campaign.creative.applied`), usando las
columnas JSON existentes; no requiere migración.

**Elegir fondo** permite fijar o liberar una excepción individual usando el
flujo creativo existente con control de revisión.

Generar es una confirmación separada con cantidades por fondo. Sólo se pueden
seleccionar letras aprobadas con fondo explícito. Se usa `/generate`, con las
mismas revisiones y controles de cupo/calidad del flujo existente. El backend
también rechaza fondos implícitos para campañas asociadas a un portal o con
objetivo de entrega. Los fallos y límites dejan los no enviados pendientes.

## Acceso y verificación

El saldo completo de una cuenta requiere administrador. Una campaña mantiene
su autorización por tenant y responsable. Las lecturas no exponen referencias
de letras ni prompts. Las operaciones de planificación no llaman proveedores
ni encolan renders.

Pruebas: `tests/test_delivery_plan.py`, `DeliveryPlan.test.jsx` y
`e2e/delivery-plan.spec.js`, además de las regresiones de campañas y dirección
creativa. El E2E usa APIs simuladas y revisa escritorio/móvil, preview, apply
y confirmación separada de generación.
