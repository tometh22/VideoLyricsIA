# CI rápido y merge queue de `staging`

## Estado seguro actual

El workflow ya acepta `pull_request`, `merge_group/checks_requested` y pushes a
`staging`. Agrega `backend-fast` (F821, compilación de todo Python y un único
head de Alembic) y conserva la suite completa en cada PR mientras
`STAGING_MERGE_QUEUE_ENABLED` no sea exactamente `true`.
El toggle solo saltea la suite del evento PR cuando su base es `staging`; los
PR a `main` y los PR apilados contra otras branches conservan la suite completa.

El contexto estable que debe proteger la rama al final es `ci-gate`:

- PR con la cola habilitada: exige `backend-fast`.
- `merge_group`: exige backend completo, frontend completo, colaboración real,
  Sentinel y las dos imágenes Docker.
- Push/merge a `staging`: vuelve a ejecutar esa suite completa.

No configurar `backend-fast` directamente como required check: GitHub aplica
los mismos checks requeridos al PR y al `merge_group`; como ese job solo existe
en PR, la cola esperaría un contexto que nunca llega. `ci-gate` resuelve esa
asimetría sin relajar el gate final.

## Bloqueo de plataforma confirmado (2026-09-03)

`tometh22/VideoLyricsIA` es un repositorio público propiedad de una cuenta de
usuario. GitHub ofrece merge queue solamente a repositorios públicos de una
organización, o privados de una organización con Enterprise Cloud. El API
confirma además que hoy no hay rulesets en el repositorio. Por eso no se debe
activar la variable ni reemplazar los required checks actuales todavía.

Referencia: <https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/configuring-pull-request-merges/managing-a-merge-queue>

## Secuencia de activación sin ventana desprotegida

1. Transferir el repositorio a una organización (público), o a una organización
   Enterprise Cloud si debe ser privado. Esto requiere decisión explícita del
   propietario; no es un cambio de CI.
2. Mergear este workflow manteniendo `backend` y `frontend` como checks
   requeridos y sin crear `STAGING_MERGE_QUEUE_ENABLED`. En ese estado el PR
   sigue pagando la suite completa: es intencional y fail-safe.
3. Abrir un PR de prueba y confirmar que existen y pasan `backend-fast`,
   `backend`, `frontend`, `editor-collaboration`, `sentinel`, `docker` y
   `ci-gate`.
4. Crear para la referencia exacta `refs/heads/staging` un ruleset que requiera
   PR, merge queue y el check `ci-gate`; sin bypass. Mantener la protección
   clásica existente durante esta prueba.
5. Encolar ese PR de prueba y confirmar que GitHub emite `merge_group`, que la
   suite completa corre sobre el SHA sintético y que `ci-gate` termina verde.
6. Reemplazar en la protección clásica los contextos `backend` y `frontend`
   por `ci-gate` (el contexto ya existe y fue observado en ambos eventos). No
   borrar la protección ni dejar una lista vacía en ningún paso.
7. Crear la variable de repositorio
   `STAGING_MERGE_QUEUE_ENABLED=true`. Recién aquí la suite completa deja de
   correr en el PR; sigue siendo obligatoria dentro de la cola y tras el merge
   a `staging`.
8. Validar un segundo PR: `backend-fast` + `ci-gate` en el PR; suite completa +
   `ci-gate` en `merge_group`; suite completa nuevamente en el push final.

Rollback: borrar (o poner en `false`) `STAGING_MERGE_QUEUE_ENABLED`. La suite
completa vuelve inmediatamente a todos los PR sin cambiar la protección.
