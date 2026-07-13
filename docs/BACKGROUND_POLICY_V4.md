# Background policy v4 rollout

This policy separates creative direction from safety authorization without
changing any public endpoint, payload, route, authentication or permission.

## Invariants

- Only the raw operator-authored background prompt can authorize smoke, fog,
  haze, mist, steam or vapor.
- Lyrics, artist/title, genre, concept, a visual bible, a scene beat and Gemini
  output are creative context only; they can never authorize an effect.
- People, atmospheric effects and commercial brands are classified and
  enforced independently.
- Universal accounts never opt into people. Other accounts require both the
  existing free-background setting and an explicit operator request.
- Generated/preview assets enter a v4 cache only after content validation.
- Atmospheric default-deny applies to AI-generated assets. User uploads and
  curated library assets keep independent people/brand validation but are not
  rejected for intentional smoke/fog already present in the source.

## Feature flag

Set `BACKGROUND_SMOKE_POLICY_MODE` identically on API, Worker and ShortWorker:

- `off` (default): versioned caches and safety refactor are present, while the
  legacy atmospheric creative behavior remains unchanged.
- `shadow`: classify and record atmospheric detections without rejecting them.
- `enforce`: remove automatic atmospheric motifs, add the provider-boundary
  negative rail and reject detected atmospheric effects unless the operator
  explicitly requested one.

An invalid or missing value resolves to `off`. Startup logs contain
`[BG_POLICY][STARTUP]` with release, process, mode, version and cache namespace.
Fresh queued jobs carry an internal policy fingerprint; a mismatched worker
fails before generation. In `enforce`, legacy queued jobs without that token
also fail closed and can be retried after the deployment settles.

## Safe rollout

1. Deploy the same commit to staging API, Worker and ShortWorker with `shadow`.
2. Confirm all startup log lines report the same release and mode.
3. Exercise Auto, Lyrics, Prompt improved, Prompt literal and multi-scene.
4. Review `[BG_POLICY][SHADOW]` and `shadow_atmospherics_detected` observations.
5. Change all staging services to `enforce`, redeploy and repeat the smoke.
6. Promote the exact verified commit to production in `shadow` first.
7. Move production to `enforce` only after the observation window is clean.

## Rollback

Set the flag to `off` on all three services and redeploy the same commit. Cache
fingerprints isolate `off`, `shadow` and `enforce`, so a rollback cannot consume
an asset generated under a different policy mode. `off` disables atmospheric
prompt mutation and rejection, but intentionally keeps the v4 cache namespace;
the first request after a mode change can therefore miss cache and regenerate.
