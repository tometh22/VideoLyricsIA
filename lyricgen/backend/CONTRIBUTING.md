# Patterns to follow (backend)

Convenciones extraídas de bugs reales que pisamos. Cuando agregues
código nuevo, seguí estos patrones para no re-introducir esos bugs.

## 1. Pydantic max_length en TODO input de cliente

**Regla**: cada campo `str` en un `BaseModel` o `Form()` que reciba
data del cliente DEBE tener `Field(max_length=...)` (o `Form(...,
max_length=...)`).

**Por qué**: sin esto, un cliente puede mandar 100 MB de string que
llena DB / RAM. DoS trivial. Audit del 2026-05-11 encontró ~12 modelos
sin límites. Ya fixeado pero el patrón se debe mantener.

**Tamaños sugeridos**:
| Tipo de campo | max_length |
|---|---|
| Identifiers (job_id, tenant_id) | 64 |
| FPS / frame_size / profile codes | 4–16 |
| Lang codes (es, en-US, …) | 16 |
| Usernames / artist names | 200 |
| Song titles | 300 |
| Email (RFC 5321) | 320 |
| Tokens / JWT | 500 |
| Style prompts / concepts | 2000–4000 |
| Free-form notes | 2048 |
| JSON arbitrario (segments_json) | 5_000_000 (5 MB) |

```python
class MyRequest(BaseModel):
    user_id: str = Field(..., max_length=64)
    notes: str = Field(default="", max_length=2048)
```

## 2. Locking para read-modify-write en la misma row

**Regla**: cuando un endpoint lee una columna, valida una condición,
y la mutaaza basándose en esa validación, usar `with_for_update()`.
Sin esto, dos requests concurrentes pueden ambos pasar el check y
ambos mutar → invariante roto.

**Por qué**: el `edit_count` de jobs (límite 3 edits) se podía saltar
con requests concurrentes. Cada edit con motion gasta Veo (~$0.90)
→ exploit con costo real.

```python
job = (
    db.query(JobModel)
    .filter(JobModel.job_id == job_id)
    .filter(JobModel.tenant_id == current_user["tenant_id"])
    .with_for_update()  # ← lockea la row en Postgres
    .first()
)
# ... read, validate, write ...
db.commit()  # ← libera el lock
```

**Limitación**: `with_for_update()` es no-op en SQLite. Tests de
concurrency deben usar marker `@pytest.mark.postgres` para no dar
false-green.

## 3. Capturar previous state ANTES de mutar (para audit logs)

**Regla**: cuando agregás un `AuditLog` que registra "previous_X",
capturá el valor en una variable local ANTES del mutate.

**Por qué**: en `/retry/{job_id}` el audit log decía
`detail["previous_status"]=job.status` y la línea anterior había
mutado job.status a "processing". Resultado: 100% de los retry logs
decían `previous_status="processing"`, inservibles.

```python
_previous_status = job.status           # capture antes
job.status = "processing"               # mutate después
db.add(AuditLog(
    action="job.retry",
    detail={"previous_status": _previous_status},   # usa la captura
))
```

## 4. Frontend fetch SIEMPRE chequea res.ok

**Regla** (frontend, no backend pero relacionado): después de cada
`await fetch(...)` que no sea GET, verificar `res.ok`. Si no, lanzar
con `data.detail`.

```jsx
const res = await fetch(url, { method: "DELETE", headers: ... });
if (!res.ok) {
  const data = await res.json().catch(() => ({}));
  throw new Error(data.detail || `Error ${res.status}`);
}
```

**Detección automática**: `npm run check:fetch` corre
`frontend/scripts/check-fetch-error-handling.mjs` y falla si encuentra
un fetch DELETE/POST/PATCH/PUT sin chequeo. Integrado en `npm run build`.

## 5. Logging structured con tenant_id + job_id

**Regla** (parcialmente migrado): en lugar de `print(f"[STAGE] msg")`,
usar `logger.info("msg", extra={"job_id": ..., "tenant_id": ...,
"stage": ...})`. `observability.py:_JsonFormatter` extrae esos
attrs automáticamente para Sentry / structured logs.

**Estado actual**: `pipeline.py` tiene ~119 `print()` legacy.
Migración a logger pendiente, defer. Para CÓDIGO NUEVO usar logger
desde el día 1.

## 6. Multi-tenant: filtrar por tenant_id en TODA query de jobs

**Regla**: cualquier query `db.query(Job).filter(Job.job_id == ...)`
DEBE también filtrar por `Job.tenant_id == current_user["tenant_id"]`.
Sin esto, leaks entre tenants.

```python
# WRONG
db.query(Job).filter(Job.job_id == job_id).first()

# RIGHT
db.query(Job).filter(Job.job_id == job_id).filter(
    Job.tenant_id == current_user["tenant_id"]
).first()
```

Admin role es la única excepción documentada (`_job_scope(user)`
handles esto).

## 7. SSE / long-running endpoints: re-validar scope en cada poll

**Regla**: en endpoints que mantienen conexión abierta (SSE, websocket),
re-validar permisos en cada iteración del loop. La auth inicial no
basta — el user puede ser movido de tenant mid-stream.

Ver `/events/{job_id}` en `main.py` como referencia.
