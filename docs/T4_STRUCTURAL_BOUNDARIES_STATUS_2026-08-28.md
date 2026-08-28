# T4 estructural — estado del gate

## Decisión

El T4 estructural queda conectado en **modo observación**. No modifica el
timing visible ni puede atravesar una frontera de ocurrencia por defecto.

## Qué quedó implementado

- Separación explícita entre `display_end`, `phonetic_end` y el inicio de la
  línea siguiente.
- Diagnóstico de fronteras heredadas del ASR, padding fijo de 250 ms y relojes
  de palabra/línea acoplados.
- Respeto absoluto de segmentos bloqueados por el operador.
- Contrato hash-bound para una propuesta temporal respaldada por al menos dos
  familias independientes.
- Segundo contrato obligatorio para cruzar al siguiente cartel: misma
  ocurrencia, índices adyacentes y prueba SHA-256.
- Ningún uso de Rotor, UMG o letra de catálogo durante inferencia.

## Replay que evitó una regresión

La primera versión usó directamente el final de la última palabra como cambio
visible. Sobre las 10 canciones generó 28 propuestas. En los 301 eventos
comparables con Rotor:

- mejoró más de 150 ms en 4 eventos;
- dañó más de 150 ms en 14 eventos;
- empeoró el MAE global un 1,53%;
- daño máximo observado: 3,421 s.

Resultado: **NO_GO**. El word clock queda como detector del síntoma, no como
juez del endpoint.

La versión endurecida exige una atestación temporal independiente antes de
proponer. Como los resultados históricos no contienen ese nuevo contrato,
produjo cero cambios y cero daño. El siguiente paso no es bajar el umbral: es
generar la segunda evidencia en el quality worker y calibrarla con los 20
endpoints humanos ya revisados.

## Gate para habilitar cambios visibles

1. Mejora relativa en el conjunto objetivo.
2. Cero cambios sobre líneas bloqueadas.
3. Cero cruces de ocurrencia sin atestación válida.
4. Cero daños mayores a 150 ms fuera del conjunto objetivo en la muestra de
   calibración.
5. Replay separado por estudio y vivo; vivo permanece Tier 2.

Artefactos de replay:

- `.context/phase0/rotor-umg-v1/t4-structural-shadow-v1-evaluation.json`
- `.context/phase0/rotor-umg-v1/endpoint-validation-20/`
