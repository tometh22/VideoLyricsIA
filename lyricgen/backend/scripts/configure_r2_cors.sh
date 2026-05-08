#!/usr/bin/env bash
# Apply the R2 CORS policy in scripts/r2_cors.json to the bucket.
#
# This must run once after the bucket is created (and again any time the
# allowed origins list in r2_cors.json changes). Browsers cache preflight
# responses for `MaxAgeSeconds` (3000s today) so a CORS change can take
# up to 50 minutes to fully propagate to active sessions.
#
# Why a script and not the dashboard: this is the only thing that lets a
# code change touch the upload flow without us also having to remember to
# click around the Cloudflare dashboard. The repo + this script become
# the source of truth.
#
# Required env (or export them ahead of time):
#   R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY  Cloudflare R2 credentials
#   R2_BUCKET                               e.g. "genly-uploads"
#   R2_ENDPOINT_URL                         e.g. "https://<accountid>.r2.cloudflarestorage.com"
#
# Run:
#   ./scripts/configure_r2_cors.sh           # apply policy
#   ./scripts/configure_r2_cors.sh --get     # print the current policy
#
# Requires the AWS CLI v2 (`aws --version`). Install: https://aws.amazon.com/cli/
set -euo pipefail

cd "$(dirname "$0")"
POLICY_FILE="r2_cors.json"

: "${R2_ACCESS_KEY_ID:?R2_ACCESS_KEY_ID not set}"
: "${R2_SECRET_ACCESS_KEY:?R2_SECRET_ACCESS_KEY not set}"
: "${R2_BUCKET:?R2_BUCKET not set}"
: "${R2_ENDPOINT_URL:?R2_ENDPOINT_URL not set}"

export AWS_ACCESS_KEY_ID="$R2_ACCESS_KEY_ID"
export AWS_SECRET_ACCESS_KEY="$R2_SECRET_ACCESS_KEY"
# R2 doesn't care about region, but the AWS CLI insists on one.
export AWS_DEFAULT_REGION="auto"

if [[ "${1:-}" == "--get" ]]; then
  echo "Current CORS policy on s3://${R2_BUCKET}:"
  aws s3api get-bucket-cors \
    --bucket "$R2_BUCKET" \
    --endpoint-url "$R2_ENDPOINT_URL"
  exit 0
fi

echo "Applying ${POLICY_FILE} to s3://${R2_BUCKET} via ${R2_ENDPOINT_URL}…"
aws s3api put-bucket-cors \
  --bucket "$R2_BUCKET" \
  --cors-configuration "file://${POLICY_FILE}" \
  --endpoint-url "$R2_ENDPOINT_URL"

echo "OK. New policy:"
aws s3api get-bucket-cors \
  --bucket "$R2_BUCKET" \
  --endpoint-url "$R2_ENDPOINT_URL"
echo
echo "NOTE: browser preflight is cached for MaxAgeSeconds (3000s)."
echo "      Sessions opened before this change may take up to 50 min"
echo "      to pick up the new origins list."
