# Excepciones temporales de dependencias

El gate `backend/scripts/security_audit.py` permite únicamente los IDs exactos
de `backend/security_exceptions.json`; una excepción nueva o vencida rompe CI.
Una excepción ausente también rompe CI, salvo las marcadas
`platform_variant`: esos IDs siguen permitidos y sujetos a vencimiento cuando
aparecen, pero pip-audit no los reporta de forma uniforme entre la rueda macOS
de Torch y la rueda Linux `+cpu` del índice de PyTorch.

No hay excepciones activas. El 2-nov-2026 se reemplazó Torch 2.8 por 2.13.0
(CPU en Linux), que no presenta los avisos del baseline anterior en PyPI.
Se eliminaron las ocho excepciones vencidas sin prorrogar sus fechas.

TorchAudio 2.11.1 usa la ABI estable y admite Torch >= 2.11. Sus releases
conservan `forced_align`; la nota anterior sobre su eliminación quedó obsoleta.
La lectura acotada de audio se hace con SoundFile, porque `torchaudio.info`
y los backends anteriores de `load` sí se retiraron. Se mantienen el modelo,
su commit inmutable, `trust_remote_code=False`, los umbrales y el algoritmo CTC.

Fuentes: https://github.com/pytorch/audio/releases/tag/v2.11.1,
https://github.com/pytorch/audio/releases/tag/v2.10.0 y
https://pypi.org/pypi/torch/2.13.0/json.

El gate sigue fallando ante cualquier aviso nuevo no revisado; no se modificó
el auditor ni se desactivó ningún control de CI.

Validación de la actualización (2-nov-2026):

- 254 pruebas de CTC, Whisper, LoRA y Plan de entrega con el runtime nuevo.
- Pruebas nativas de WAV/FLAC/MP3, lectura por ventanas, mono/resampling,
  tokens repetidos de `forced_align` y forward de Whisper sin descargas.
- Comparación local del modelo español fijado en el código con Torch 2.8
  y 2.13 sobre voz sintética: muestras de audio y los 293 frames de
  alineación idénticos. Los log-puntajes difieren hasta 0.002979; no son
  idénticos numéricamente. Es una prueba de compatibilidad, no un nuevo
  benchmark de calidad sobre canciones.
- CI debe validar nuevamente toda la suite, renders y auditoría en Linux
  con las ruedas CPU exactas antes del merge.
