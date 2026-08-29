# Estado diagnóstico y replay — 2026-08-29

Todo este bloque vive en la rama de evaluación; no modificó staging ni producción.

## Resultado ejecutivo

- **T4 aprendido sigue NO_GO.** La autopsia eliminó 428 arrastres intermedios mal contados. El clasificador sí predice qué línea será tocada (AUC 86.2%), pero el regresor acierta ±150 ms en solo 13.4% y pierde contra la mediana de líneas corregidas (19.8%).
- **Predictor de errores:** AUC pasó de 70.8% a 79.0% (CI95 72.3%–85.1%); no cruza 0,80. Como ordenador, revisar el primer tercio captura 52.6% de las correcciones, contra 29.6% en el orden actual.
- **Replay exacto del selector desplegado:** 10/41 stems `mdx_extra` disponibles (5 preservados y el resto regenerados localmente). Hubo 60 correcciones reales, 158 propuestas y 6 coincidencias (recall 10.0%, precisión 3.8%). Con bootstrap de 10 canciones, el selector queda **NO_GO**.
- **Taxonomía:** tres familias locales, cero egreso. 70/454 unánimes; 384 disputadas, todas con clip local. La expectativa de una cola de 5–20 quedó refutada.
- **LoRA:** `whisper-large-v3-turbo` completó un paso real sobre JamendoLyrics filtrado a licencias BY/BY-SA/CC BY/CC BY-SA, sin audio UMG ni egreso. Esto valida el ejecutor, no una mejora. Las 498 muestras UMG siguen bloqueadas.
- **T7 preparado:** 9746 pares en 65 canciones (2372 omisiones, 2372 inserciones y 2409 sustituciones); entrenamiento bloqueado por la misma autorización UMG.
- **Fase 2:** repetidas 6.0% vs únicas 5.5%; uplift relativo 9.4%, debajo del 20%, por lo que no se implementa votación. Rescoring fonético carece de n-best/posteriors prehumanos; Gemini carece de credencial y autorización de egreso; N=5 espera stems exactos.

## Decisiones de Tomi

1. **Taxonomía:** recomendación **sí** a validación humana; no publicar las 384 disputadas por mayoría.
2. **Entrenamiento UMG:** recomendación **no todavía**; ver `umg_training_egress_analysis.md`.
3. **Predictor:** recomendación **sí solo en observación**, nunca como router obligatorio mientras no pase AUC 0,80.
