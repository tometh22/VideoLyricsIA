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
| Sin punto final en las líneas | #11, #12, #22, #25, #32, #33; "Para no volver." (oct), "Lejos de vos." (nov) | Sí: "Punto final", obligatorio en los dos portales, también en el re-render por pedido de cambio (las siglas "V.S.D." y los "..." quedan) |
| ¿? sólo si de verdad es una pregunta | "Cuando vuelvas" sin signos (#121), "Cuando miro en tus ojos" (#117) | Sí: "¿Cuando…" sin tilde, "¿A ver si…" |
| Un signo de pregunta siempre con su par | "Si ya no tengo las caricias de mi madre?", "¿Por qué soy wacho amor" (oct) | Sí, obligatorio: con interrogativo con tilde se completa ("Pero, ¿qué hago yo acá?"), si no se quitan; la otra opción a un número |
| Exclamaciones: Argentina sin signos, Chile con ¡! | "¿Qué lindo añorar la zamba?" → sin signos (AR, #124); "¡Cuánto me hiciste llorar!" (CL, #100) | Sí: "¿Qué lindo…?" según el portal |
| Argentina: coros y ecos sin paréntesis | "(cargada del amor)", "(AY, AY, AY)" de más (oct-nov) | Sugerencia: UMG aceptó otros 22 con paréntesis |
| La misma palabra lleva siempre la misma tilde | "MÍO MIO" (#114) | Sí: "Tildes distintas" |
| Tildes que faltan sin dudas | "ARDÍAN", "Descubriéndose", "ESTÁ de más" (CL), "DARÁN" (AR) | Sí, obligatorio: "Falta una tilde" (sólo palabras que sin tilde no existen, gerundio + pronombre, -ción/-sión, "está" antes de "de/en/bien…") |
| Tildes que cambian el sentido se deciden escuchando | "llegue / llegué" (#115), "cuando / cuándo" (#108) | No: sólo avisa si la palabra aparece distinta en la canción |
| Palabras separadas, con espacio después de la coma | "logro entender", "si esto" (#125), "vuelvo, vuelvo" | Sí: "Palabras pegadas" / "Falta un espacio" |

## Pantalla

| Regla | Ejemplo real | Control automático |
|---|---|---|
| Frase completa en una sola pantalla | "Borracho y agresivo…" (#75), "Cuando vuelvas / Quiero verte" (#121) | No: criterio del revisor |
| Nunca una palabra sola en pantalla | "QUE" suelto (#98, #128) | Sí: "Palabra sola en pantalla" → unir |
| Líneas de largo parecido; repeticiones cortas por separado | "MANOS / COSAS / MANOS / COSAS" (#131), Mataz (#125) | No |
| La línea aparece cuando se canta y dura lo que dura | "alargar brasero" (#115), "aparecer antes" (#119) | Parcial (preflight: línea que termina antes de la palabra) |

## Perfil por portal

La Revisión rápida sabe a qué portal va la canción (el destino de la
campaña; "umg" si es una entrega UMG sin portal fijo) y aplica sus reglas.
Lo que no se puede decidir sin escuchar queda afuera: preguntas de sí/no sin
signo de duda ("¿Llegas a la ciudad?"), "¿Cuánto…?" que exclama o pregunta,
el voseo "quedés/quedes", las comas.

## Artista y título

Se escriben como los pide UMG desde que se registra la campaña:
`lyricgen/backend/data/umg_metadata_catalog.json` fija el artista
("Charly García", "Serú Girán", "Los Enanitos Verdes") y las correcciones
de canciones puntuales ("Tuyu", "¿Dónde Estarás?", "Coti Ft. Enrique
Iglesias" en "¿Dónde Están Corazón?"). Cada corrección de artista/título que
se aplica a un video de UMG (pedido de cambio o Editar → artista/título) se
recuerda para las canciones siguientes. Un video renderizado con un nombre
distinto no se publica: hay que corregirlo y volver a renderizar
(`UMG_METADATA_GATE=observe` lo deja pasar con un aviso en el log).

## Cómo se usa en el editor

1. Abrí **Sugerencias de letra** con **Ver sugerencias** si querés consultar
   las propuestas. Son opcionales y pueden equivocarse, aunque coincidan
   varias fuentes. La línea de la tarjeta activa queda marcada.
2. Cada tarjeta muestra la línea como va a quedar: en verde lo que entra,
   tachado lo que sale (en un cambio de signos, sólo el signo).
3. Teclado (sólo cuando abrís el panel y le das foco):

   | Tecla | Qué hace |
   |---|---|
   | **Enter** | aplica el arreglo y pasa al siguiente |
   | **⌫** | "conservar mi letra" |
   | **1–3** | elige otra opción cuando los oídos no coinciden |
   | **E** | escucha el tramo (con "Reproducir al avanzar" suena solo al pasar de punto) |
   | **J / K** | siguiente / anterior |
   | **M** | lleva el cursor a esa línea para editarla a mano |
   | **Z** | deshace la última decisión |
   | **?** | muestra los atajos, las fuentes comparadas y esta guía |

4. **Aprobar** conserva la letra actual aunque queden sugerencias sin revisar.
   **Conservar mi letra** descarta una sugerencia y guarda esa decisión;
   **Aplicar sugerencia** cambia el texto y permite deshacer.
5. Si tenés la letra oficial (Google, planilla de UMG), pegala **apenas abrís
   la canción**, antes de pasar las tarjetas: **Comparar con letra oficial**
   (abajo del panel) o **Pegar letra oficial** (barra de la letra) abren la
   misma ventana. **Comparar** no cambia tu letra ni los tiempos;
   **Reemplazar** pisa el texto y re-sincroniza. Con la letra oficial el panel encuentra bastante más (en los
   pedidos de septiembre, 115 de 136 cambios contra 97 sin ella).
6. Si la canción está marcada como **difícil** (en vivo, casi hablada, los
   oídos automáticos no coinciden), escuchala entera o pedí una segunda
   revisión.

## Política de aprobación

- Las sugerencias de letra no bloquean campañas, trabajos individuales ni
  re-renders. Siguen vigentes las validaciones de letra vacía, conflictos
  de edición y errores de guardado.
- `LYRIC_REVIEW_MODE` usa `observe` por defecto; `off` desactiva el análisis.
  El valor legado `enforce` equivale a `observe` y
  `LYRIC_REVIEW_ENFORCE_TENANTS` ya no activa bloqueos.
- `required` en la respuesta es metadata legada de prioridad, no una
  obligación de aplicar o descartar. No se modifican las decisiones previas.
- Las campañas batch son "sólo audio": nunca se busca letra en lrclib para
  ellas; la letra oficial entra por la planilla o pegada por el operador.
