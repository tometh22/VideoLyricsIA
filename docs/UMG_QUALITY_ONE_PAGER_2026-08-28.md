# Genly: control verificable de letras y timing

## Resultado ejecutivo

Genly combina transcripción automática, alineación palabra por palabra y una
capa de control editorial que se abstiene cuando el audio no permite sostener
una corrección. El objetivo operativo no es prometer una precisión imposible,
sino reducir el tiempo de edición sin introducir texto que no esté respaldado
por el audio.

En una comparación de mismo audio sobre **10 canciones en español**:

- De **74 eventos que Genly mostraba y Rotor no**, **70 tenían soporte
  acústico** en la auditoría. La discrepancia era principalmente contenido real
  ausente en Rotor, no texto inventado por Genly.
- En la revisión específica de los **15 huecos de estudio** más sospechosos no
  se confirmó ninguna línea léxica sustantiva perdida: hubo **2 fragmentos
  marginales de interjección o contenido mixto** (`oh/no`) y **5 regiones que
  ni un humano pudo juzgar con seguridad**. Esas regiones quedan en abstención,
  no se completan por inferencia.
- El material **en vivo** se enruta obligatoriamente a **Tier 2 con revisión
  humana**. Ninguna sugerencia de letra o timing se aplica automáticamente.

## Cómo se controla la calidad

1. **El audio es el árbitro.** Una diferencia con otro transcriptor no se
   considera error hasta verificar qué se oye realmente.
2. **Texto, timing y política editorial se evalúan por separado.** Ad-libs,
   vocalizaciones, voz hablada, público y repeticiones no se mezclan con errores
   léxicos.
3. **Abstención por defecto.** Si dos fuentes independientes no coinciden o el
   fragmento es ambiguo, el sistema marca revisión en vez de inventar.
4. **Trazabilidad.** Las propuestas conservan versión de política, familias de
   evidencia y huellas criptográficas; una edición humana nunca se sobrescribe.
5. **Calibración antes de automatización.** La aprobación automática permanece
   bloqueada hasta reunir al menos 50 ventanas verificadas en 10 canciones, con
   cero propuestas incorrectas aprobadas y precisión inferior bootstrap por
   canción de al menos 90%.

## Trabajo activo sobre timing

La principal oportunidad en estudio ya no es recuperar letra: es corregir las
fronteras de palabra y de cartel que algunas etapas heredaban del segmentado
ASR. El nuevo T4 separa el reloj fonético del reloj visual y prohíbe mover una
frontera a otra repetición de verso o estribillo sin prueba de identidad. Está
en modo observación y se habilitará sólo si mejora los casos objetivo sin dañar
el conjunto de control.

## Impacto esperado para operación

- Menos tiempo buscando frases que en realidad son reverb, sostenidos o ruido.
- Sugerencias acotadas al segundo exacto y con explicación auditable.
- Corrección humana concentrada en ambigüedades reales, especialmente en vivo.
- Entrega final siempre bajo control editorial, con aprendizaje a partir de las
  decisiones que el equipo ya toma durante el trabajo diario.

---

**Alcance de los números:** piloto interno de 10 canciones; no representa aún
una certificación de precisión sobre todo el catálogo. El resultado 70/74
proviene de triage acústico asistido y se conserva como evidencia de piloto,
no como garantía universal.
