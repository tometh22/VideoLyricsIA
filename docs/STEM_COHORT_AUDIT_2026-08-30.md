# Auditoría de stems y corte de cohortes — 2026-08-30

## Definición

- **Cohorte anterior (26):** 21 stems documentados como Demucs `mdx_extra`
  local/MPS y 5 stems legacy cuyo origen no había quedado registrado.
- **Cohorte nueva (15):** stems generados en RunPod/CUDA con Demucs 4.0.1 y
  modelo `mdx_extra`.

## Métricas separadas

| Métrica | 26 anteriores | 15 RunPod |
|---|---:|---:|
| WER nativo | 14,28% | 25,17% |
| WER MSS-ALT | 14,71% | 22,81% |
| Mejora relativa MSS-ALT | −3,01% | +9,40% |
| IC 95% mejora relativa | [−20,54%, +12,68%] | [−2,59%, +24,97%] |
| Realineado global p50 | 123,7 ms | 110,0 ms |
| Realineado global p90 | 797,8 ms | 953,3 ms |
| Jerárquico p50 | 110,0 ms | 220,0 ms |
| Jerárquico p90 | 594,8 ms | 1.080,0 ms |

Los 15 nuevos son una cohorte más difícil ya antes de MSS-ALT (WER nativo
25,17% contra 14,28%). Por lo tanto, el agregado 41 no permite atribuir su peor
timing al origen del stem. MSS-ALT ayuda más en esa cola difícil, pero su
intervalo todavía cruza cero y también introduce regresiones: sigue NO_GO como
mutación productiva.

## Control mismo audio: RunPod contra local

Se eligieron los cinco audios RunPod de menor duración antes de inspeccionar
los offsets y se regeneraron localmente con el mismo modelo.

- Modelo en ambos: `mdx_extra`.
- Demucs en ambos: 4.0.1.
- Local: MPS, torch 2.8.0. RunPod: CUDA.
- Los 41 stems: estéreo, 44.100 Hz.
- Los cinco pares: misma cantidad de frames y diferencia de duración 0 ms.
- Correlación alineada por par: 0,99933–0,99979.
- Offset por correlación cruzada en los cinco pares: **0,0 ms**.

Los SHA-256 no coinciden, como es esperable por diferencias numéricas
MPS/CUDA, pero las líneas de tiempo sí son equivalentes dentro de 1 ms. Queda
descartado que RunPod haya introducido resampling, padding o desplazamiento
temporal. La diferencia 26/15 es de dificultad de cohorte, no de timeline del
separador.

Los reportes reproducibles quedan en `eval/runs/stem_cohort_comparison` y
`eval/runs/stem_cohort_audit`; esas salidas son artefactos ignorados y se
regeneran con los módulos `eval.stem_cohort_report` y
`eval.stem_cohort_audit`.

## Relectura sobre la cohorte canónica exacta (2026-08-31)

La decisión de crudo exact-only deja **16** canciones del inventario anterior
y **7** de las nuevas RunPod. Los nombres `26/15` identifican el origen físico;
no autorizan a reintroducir las 18 reconstruidas en un gate.

| Métrica calibrada leave-one-song-out | 16 anteriores exactas | 7 RunPod exactas |
|---|---:|---:|
| Realineado global p50 / p90 | 20 / 628 ms | 20 / 1.240 ms |
| Jerárquico p50 / p90 | 20 / 363 ms | 20 / 840 ms |
| WER nativo | 13,65% | 28,50% |
| WER MSS-ALT | 12,55% | 28,69% |
| Mejora relativa MSS-ALT (IC95) | +8,03% [−4,02%, +23,79%] | −0,65% [−10,05%, +12,34%] |

Ambos orígenes sostienen el p50 de 20 ms. La cola RunPod es peor, pero ya era
una cohorte mucho más difícil antes de MSS-ALT. Sumado a los cinco offsets de
0 ms, esto refuta una causa de stems/timeline; no reabre ninguno de los NO_GO.
