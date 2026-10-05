#!/usr/bin/env bash
set -euo pipefail
if [[ -z "${BOUNTY_QUEUE_CANARY:-}" ]]; then
  printf '%s\n' 'BOUNTY_HYP8 source=script canary=absent'
  exit 42
fi
digest="$(printf '%s' "$BOUNTY_QUEUE_CANARY" | sha256sum | cut -d' ' -f1)"
printf 'BOUNTY_HYP8 source=script canary_sha256=%s\n' "$digest"
# retrigger 232243
# v3 233347
