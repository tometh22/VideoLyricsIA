# Backend tests

## Correr todos los tests (modo SQLite, default local)

```bash
cd lyricgen/backend
pip install -r requirements.txt pytest httpx
pytest tests/ -v
```

Los tests con marker `@pytest.mark.postgres` se **skipean** en SQLite
porque dependen de features que SQLite no soporta (row locks con
`with_for_update()`, `pg_try_advisory_lock`, JSONB). El skip message
te dice por qué.

## Correr tests Postgres-only

Necesitás Postgres corriendo. Lo más rápido vía docker:

```bash
docker run -d --name pg-test -p 5432:5432 \
    -e POSTGRES_USER=test -e POSTGRES_PASSWORD=test \
    -e POSTGRES_DB=lyricgen_test postgres:18

# en otra terminal:
DATABASE_URL=postgresql://test:test@localhost:5432/lyricgen_test \
    pytest tests/ -m postgres -v
```

Cuando termines:
```bash
docker stop pg-test && docker rm pg-test
```

## CI

`.github/workflows/ci.yml` levanta Postgres 18 como service automáticamente
para el job `backend`. Todos los tests (incluidos los `@postgres`) corren
contra esa DB. No tenés que hacer nada — el `DATABASE_URL` se setea por
env del workflow.

## Convenciones

- **Un test por bug**: cuando arreglás un bug crítico, agregá un test
  que falla ANTES de la fix y pasa DESPUÉS.
- **Marker `@pytest.mark.postgres`** cuando el test depende de algo
  Postgres-only. Sin esto, el test daría false-green en SQLite (donde
  la feature es no-op).
- **Race conditions**: usar `threading.Thread` + `threading.Event` —
  patrón ya establecido en `test_prores_concurrent.py:156-171` y
  `test_edit_race.py`.
- **Fixtures disponibles** (ver `conftest.py`): `client`, `db`,
  `admin_token`, `user_token`, `unauthorized_user_token`. Helper:
  `auth(token)` devuelve `{Authorization: Bearer ...}`.

## Cómo modificar el comportamiento postgres-skip

`conftest.py:pytest_collection_modifyitems` detecta el dialect de la
DB y skipea tests con marker `postgres` si no es Postgres. Si querés
forzar que corran contra SQLite (raramente útil), borrá la línea del
marker en el test específico — pero entendé que el resultado puede
ser false-green.
