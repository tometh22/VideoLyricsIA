# Capa D — estado del replay al 2026-08-29

La infraestructura de replay está lista, pero ninguna categoría está habilitada
para tier agente. No se llamó a Gemini, no se extrajeron clips y no hubo costo
externo.

## Inventario preparado

- 41 canciones `exact+reconstructed`.
- 1.304 zonas seleccionadas por la unión calibrada de flags de Capa B.
- 511 zonas con corrección humana de timing.
- 171 zonas con corrección humana de texto.
- 674 zonas sin cambio humano, necesarias para medir falsos resueltos.
- 0 zonas ejecutables hoy: el corpus histórico no preservó las hipótesis
  independientes que existían antes de Agus.

Las etiquetas de corrección no son excluyentes, por lo que sus conteos no deben
sumarse para inferir el total de zonas.

## Decisión

El replay no puede rellenarse con la letra aprobada ni con variantes derivadas
de ella: sería circular. El próximo input válido es un JSONL regenerado sobre
el estado pre-humano, con candidatos respaldados por dos familias realmente
independientes. Whisper large-v2 y large-v3 cuentan como una sola familia;
Gemini no puede aportar candidatos porque actúa como decisor.

Cuando exista ese artefacto, el flujo ya implementado es:

1. extraer clips de ±4 s solamente para zonas con candidato verificado;
2. ejecutar Gemini con elección, edición mínima o abstención;
3. comparar contra Agus por texto, timing y vocalización;
4. adjudicar hasta 30 desacuerdos con tres familias distintas de Gemini;
5. habilitar por categoría solo con ≥50 decisiones, ≥10 canciones, acuerdo
   funcional ≥80% y falsos resueltos <3%;
6. mantener vivo fuera y producir una política de auditoría 20% → 10% que no
   puede autoactivarse.
