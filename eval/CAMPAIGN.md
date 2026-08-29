# Campaña de aprendizaje con correcciones propias

Baseline congelado de trabajo: cohorte de 41 crudos `exact+reconstructed`, WER
main 5,77% y error absoluto de final p90 750 ms. El cierre definitivo exige la
extracción fresca, SHA-256 de los 65 audios y cinco controles contra el portal.

## Orden y gates

1. Autopsia final sobre todos los diffs observados. Ningún modelo se implementa
   antes de ordenar los buckets por contribución.
2. LoRA y curva dato→mejora con split por canción y leave-artist-out. Solo
   fuentes/sugerencias; nunca mutación directa.
3. T4 aprendido con validación cruzada por canción. Gate de sugerencia: al
   menos 60% dentro de ±150 ms de la corrección humana held-out.
4. Predictor de error. Gate: AUC ≥0,80 held-out por canción.
5. Solo los módulos de inferencia que ataquen buckets materiales de la
   autopsia. Gemini audio es fuente, nunca juez.
6. Destilación recién con 200 canciones de gold.

Todo intervalo de confianza usa bootstrap por canción, nunca por evento. Los
reentrenamientos se evalúan cada 50 canciones nuevas y reportan la curva de
calidad contra cantidad de gold.
