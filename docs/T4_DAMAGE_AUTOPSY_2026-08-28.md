# Autopsia T4 — 14 regresiones del candidato de reloj de palabra

## Alcance

El candidato descartado reemplazaba el final visible por el final de la
última palabra informado por WhisperX. Produjo 28 cambios; 18 pudieron
compararse con el export verificado de Rotor, de los cuales 4 mejoraron y 14
empeoraron más de 150 ms contra ese comparador.

La tabla separa una **regresión contra Rotor** de un daño confirmado por el
audio. Rotor no se usa para generar ninguna propuesta.

| Caso | Línea | Cambio | Causa raíz | Evidencia |
|---|---:|---:|---|---|
| Rata Blanca — Hada y el Mago | 1 | 19,590→20,040 | reloj ASR estirado | pitch termina en 19,400; la palabra de ASR continúa 640 ms más |
| Rata Blanca — Hada y el Mago | 7 | 51,170→51,827 | reloj ASR estirado | pitch termina en 49,150; `destino` ocupa 2,841 s en el reloj ASR |
| Rata Blanca — Hada y el Mago | 14 | 89,170→89,430 | endpoint no resoluble por pitch | el último run es demasiado corto; debe abstenerse o usar consonante/energía independiente |
| Rodrigo Romero — La Foto | 9 | 55,390→55,626 | falso daño del comparador | pitch termina en 55,650 y respalda la propuesta; Rotor corta antes |
| Rodrigo Romero — La Foto | 10 | 61,910→62,072 | falso daño del comparador | pitch termina en 62,200 y respalda la propuesta; Rotor corta antes |
| Rodrigo Romero — La Foto | 27 | 174,190→174,445 | reloj ASR estirado | pitch termina en 173,150; el reloj de palabra queda pegado a una cola no léxica |
| Rodrigo Romero — La Foto | 32 | 195,410→196,173 | falso daño del comparador | pitch termina en 195,700 y respalda una extensión, con anticipo perceptual |
| Si No Es Muy Tarde | 3 | 22,210→22,852 | reloj ASR estirado | pitch termina en 21,250; `placar` absorbe cola acústica |
| Si No Es Muy Tarde | 5 | 32,610→34,345 | reloj ASR estirado | pitch termina en 33,450; `usar` absorbe casi 900 ms adicionales |
| Si No Es Muy Tarde | 10 | 59,790→59,979 | endpoint no resoluble por pitch | el run final no es estable; no corresponde proponer sólo con ASR |
| Si No Es Muy Tarde | 22 | 132,770→133,052 | endpoint no resoluble por pitch | el run final no es estable; no corresponde proponer sólo con ASR |
| Si No Es Muy Tarde | 24 | 143,730→143,943 | frontera textual no comparable | Genly incluye `Y solo te pido`; Rotor corta la línea en `merecía` |
| Si No Es Muy Tarde | 26 | 151,850→155,271 | falso daño del comparador | `no` es una vocal sostenida; pitch termina en 155,350 |
| Si No Es Muy Tarde | 29 | 166,570→166,834 | endpoint no resoluble por pitch | no hay run estable; la frase siguiente empieza después y no fue invadida |

## Ranking de causas

1. Fin de palabra ASR estirado sobre cola/reverb: **5/14**.
2. Sin pitch estable para certificar el final: **4/14**.
3. Falso daño contra Rotor; el audio respalda la extensión: **4/14**.
4. Frontera textual distinta entre sistemas: **1/14**.
5. Pitch saltó a coro/armonía: **0 confirmados**.
6. Salto a otra ocurrencia: **0 confirmados**.
7. Invasión del comienzo real de la frase siguiente: **0 confirmados**.

## Reglas derivadas para propuestas humanas

- El reloj de palabra es detector del síntoma, nunca juez único.
- El candidato visible usa el último run de pitch estable dentro de la línea y
  anticipa 100 ms para timing perceptual.
- Si no hay pitch estable, se abstiene; no se rellena con una constante.
- Nunca cruza el inicio del cartel siguiente y rechaza saltos mayores a 6 s.
- Líneas bloqueadas o editadas por un operador no reciben propuestas.
- Los cambios se muestran para aceptar/rechazar; no existe autoaplicación.

## Gate reproducido

- Cobertura del conjunto grave: **31/62 (50,0%)** usando veto de saltos mayores
  a 6 s y fallback de reloj de palabra sólo cuando queda dentro de la misma
  frontera.
- Precisión sobre endpoints humanos evaluables: **10/16 (62,5%)** dentro de
  ±150 ms con anticipo perceptual de 100 ms.
- Cuatro endpoints humanos sin una referencia utilizable se excluyen del
  denominador; no se convierten artificialmente en aciertos o errores.

Este gate habilita únicamente sugerencias de un clic. No certifica
autocorrección.
