#!/bin/sh
# Process layout for one Genly deployment on the OVH host.
#
# Staging and production are two deployments (separate DB, Redis and RQ
# prefixes) that share the CPU. Both must export the same
# PRORES_SEMAPHORE_NAME and PRORES_SEMAPHORE_DIR so the encode cap and the
# portal-demand markers are host-wide. Railway service settings are not
# this file: after 2026-12-01 this script and deploy/ovh/docker-compose.yml
# are the source of truth.
#
# Usage, from the repo root, one process per role:
#   deploy/ovh/start-workers.sh render
#   deploy/ovh/start-workers.sh default
#   deploy/ovh/start-workers.sh prores
#   deploy/ovh/start-workers.sh interactive
#   deploy/ovh/start-workers.sh batch-short
#   deploy/ovh/start-workers.sh batch-render
#   deploy/ovh/start-workers.sh quality
#
# Run more ProRes processes than PRORES_MAX_CONCURRENCY so a download does
# not consume every encode permit. The API process needs the same
# PRORES_PORTAL_QUEUE, PRORES_PREWARM_QUEUE, PRORES_SEMAPHORE_NAME and
# PRORES_SEMAPHORE_DIR; it writes the portal markers and does not run ffmpeg.

set -eu

ROLE="${1:-}"
ROOT="$(CDPATH= cd -- "$(dirname "$0")/../.." && pwd)"
cd "$ROOT"

export PYTHONPATH="${PYTHONPATH:-$ROOT/lyricgen/backend}"
export PRORES_PORTAL_QUEUE="${PRORES_PORTAL_QUEUE:-prores_portal}"
export PRORES_PREWARM_QUEUE="${PRORES_PREWARM_QUEUE:-prores_prewarm}"
export PRORES_SEMAPHORE_DIR="${PRORES_SEMAPHORE_DIR:-/var/lock/genly-prores}"
export PRORES_SEMAPHORE_NAME="${PRORES_SEMAPHORE_NAME:-prores}"
mkdir -p "$PRORES_SEMAPHORE_DIR"

WORKER="python lyricgen/backend/worker.py"

case "$ROLE" in
  render)
    # No `default` and no ProRes queues. Canary stays with render.
    exec env QUEUES=enterprise,canary $WORKER
    ;;
  default)
    # Outbox reconciler and the rest of the light queue.
    exec env QUEUES=default $WORKER
    ;;
  prores)
    # Strict order: portal, then prewarm. Nothing else.
    exec env QUEUES=prores_portal,prores_prewarm $WORKER
    ;;
  interactive)
    exec env WHISPER_PRELOAD=1 QUEUES=transcription,bg_preview,audio_preview $WORKER
    ;;
  batch-short)
    exec env WHISPER_PRELOAD=1 WORKLOAD_CLASS=batch QUEUES=campaign_control,transcription_batch $WORKER
    ;;
  batch-render)
    exec env WORKLOAD_CLASS=batch QUEUES=batch_render $WORKER
    ;;
  quality)
    # WHISPER_PRELOAD stays off. The rest of the quality env comes from
    # the service environment, same values as railway/quality-worker.toml.
    exec env QUEUES=transcription_quality $WORKER
    ;;
  *)
    echo "usage: $0 {render|default|prores|interactive|batch-short|batch-render|quality}" >&2
    exit 2
    ;;
esac
