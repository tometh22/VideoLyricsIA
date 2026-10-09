# Excepciones temporales de dependencias

El gate `backend/scripts/security_audit.py` permite únicamente los IDs exactos
de `backend/security_exceptions.json`; una excepción nueva o vencida rompe CI.
Una excepción ausente también rompe CI, salvo las marcadas
`platform_variant`: esos IDs siguen permitidos y sujetos a vencimiento cuando
aparecen, pero pip-audit no los reporta de forma uniforme entre la rueda macOS
de Torch y la rueda Linux `+cpu` del índice de PyTorch.

Las excepciones actuales se limitan al motor CTC: `torch`/`torchaudio` 2.8 no
pueden subir mientras se use `torchaudio.functional.forced_align`, eliminado en
2.9. Los modelos remotos están restringidos en `ctc_align.py` a un ID conocido,
un commit SHA inmutable y `trust_remote_code=False`. `transformers` se subió a la
5.18.0 el 1-oct-2026 (la serie 4 terminó en 4.57.6, afectada, y las correcciones
sólo existen en la 5.x): se validó con el modelo real, cuyas emisiones CTC
resultaron idénticas bit a bit a las de 4.57.6, y quedó sin excepciones. El JWT pasó de `python-jose`
(sin mantenimiento; CVE-2026-85394) a PyJWT ≥ 2.15.0 el 9-oct-2026, y con eso se
fueron las excepciones de `python-jose` y `ecdsa`.

Propietario: backend/video. Vencimiento máximo: 2026-10-31. Antes de esa fecha
hay que vendorizar el Viterbi de forced-align o reemplazar el alineador y volver
a generar el baseline por ID.
