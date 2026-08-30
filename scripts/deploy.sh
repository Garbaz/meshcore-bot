#!/bin/bash
# Deploy meshcore-bot to a remote host via rsync and restart the supervisor service.
#
# Usage: ./scripts/deploy.sh DEST [DEST_DIR] [SUPERVISOR_NAME]
#   DEST             required: SSH host (e.g. tobi@192.168.178.56)
#   DEST_DIR         optional: remote directory (default: ~/meshcore-bot/)
#   SUPERVISOR_NAME  optional: supervisor program name (default: meshcore-bot)

set -euo pipefail

DEST="${1:-}"
DEST_DIR="${2:-~/meshcore-bot/}"
SUPERVISOR_NAME="${3:-meshcore-bot}"

if [ -z "$DEST" ]; then
    echo "usage: $0 DEST [DEST_DIR] [SUPERVISOR_NAME]" >&2
    exit 1
fi

echo "deploying to $DEST:$DEST_DIR (supervisor: $SUPERVISOR_NAME)"

rsync -vhra ./ "$DEST:$DEST_DIR" \
    --include='**.gitignore' \
    --exclude='/.git' \
    --filter=':- .gitignore' \
    --delete-after

ssh -t "$DEST" sudo supervisorctl restart "$SUPERVISOR_NAME"
