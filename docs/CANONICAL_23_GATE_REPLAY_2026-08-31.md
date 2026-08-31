# Replay de gates sobre la cohorte canónica exacta — 2026-08-31

Primera línea: **la cohorte canónica es 23 `exact`; `reconstructed` queda sólo
para diagnóstico porque el rewind no reproduce texto, segmentación ni timing
con fidelidad suficiente.** No se creó ningún gate nuevo.

## Gates recalculados

| Bloque | Resultado exact-23 | Decisión |
|---|---|---|
| ZTLR histórico | 439/905 = **48,5%** | baseline corregido |
| Realineado jerárquico | p50 **20 ms**, p90 **469 ms**; 68/274 líneas solo-timing reproducidas a ±150 ms | NO_GO global; selector sigue en calibración |
| Selector timing | 2 aprobaciones en 2 canciones; precisión puntual 100%, bootstrap low 0% | NO_GO por evidencia insuficiente |
| MSS-ALT | WER 19,06% → 18,43%; mejora relativa +3,30%, IC95 [−4,85%, +13,15%], 4 canciones regresan >2 pp | NO_GO |
| Poda de flags | recall 93,13%, 400 falsos; 362,6 s/canción | NO_GO |
| Cola total proyectada | 164,3 s/canción retrospectivos → 362,6 s con el punto de recall 93% | NO_GO |

El realineado no está roto en el centro: mantiene 20 ms de mediana y reduce la
cola p90 del común global→jerárquico de 774 a 460 ms. No puede aplicarse de
forma global porque todavía confunde ocurrencias en la cola y el selector sólo
puede certificar dos líneas sin bajar su intervalo de confianza.

## Viejos vs. RunPod

El inventario sigue siendo 26 stems anteriores y 15 nuevos. Dentro de las 23
canciones con crudo exacto, la comparación válida es 16 vs. 7:

- p50 realineado: **20 ms en ambos grupos**;
- p90 jerárquico: 363 ms anteriores vs. 840 ms RunPod;
- WER nativo: 13,65% anteriores vs. 28,50% RunPod;
- modelo: `mdx_extra`, Demucs 4.0.1 en los RunPod y los controles locales;
- todos los stems: estéreo 44.100 Hz;
- cinco pares mismo master: misma longitud y offset por correlación **0 ms**.

Conclusión: el grupo nuevo tiene canciones más difíciles; no hay evidencia de
resampling, padding o desplazamiento introducido por RunPod. Los NO_GO se
mantienen por la cola del método, no por stems defectuosos.

## Contrato para T4

Los inicios salen del objetivo: sobre 848 líneas alineadas, error de inicio
p50/p90 = **0/0 ms**. T4 se evalúa solamente sobre los **95 finales tempranos
(11,2%)**: mediana 600 ms, p90 2.737 ms, máximo 8.407 ms y 26 casos >1 s.
Cualquier reporte global que mezcle inicios o finales ya correctos puede
ocultar daño y no sirve para promover el próximo T4.

