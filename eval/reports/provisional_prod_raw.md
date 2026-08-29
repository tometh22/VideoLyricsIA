# Baseline provisional `prod_raw`

Fuente: snapshot local auditado de 65 entregas, reconstruido desde cero con el
contrato canónico. Todavía es provisional: faltan la extracción fresca de
versiones/diffs, los 65 audios verificados y los cinco controles del portal.
No se corrió ninguna variante.

| Cohorte | Casos | WER main corpus | WER canción p50 / p90 | Recall línea medio | Timing completo p50 | Final abs p90 | Perfectas | Casi perfectas |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| exact + reconstructed | 41 | 5,77% | 3,14% / 16,89% | 97,53% | 76,92% | 750 ms | 2,44% | 9,76% |
| + estimated | 57 | 8,12% | 5,23% / 24,03% | 95,94% | 75,00% | 840 ms | 1,75% | 7,02% |

En las 57 hipótesis históricas hay 97 líneas de referencia sin match y 105
líneas de hipótesis sin match bajo el veto de identidad de ocurrencia. El
error absoluto de inicio tiene p90 de 0,22 ms y el final p90 de 840 ms. El
inicio casi perfecto es plausible pero debe confirmarse con la extracción
fresca: una parte del corpus reconstruido solo revierte los campos que los
audits conservaron.

El diff derivado inicial contabiliza 1.371 operaciones: 974 de timing y 397
de texto/estructura. El desplazamiento de timing tiene p50 361 ms y p90
2.391 ms. Estos conteos no sustituyen el historial observado: se recalcularán
desde todas las `EditorVersion` y `lyrics.segments_diff` cuando estén las
credenciales.

Durante la validación se encontró un defecto del primer harness: el Hungarian
podía emparejar una repetición con otra ocurrencia a 60–142 segundos. Se agregó
un veto auditable para matches solo-texto separados por más de 10 segundos y
se reconstruyó todo el snapshot antes de publicar estos números.
