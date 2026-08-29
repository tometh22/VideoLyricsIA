# GenLy — control humano y mejora medible de letras sincronizadas

GenLy convirtió 65 entregas aprobadas por UMG en un corpus histórico auditable:
cada canción conserva el audio verificado, la salida pre-humana disponible, las
versiones intermedias y la entrega aprobada. Esto permite medir las mejoras del
motor contra decisiones humanas reales, no contra una métrica autorreferencial.

## Línea de base certificada

- 65 entregas humanas aprobadas; 41 conservan crudo histórico exacto o
  reconstruido y 57 permiten comparación histórica de texto.
- En las 41 más confiables, WER de la salida pre-humana: **5,78%** (CI95 por
  canción: **3,52–8,42%**).
- Error p90 de final de línea: **750 ms**, hoy el frente principal de mejora.
- Todas las evaluaciones separan estudio/vivo, idioma y calidad del crudo; no se
  mezclan baselines incompatibles.

## Flujo de calidad

El circuito objetivo es: **motor → sugerencias revisables → render → preflight
automático → entrega → feedback de QC del sello**. Las propuestas nunca alteran
una línea bloqueada ni reemplazan la decisión del operador. Cada aceptación,
rechazo y ajuste manual queda como dato de calibración para la siguiente
iteración.

Medimos dos resultados de negocio:

1. minutos de corrección humana por canción, antes y después;
2. hallazgos del QC del sello por entrega, con objetivo de cero.

## Disciplina de despliegue

Ningún modelo pasa a autocorrección por una demo. Los candidatos se validan con
leave-one-song-out y bootstrap por canción. Una categoría solo puede automatizarse
cuando sostenga precisión demostrada en operación; mientras tanto aparece como
sugerencia de un clic o el sistema se abstiene.

## Próximo hito compartido

Reportar por lote: minutos por tema, sugerencias mostradas/aceptadas/rechazadas
por tipo, y hallazgos del QC posterior. El corpus aprobado permite que cada
mejora nueva se compare con la misma referencia humana y que un resultado
negativo se cierre sin poner entregas en riesgo.

> Documento de producto. No implica autorización para entrenar modelos con
> material UMG; esa decisión contractual se trata por separado.
