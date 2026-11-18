# Guía de estilo de letras para UMG

Armada con los 131 pedidos de cambio que UMG Argentina y UMG Chile mandaron
por el portal entre mayo y septiembre de 2026. Cada regla dice de dónde sale
y si el editor la controla sola (**Revisión rápida**) o queda a criterio del
revisor.

## Texto

| Regla | Ejemplo real | Control automático |
|---|---|---|
| Se escribe lo que se canta, no lo que "debería" decir | "Pásenlo en la radio", no "Pásalo" (#120) | Sí: se compara con la letra oficial, Gemini y el testigo |
| No se borra nada que se cante, aunque el pedido cite sólo un pedazo | "…se fundió, dormite ya" (#113), "Que hace un año atrás" (#112) | Sí: "Falta texto" + el aplicador de pedidos ya no pisa la línea entera |
| Si un coro se corrige, se corrige en **todas** sus repeticiones | "corregir en todos los coros" (#99, #104, #108, #121) | Sí: los arreglos iguales se agrupan y se aplican juntos |
| El título se escribe igual cada vez que aparece | "Cuando vuelva**s**" (#121), "lavártelo**s**" (#129), "Por qué" separado (#104) | Sí: "Como en el título" |
| Se respetan los modismos y apócopes | "pa'", "na'", "querís", "de onde" (#96, #122) | Sí: nunca se proponen como error |
| Nombres propios y lunfardo como los escribe el artista | "Kapelusz", "Xuxú", "linyerismo voyeur" (#120, #123) | Parcial: letra oficial + memoria de correcciones del artista |
| Palabras repetidas se escriben todas | "también, también" (#115), "suman, suman mil" (#119) | Parcial: si dos oídos lo escuchan |
| Ad-libs cantados sí; gritos, aplausos y charla no | "Y-yah-yah" sí (#119); "Amen" gritado no (#126) | No: criterio del revisor |

## Ortografía y puntuación

| Regla | Ejemplo real | Control automático |
|---|---|---|
| Sin punto final en las líneas | #11, #12, #22, #25, #32, #33 | Sí (preflight de entrega) |
| ¿? sólo si de verdad es una pregunta | "Cuando vuelvas" sin signos (#121), "Cuando miro en tus ojos" (#117) | Sí: "¿Cuando…" sin tilde, "¿A ver si…" |
| Exclamaciones con ¡! | "¡Qué lindo añorar la zamba!" (#124), "¡Cuánto me hiciste llorar!" (#100) | Sí: "¿Qué lindo…?" pasa a ¡! |
| La misma palabra lleva siempre la misma tilde | "MÍO MIO" (#114) | Sí: "Tildes distintas" |
| Tildes que cambian el sentido se deciden escuchando | "llegue / llegué" (#115), "cuando / cuándo" (#108) | No: sólo avisa si la palabra aparece distinta en la canción |
| Palabras separadas, con espacio después de la coma | "logro entender", "si esto" (#125), "vuelvo, vuelvo" | Sí: "Palabras pegadas" / "Falta un espacio" |

## Pantalla

| Regla | Ejemplo real | Control automático |
|---|---|---|
| Frase completa en una sola pantalla | "Borracho y agresivo…" (#75), "Cuando vuelvas / Quiero verte" (#121) | No: criterio del revisor |
| Nunca una palabra sola en pantalla | "QUE" suelto (#98, #128) | Sí: "Palabra sola en pantalla" → unir |
| Líneas de largo parecido; repeticiones cortas por separado | "MANOS / COSAS / MANOS / COSAS" (#131), Mataz (#125) | No |
| La línea aparece cuando se canta y dura lo que dura | "alargar brasero" (#115), "aparecer antes" (#119) | Parcial (preflight: línea que termina antes de la palabra) |

## Cómo se usa en el editor

1. Abrí la canción: arriba de la letra está **Revisión rápida**, con una
   tarjeta a la vez. La línea de la letra correspondiente queda marcada.
2. Cada tarjeta muestra la línea como va a quedar: en verde lo que entra,
   tachado lo que sale (en un cambio de signos, sólo el signo).
3. Teclado (el panel toma el teclado al abrir la canción):

   | Tecla | Qué hace |
   |---|---|
   | **Enter** | aplica el arreglo y pasa al siguiente |
   | **⌫** | "está bien así" / "no se canta" |
   | **1–3** | elige otra opción cuando los oídos no coinciden |
   | **E** | escucha el tramo (con "Reproducir al avanzar" suena solo al pasar de punto) |
   | **J / K** | siguiente / anterior |
   | **M** | lleva el cursor a esa línea para editarla a mano |
   | **Z** | deshace la última decisión |
   | **?** | muestra los atajos, las fuentes comparadas y esta guía |

4. "Aprobar" dice **Faltan N · Revisar** mientras quede algo obligatorio;
   al tocarlo te lleva al panel. Las sugerencias no bloquean.
5. Si tenés la letra oficial (Google, planilla de UMG), pegala **apenas abrís
   la canción**, antes de pasar las tarjetas: **Comparar con letra oficial**
   (abajo del panel) o **Pegar letra oficial** (barra de la letra) abren la
   misma ventana. **Comparar** no cambia tu letra ni los tiempos;
   **Reemplazar** pisa el texto y re-sincroniza. Con la letra oficial el panel encuentra bastante más (en los
   pedidos de septiembre, 115 de 136 cambios contra 97 sin ella).
6. Si la canción está marcada como **difícil** (en vivo, casi hablada, los
   oídos automáticos no coinciden), escuchala entera o pedí una segunda
   revisión.

## Qué bloquea y dónde

- Bloquea la aprobación en las campañas batch (UMG) o en los tenants de
  `LYRIC_REVIEW_ENFORCE_TENANTS`; en el resto el panel se muestra sin frenar.
- Un re-render por pedido de cambio sólo frena si se perdió letra cantada.
- Un cambio de fondo o tipografía nunca frena por la letra.
- Las campañas batch son "sólo audio": nunca se busca letra en lrclib para
  ellas; la letra oficial entra por la planilla o pegada por el operador.
