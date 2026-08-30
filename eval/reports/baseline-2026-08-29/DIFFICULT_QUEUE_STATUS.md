# Cola difícil — estado 2026-08-30

**WER cola difícil: 17,3% → pendiente del replay pesado.**

**Minutos/canción: fáciles pendientes del timer por cohorte; difíciles 20–30
min → pendiente.** El valor histórico de 127,1 s era una proyección de cola de
revisión sin separar dificultad, no un timer real, y no se reutiliza como si lo
fuera.

## Implementado y validado localmente

- Cohorte congelada: 12 difíciles y 29 fáciles entre las 41 canciones con
  crudo exacto o reconstruido. Las difíciles concentran WER 17,3%; las fáciles,
  2,0%.
- LID por canción completa y chunk: Whisper detect, comparación de
  log-probabilidad forzada es/en, confirmación léxica y persistencia temporal.
  El mix usado por falta de stem queda marcado como fallback e incertidumbre.
- Tres variantes de code-switch medidas: idioma propio por ventana, auto-LID
  por ventana y prompt bilingüe de canción completa.
- Enrutador previo a la transcripción con `LeaveOneGroupOut`: actividad vocal,
  onsets, articulaciones de pitch, tempo, relación voz/mix y señales LID. Vivo,
  LID mixto o faltante siempre enrutan pesado. También se midió un probe base
  mix/stem con confianza, repetición y desacuerdo.
- Pipeline pesado acotado a cinco pasadas: MSS-ALT + Whisper original, slow
  20%, pitch −3 y code-switch cuando aplica. Las TTA son una sola familia;
  Gemini es testigo independiente opcional y nunca juez de sí mismo. Sin ese
  testigo, la selección conserva exactamente el baseline de producción.
- Resolver de vocalizaciones como propuesta: solo sobre ventanas ya
  clasificadas como vocalización, ≥0,75 s, paréntesis, articulaciones de pitch y
  abstención si la vocal es incierta. Una línea léxica no puede convertirse en
  vocalización. La extensión de melisma exige continuidad y ±2 semitonos, y no
  cruza la siguiente línea.
- Reporte de media página cuya primera línea es WER difícil y la segunda es
  minutos fácil/difícil. Resultados ausentes quedan `PENDING/BLOCKED`.

77 tests locales pasan. Staging no fue modificado: la regla del bloque exige
replay completo antes de preparar sugerencias.

## Resultado Spanglish

El LID final detectó solo `Runaway` entre las 41 comparables y produjo cero
falsos positivos contra las etiquetas humanas. Para llegar ahí fue necesario
vetar boilerplate/repetición y confirmar los candidatos mixtos con una
transcripción no forzada de contexto completo. El mix sin stem nunca puede
activar por sí solo el decoder code-switch.

Las tres variantes ASR quedaron `NO_GO` en el piloto: WER main 6,74% baseline
frente a 193,26% por idioma/ventana, 125,84% auto por ventana y 153,93% con
prompt bilingüe de canción completa. El patrón dominante fue repetición
alucinada. Ninguna variante pasa a staging; la señal LID solo puede enrutar a
un pipeline pesado que conserve el baseline si sus candidatos no verifican.

## Resultado del enrutador

El clasificador acústico previo quedó `NO_GO`: AUC 0,428; para llegar a recall
95% debía enrutar prácticamente todo (40/41 held-song) y aun así obtuvo recall
91,7%. Agregar un probe Whisper-base mix/stem empeoró AUC a 0,333 y no cambió
la cobertura. La infraestructura queda, pero no decide gasto en producción.
El fallback seguro es correr el baseline y decidir un segundo pase con señales
post-ASR; el predictor de líneas existente llega a AUC 0,790, aunque a recall
95% todavía enruta demasiadas canciones. Hasta mejorar ese selector, el replay
pesado se mide sobre la cola difícil conocida, separado del gate del router.

El smoke end-to-end del pipeline pesado también encontró un fallo antes de que
llegara a staging: sobre `Hombre Lobo`, el medoid TTA base elegía una salida de
WER 90% frente al baseline 16,25%. El fallback nuevo lo neutraliza: sin acuerdo
Gemini independiente, el resultado final queda 16,25% → 16,25%. Esto prueba
ejecución y no-regresión, no mejora; el replay con modelo productivo sigue
pendiente de los stems y de la segunda familia.

## Resultado de vocalizaciones

El replay usó las 34 ventanas prehumanas de Bersuit/La Renga y el CSV humano
completo. Había 4 vocalizaciones editoriales positivas. El gate previo marcó
las 34 como `ambiguous_pitched_voice`/`ambiguous`, así que el resolver se
abstuvo: 0 propuestas, 0 inventadas, 0/4 pre-resueltas. Estado `NO_GO` por
recall. No se promueve automáticamente `pitched_voice` a vocalización porque en
el mismo conjunto hay público y regiones sin letra con features similares.

## Gates todavía abiertos

- El gold solo contiene una canción es/en confirmada (`Runaway`, Los Pericos).
  El piloto actual ya es negativo; además, cualquier futura afirmación positiva
  de mejora ≥30% requiere al menos dos canciones humanas adicionales.
- Faltan 15 stems completos. El bundle determinista ya existe, pero RunPod
  sigue sin credencial privada inyectada. Después de `runpodctl doctor` y
  `listo`: importar stems, correr router, MSS-ALT completo y pipeline pesado.
- El gate pesado exige mejora relativa de WER ≥25% sobre las 12 difíciles, con
  CI95 por canción positivo. Vivo permanece Tier 2 incluso si pasa.
- Vocalizaciones exigen ≥60% pre-resueltas y cero propuestas sobre letra real;
  faltan las ventanas prehumanas del gate de contenido para medirlo sin fuga.
- Los minutos antes/después deben salir del timer real del revisor, separados
  por cohorte. La proyección por ratio de WER queda etiquetada como proyección.

## Única acción externa pendiente

En Terminal, sin pegar la clave en el chat:

```zsh
/Users/tomi/conductor/workspaces/VideoLyricsIA-main/riyadh/.context/bin/runpodctl doctor
```

Después responder `listo`.
