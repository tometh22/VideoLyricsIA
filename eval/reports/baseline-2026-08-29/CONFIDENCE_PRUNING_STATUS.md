# Selector de confianza + poda — estado 2026-08-30

**Segundos de revisión/canción: 127,1 → 342,9 en el punto de recall 93%.** El
ensamble evaluado es `NO_GO`: aumentaría el trabajo del revisor.

## Implementado

- Bundle determinista para los 15 stems `mdx_extra` faltantes, sin
  credenciales, con SHA-256 del master de entrada y del stem de salida.
- Importador transaccional: rechaza IDs, hashes, modelo, duración o archivo
  inesperados antes de modificar el cache.
- Telemetría por línea de los dos testigos CTC (global/local), anclas,
  ocurrencias y scores. Una abstención jerárquica es un negativo duro.
- Selector de timing con `LeaveOneGroupOut`, VAD del stem y curvas para
  precisión 90/93/95. Vivo queda excluido de cualquier aprobación automática.
- Dos rankers de flags, texto y timing, también `LeaveOneGroupOut`, con costo
  explícito de 10 s por flag falso y 60 s por corrección perdida.
- Timing aprobado por el selector sale sólo de la fuente de timing; jamás
  borra un flag de texto pendiente.
- Gate MSS-ALT pareado: CI95 de mejora relativa y veto a cualquier canción que
  empeore más de 2 puntos absolutos de WER.
- Agregador final que exige cohorte completa. Un MSS ganador debe propagarse
  aguas abajo; un `NO_GO` conserva explícitamente el baseline.
- Runtime local MPS reproducible para MSS-ALT, sin timestamps de palabra que
  no intervienen en WER y con la configuración de decodificación persistida.

## Cohorte completa

Los 15 stems faltantes se produjeron en RunPod, se importaron sólo después de
verificar identidad, hashes y duración, y el cache quedó 41/41. El bundle de
entrada tuvo SHA-256
`fd80406ad3d678625bb41804e651bf22464a18581a448a322b7f05ad253064f0`.
El archivo de resultados verificado tuvo SHA-256
`ea20618fb8e5c19aa2bc84db97b2d37262a483d7beac4c8c5c1c64267bce4926`.
El pod se eliminó al terminar; costo aproximado: USD 0,19.

## Resultados

- Alineador global: 35/41, p50 120 ms, p90 860 ms, `NO_GO` global.
- Jerárquico: 40/41, p50 150 ms, p90 771 ms, `NO_GO` global.
- Selector leave-one-song-out: 31 canciones comparables y 7 abstenciones
  seguras; AUC 0,684. Sólo aprueba 2/1.117 líneas en dos canciones. Su CI por
  canción es 0–100%, por lo que queda `NO_GO_INSUFFICIENT_EVIDENCE`.
- Poda a recall 93,2%: 1.406 líneas, 762 falsos flags y 342,9 s de cola por
  canción. `NO_GO`.
- MSS-ALT: WER 18,80% → 18,07%; mejora relativa 3,88%, CI95 −5,83% a
  +14,45%; 10 canciones regresan más de 2 puntos. En la cola difícil mejora
  sólo 0,8% relativo. `NO_GO`.

Ninguno de estos tres candidatos se promueve. Staging conserva el flujo
anterior.

## Validación local

82 tests pasan. Las abstenciones explícitas cuentan como evaluación completa
pero jamás como permiso de mutación. Un selector cuyo gate falla no puede
quitar líneas de la cola humana.
