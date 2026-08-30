# Selector de confianza + poda — estado 2026-08-30

**Segundos de revisión/canción: 127,1 → pendiente.** No se publica un número
parcial como resultado del bloque.

## Implementado

- Bundle determinista para los 15 stems `mdx_extra` faltantes, sin
  credenciales, con SHA-256 del master de entrada y del stem de salida.
- Importador transaccional: rechaza IDs, hashes, modelo, duración o archivo
  inesperados antes de modificar el cache.
- Telemetría por línea de los dos testigos CTC (global/local), anclas,
  ocurrencias y scores. Una abstención jerárquica es un negativo duro.
- Selector de timing con `LeaveOneGroupOut`, VAD del stem y curvas para
  precisión 90/93/95. Vivo queda excluido de cualquier aprobación automática.
- Dos rankers de flags, texto y timing, también `LeaveOneGroupOut`, con costo
  explícito de 10 s por flag falso y 60 s por corrección perdida.
- Timing aprobado por el selector sale sólo de la fuente de timing; jamás
  borra un flag de texto pendiente.
- Gate MSS-ALT pareado: CI95 de mejora relativa y veto a cualquier canción que
  empeore más de 2 puntos absolutos de WER.
- Agregador final que exige cohorte completa y propagación MSS aguas abajo. No
  prepara staging con resultados parciales.

## Bloqueo externo comprobado

`runpodctl` responde `API_KEY_MISSING`. El bundle local contiene 15 canciones,
709.515.347 bytes y SHA-256
`fd80406ad3d678625bb41804e651bf22464a18581a448a322b7f05ad253064f0`.
La clave antes pegada en el chat no se reutiliza.

Para habilitar el canal seguro, Tomi debe ejecutar en Terminal:

```zsh
/Users/tomi/conductor/workspaces/VideoLyricsIA-main/riyadh/.context/bin/runpodctl doctor
```

La clave se pega en ese prompt privado. No debe enviarse por chat. Después de
eso el bloque continúa: procesar/importar los 15 stems, re-ejecutar ambos
alineadores sobre la cohorte, selector, poda, MSS-ALT y el reporte conjunto.

## Validación local

59 tests pasan. La corrida parcial queda marcada
`BLOCKED_INCOMPLETE_COHORT`; no es un NO_GO del selector ni una autorización de
staging.
