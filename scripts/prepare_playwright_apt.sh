#!/usr/bin/env bash
set -euo pipefail
# GitHub's image includes Google Chrome's APT feed. Playwright downloads its own
# pinned Chromium and needs only Ubuntu system libraries. A stale Chrome feed
# must not break apt update with Hash Sum mismatch before any browser test runs.
# Keep integrity checking enabled. Disable only that unrelated feed, recoverably,
# on the disposable CI runner; never skip installation or browser/E2E gates.
for source in /etc/apt/sources.list.d/*.list /etc/apt/sources.list.d/*.sources; do
  if [[ -f "$source" ]] && grep -q 'dl.google.com/linux/chrome-stable/deb' "$source"; then
    sudo mv -- "$source" "${source}.genly-ci-disabled"
    echo "Disabled unrelated Chrome APT feed: $source"
  fi
done
