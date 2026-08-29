# Reconciliación WER histórico vs Bersuit

Fecha: 2026-08-29.

## Resultado

No hay una incompatibilidad de normalización. Bersuit es un outlier real y,
además, los dos números describen tareas distintas.

| Medición | Normalizador histórico | Normalizador canónico nuevo |
|---|---:|---:|
| UMG histórico, 57 canciones | 969 / 11.677 = **8,298%** | 965 / 11.677 = **8,264%** |
| Bersuit, Whisper large-v3 sobre mezcla | 403 / 592 = **68,074%** | 401 / 592 = **67,736%** |

El normalizador histórico usa Unicode NFKC y conserva las tildes como parte
del carácter. El contrato nuevo usa NFD y elimina marcas combinantes. Esa
diferencia mueve menos de 0,04 puntos porcentuales al corpus UMG y 0,34 puntos
en Bersuit; no explica la brecha.

En las 57 canciones, la mediana del WER individual histórico es 5,45%, el p90
es 20,95% y el peor caso es 49,38%. Bersuit queda por encima de las 57.

## Por qué no son baselines equivalentes

- UMG 8,3% compara la salida histórica completa del producto —recuperación de
  letra, ASR, alineación y posprocesado— con lo que después aprobó el humano.
- Bersuit 68% compara ASR local ciego de una grabación en vivo extrema con una
  letra canónica no temporizada. La performance agrega, omite y repite material,
  y mezcla cantante, público y reverberación.
- `Señor Cobranza` no pertenece a las 65 entregas. No es una observación del
  mismo corpus ni del mismo pipeline histórico.

Conclusión: el harness no debe usar 0,68 como sustituto del baseline UMG. Sí
debe conservar Bersuit como stress test separado de dominio en vivo. Las
variantes quedan bloqueadas hasta cerrar la extracción, los 5 controles de
portal y el baseline local de las 8 canciones sin crudo.
