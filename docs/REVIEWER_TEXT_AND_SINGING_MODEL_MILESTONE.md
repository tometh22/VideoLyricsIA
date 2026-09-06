# Texto y evidencia aprendida de canto — 2026-09-05 ART

## Resultado

**Nueva capacidad ejecutada:** candidato completo con dos reparaciones textuales
y realineación acotada; detector preentrenado de actividad cantada sobre audio
español; probabilidades por frame y trayectorias alternativas del CTC.
**No hay todavía un modelo validado de final fonético/perceptual español ni
una nueva reparación automática de timing.** La variante espectral manual está
archivada (`SPECTRAL_EXPERIMENT_STATUS`); sus evidencias permanecen intactas.

### A. Candidata textual, sin modificar el vigente

Bersuit rev0, 41 líneas. Cambios acumulados en la copia aislada:

| Línea (1-based) | Reparación | Evidencia y realineación |
|---|---|---|
| 13 | `nos` → `no` | Dos familias de audio sin referencia; ocurrencia ASR99,00–104,82. CTC acotado99,08–104,72 |
| 29 | Palabra fusionada → dos palabras | Caso de desarrollo anterior preservado; CTC acotado209,92–214,76 |

Los documentos actuales, sus 38 líneas protegidas, aprobaciones y cola no se
tocaron. La realineación queda como evidencia de palabras, **no certificación
ni cambio de los límites de pantalla**. El resto sigue conservado sin certificar.

Fallo conservado: la primera realineación de línea13 usó90–114 y terminó en
112,30, invadiendo otra frase. No se adoptó. Acotando al vecindario de la
ocurrencia original97,92–105,16 se obtiene104,72. La escucha de los modelos
conserva su contexto amplio; solo la alineación condicionada se acota.

Se reutilizaron todas las llamadas anteriores: **cero llamadas nuevas**.
No se volvió a escuchar la canción completa. El caso histórico sigue siendo
desarrollo contaminado, no evaluación independiente.

### Las 15 frases no exactas

Ahora se alinean secuencias con anclas consecutivas o discontiguas y se retienen
operaciones, intervalos, candidatos alternativos y conflictos. Se evita que una
coincidencia parcial de prefijo sea preferida artificialmente a una sustitución
con el mismo costo de edición. Ningún score de similitud certifica el texto.

| Línea | Diagnóstico, no veredicto humano |
|---|---|
| 2 | Correspondencia parcial/discontinua; no prueba de omisión |
| 9 | Desacuerdo de reconocimiento: `de` / `en` |
| 12 | Ubicación en conflicto entre resultados de ventanas; fragmentación de palabra |
| 13 | Hipótesis léxica respaldada por dos familias; incorporada al candidato |
| 17 | Diferencia ortográfica de tilde; no error léxico demostrado |
| 18 | Desacuerdo de reconocimiento en palabra final |
| 21 | Desacuerdo de reconocimiento en palabra inicial |
| 22 | Desacuerdo `así` / `hace` |
| 28 | Ubicación en conflicto; no se certifica por parecido |
| 29 | La escucha integral difiere en tilde; el parche léxico anterior conserva su evidencia separada |
| 31 | Sin anclas suficientes; se conserva la abstención anterior |
| 33 | Correspondencia dudosa y desacuerdo de reconocimiento |
| 35 | Ubicación en conflicto entre ventanas de una frase repetida |
| 38 | Desacuerdo sobre la palabra final |
| 40 | Posible truncamiento de ventana y desacuerdo; no implica letra omitida |

Hay correspondencias propuestas en14/15, no14 frases certificadas. No se halló
un caso meramente de puntuación/capitalización entre estas15; las tildes se
separan de formato. Los conflictos de ubicación no prueban automáticamente
que el modelo saltó a otra repetición: también pueden ser timestamps inestables.

## B. Modelo de canto realmente ejecutado

Se integró el checkpoint `model_log_0mean` del detector
[CPJKU/veracity](https://github.com/CPJKU/veracity), entrenado para actividad
cantada. Se carga la red original sin modificar, `state_dict` estricto y
`weights_only=True`, sin reentrenamiento ni ajuste de umbrales.

- Revisión upstream: `0983900f136173015f3c5d0b116be014edd33905`.
- Checkpoint: 7.433.173 bytes; SHA256
  `f09cd6a55c83c13640622ce67480443ce282e3124dd22fccee8a898bb2ba9dee`.
- Entrada: audio22.050Hz; no tokenizer ni inventario de fonemas.
- Salida: probabilidad aprendida de actividad cantada cada1/70s; contexto de
  red115frames, aproximadamente1,64s. **No palabras, fonemas, notas ni corte perceptual.**
- Compatibilidad funcional con español: ejecutada en tres fragmentos de
  Bersuit y uno de Luciano. Calidad/calibración específica en español:
  **no validada**; no se presupone por carecer de tokenizer.
- El repositorio publica licenciaMIT; no se incorporaron pesos al producto
  ni se activó el modelo en staging.

No es otra similitud log-mel con umbral manual: usa una CNN con pesos aprendidos
y solo se exportan sus probabilidades. No se transforma su score en un end.

### Evidencia distinta del blank

Promedios durante los500ms inmediatamente posteriores al final CTC nuevo:

| Línea | End CTC | P(blank) media | P(actividad cantada) media |
|---|---:|---:|---:|
| 7 | 54,90 | 0,9956 | 0,7533 |
| 34 | 238,22 | 0,999995 | 0,9852 |
| 41 | 280,58 | 0,9612 | 0,6617 |

Esto muestra desacuerdo entre emisiones de caracteres y actividad aprendida,
no una duración fonética certificada. La red puede responder a otra voz/coro;
su contexto amplio tampoco habilita cortes precisos por sí mismo. No se
atribuyó la actividad automáticamente a la última palabra.

### CTC: frames y alternativas, sin llamarlos fonemas

El modelo actual permite obtener la matriz de log-probabilidades antes del
alineamiento. Se exportan blank, último carácter, top5clases y perfil de costo
de todas las salidas posibles del último carácter en la trellis CTC. Se
mantienen emisión, texto y penalización star, sin tuning.

La clase star es sintética, no una clase aprendida de silencio. Las salidas
son **grafemas**, no probabilidades de fonemas. Blank puede cubrir sonido sin
emitir otro carácter. No significa que el cantante haya dejado de vocalizar.

| Línea | End rev0 | Mejor trayectoria | Costo relativo de salir cerca de rev0 |
|---|---:|---:|---:|
| 7 | 55,15 | 54,90 | −17,47 |
| 34 | 238,49 | 238,22 | −23,06 |
| 41 | 283,52 | 280,58 | −2,58 |

Son diferencias de log-score de trayectorias, no confianza ni probabilidad de
corrección. En línea41 existe una trayectoria tardía mucho menos penalizada
que en los otros dos casos. Esto permite estudiar la ambigüedad del alineamiento
sin asumir que mover el argmax produzca un final perceptual correcto.

Luciano se mantuvo como control de repetición/no-daño. Se ejecutó actividad
en175,5–184,3, sin cambiar179,32 ni forzar extensión.

### Alternativas verificadas y bloqueo fonético

| Alternativa | Checkpoint/salida verificados | Límite para este uso |
|---|---|---|
| [SOFA](https://github.com/qiuqiao/SOFA/discussions/categories/pretrained-model-sharing) | Publicaciones de checkpoints de canto chino, inglés, japonés y francés; alineación por fonemas | No se verificó checkpoint ni inventario español compatible en las publicaciones inspeccionadas; no se sustituyó por diccionario chino |
| [SOFA-combined](https://huggingface.co/Silasimo/SOFA-combined) | Card declara canto sintético y partición inglesa deGTSinger | No prueba de compatibilidad fonética española |
| [STARS](https://github.com/gwx314/STARS) | Checkpoints publicados chino y chino-inglés; alineación y notas | No checkpoint español verificado; cambiar `ph_num` no crea pesos entrenados para otros fonemas |
| [ROSVOT/RWBD](https://github.com/RickyL-2000/ROSVOT#model-weights) | Enlace de pesos; notas y predictor de límites de palabra; autores declaran entrenamientoM4Singer | No se descargó ni ejecutó; entrenamiento publicado no verifica límites de palabras españolas. Nota no equivale a final de palabra |

No se afirma que no exista ningún modelo español: **no se encontró uno
fonético verificablemente utilizable entre estas alternativas**. Sí se encontró
y ejecutó el detector de actividad anterior. Son alcances distintos.

## Relojes y selector

Se reutilizó la auditoría de reloj: correspondencia mezcla/stem sigue sin
verificarse por falta de prueba del delay/padding del codificador del proveedor.
No se transfirieron timestamps ni se compararon curvas como si compartieran
el mismo cero. La mezcla sigue como señal de referencia. No se presupone que
el stem sea peor o mejor.

El selector ahora admite evidencia en reloj nativo de mezcla, verificado y
sin transferencia, sin exigir sincronía con un stem no utilizado. Se conservan
los requisitos de atribución de voz, evidencia fonética y protección humana.
Una intención `extend` que termine antes del baseline se rechaza; cualquier
reducción exige `reduction_evidence_verified`, además de los demás gates.

## Captura prospectiva, sin una sesión extra

Se preparó captura en la transacción normal de `save_document`, reutilizando
`AuditLog`: baseline y end/start enviados por el humano, deltas separados,
audioSHA/revisión, revisiones documentales, actor autenticado, timestamp y build.
No agrega formulario ni aprobación adicional. Una repetición idempotente no
duplica evidencia. Se excluyen aplicación de candidato, restore y cambios de
sistema; reordenaciones/cambios estructurales no se emparejan arbitrariamente.

`REVIEWER_TIMING_CAPTURE_ENABLED=0` por defecto. **Implementado, no desplegado
ni capturando nuevas decisiones todavía.** Para habilitar después del rollout
autorizado, registrar `REVIEWER_TIMING_CAPTURE_EPOCH` y build verificado posterior
al fix. No se declara que un cliente antiguo esté descartado sin atestación.

La autoría registrada significa petición autenticada del editor, no intención
manual demostrada para cada borde vecino. Se marca la asociación posicional
cuando falta ID estable y si la normalización cambió timing. Todo queda como
evidencia operativa, no anotación ciega, intervalo perceptual exacto ni gold
limpio automático. No se activa entrenamiento.

## Artefactos y ejecución

Privados en `.context/reviewer-shadow-artifacts/`:

- `text-frames-v1`: primer intento, incluida realineación que invade frase vecina.
- `text-frames-v2`: candidata acotada, correspondencias y tres trazasCTC+actividad;
  controlLuciano. Modelo ejecutado desde commit`82a5a210`.
- `veracity-vendor`: checkout upstream fijado, pesos y licencia, sin modificaciones.
- La reclasificación ortográfica posterior no vuelve a llamar a proveedores.

Python3.11.15 del entorno dedicado; torch/torchaudio existentes. No se modificó
código upstream ni dependencias generales para acomodar un Python incorrecto.
Comando de diagnóstico:

```sh
HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 PYTHONPATH=lyricgen/backend "$REVIEWER_PYTHON" lyricgen/backend/scripts/reviewer_text_and_frames.py model --root "$REVIEWER_ARTIFACTS" --output "$REVIEWER_NEW_RUN"
```

En la ejecución fijada, tres diagnósticosCTC sumaron8,80s y las cuatro
inferencias de actividad1,63s. Cero llamadas pagas nuevas. No hay factura de
CPU local: no se inventa costo monetario cero. Todavía no se midió ahorro humano.

El siguiente paso útil es vincular esta actividad y las trayectorias a
fonemas/voz objetivo con evidencia, no convertir activity>umbral en otro padding.
No se habilitó ninguna regla de timing ni se publicaron sugerencias nuevas.
