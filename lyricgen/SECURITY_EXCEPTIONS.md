# Excepciones temporales de dependencias

El gate `backend/scripts/security_audit.py` permite únicamente los IDs exactos
de `backend/security_exceptions.json`; una excepción nueva, vencida o ya
corregida rompe CI.

Las excepciones actuales se limitan al motor CTC: `torch`/`torchaudio` 2.8 no
pueden subir mientras se use `torchaudio.functional.forced_align`, eliminado en
2.9. Los modelos remotos están restringidos en `ctc_align.py` a un ID conocido,
un commit SHA inmutable y `trust_remote_code=False`. `transformers` permanece en
la rama 4 hasta validar su major 5 con el benchmark de alineación. `ecdsa` llega
por `python-jose`, pero los tokens de la aplicación aceptan sólo HS256.

Propietario: backend/video. Vencimiento máximo: 2026-10-31. Antes de esa fecha
hay que vendorizar el Viterbi de forced-align o reemplazar el alineador, migrar
el JWT a una librería mantenida y volver a generar el baseline por ID.
