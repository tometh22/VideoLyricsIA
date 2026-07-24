# Production remediation cutover

This change is promoted one wave at a time: PR to `staging`, CI, staging soak,
independent review, then a fresh explicit production approval. An open P0/P1
stops promotion. Database migrations, `auth_version` bumps and secret rotations
are forward-only; rollback reverts compatible code/config and preserves schema.

## One-time Railway service contract

Set all three services to Root Directory `/lyricgen`; Config File remains
repository-absolute because Railway does not resolve it below Root Directory:

| Service | Config File | Required variables | Replicas |
|---|---|---|---:|
| API | `/railway/api.toml` | `RQ_PAYLOAD_VERSION=2`, `FLEET_READINESS_STRICT=1` | 2 |
| Worker | `/railway/worker.toml` | `QUEUES=enterprise,default,canary`, `RAILWAY_SHUTDOWN_TIMEOUT_SECONDS=1200`, `RQ_PAYLOAD_VERSION=2` | 7 |
| ShortWorker | `/railway/short-worker.toml` | `QUEUES=transcription,bg_preview`, `RAILWAY_SHUTDOWN_TIMEOUT_SECONDS=1200`, `RQ_PAYLOAD_VERSION=2` | 3 |

Do not change `us-west2` in this recovery. Before cutover, export the effective
service configuration and confirm root/config paths, region, replica counts,
draining budget and environment-scoped secrets. Docker builds for API and both
worker services must include `/app/assets`.

## Staging gate

1. Run migrations and confirm the single Alembic head plus `users.auth_version`
   and `jobs.segments_revision` defaults.
2. Exercise RQ v1 (metadata-less) and v2 payloads; reject every other version.
3. Confirm API, Worker and ShortWorker heartbeats report the same release SHA.
4. Run synthetic auth, upload, 55+ line editor, reanchor, edit, background,
   render/download, degraded-R2 and rollback drills. Official anchor lyrics stay
   opt-in and may be enabled only with the three SHAs equal.
5. Confirm `features.youtube_publish=false` in capabilities and direct YouTube
   OAuth/callback/mutation calls return `feature_disabled`.
6. Confirm browser/network logs contain zero `tt=access` query tokens.

## Production maintenance cutover

1. Announce the window. Set `SUBMISSIONS_PAUSED=1` on the API service (the
   fail-closed bootstrap guard), then set the Redis control with an expiry:
   `PUT /admin/ops/submissions` using `{paused:true, reason, until, retry_after}`.
2. Wait for every queue and in-flight registry to reach zero. Scale API to zero
   so an old replica cannot enqueue after the check.
3. Trigger the API deployment for the target SHA while the service is still
   paused. Its configured `preDeployCommand` executes exactly
   `bash /app/backend/scripts/prod_migrate.sh` from the newly built image before
   either replica starts. Verify columns, defaults and Alembic head. The new
   replicas must remain out of rotation with `worker_fleet_incoherent` until
   the worker fleet reaches the same SHA/protocol.
4. Deploy ShortWorker and Worker at that exact SHA. Verify all four queue
   consumers, assets, RQ protocol and release heartbeats; `/health/ready` must
   then become 200. It requires DB+Redis but remains ready when only R2 is
   degraded.
5. Keep API at two replicas and submissions paused for synthetic checks.
6. Run production synthetic login, upload initiation (expected paused), editor
   autosave, render canary and download checks.
7. Reopen Redis submissions with `{paused:false}`, verify the response and
   health snapshot, then set `SUBMISSIONS_PAUSED=0`. Monitor conflicts, rejected
   sessions, incompatible RQ payloads, upload retries, divergent SHAs and pause
   state.

## Security sequence

Stop Sentinel or remove its token first. Configure redaction and quiet
`httpx/httpcore` logs, validate with a canary token, then revoke the old Telegram
token in BotFather and restart with the replacement. Treat retained logs as
compromised and restrict access/retention.

After session tests pass across two API replicas, execute
`POST /admin/ops/logout-all` with the explicit confirmation phrase. Do not rotate
`JWT_SECRET` for this event: `auth_version` invalidates access/media tokens and
session revocation invalidates their JTIs without split-brain between replicas.

## Rollback

Pause submissions first. Roll back only to code that understands RQ v1/v2 and
the additive columns. Never downgrade the migrations, decrement `auth_version`,
restore revoked sessions, or restore the old Telegram token. If no compatible
artifact exists, keep the system paused and roll forward.
