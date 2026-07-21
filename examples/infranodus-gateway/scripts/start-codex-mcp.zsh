#!/bin/zsh
set -euo pipefail

readonly SERVICE_NAME="ouroboros-infranodus-gateway"
readonly NODE_BIN="/Users/heomin/.hermes/node/bin/node"
readonly SERVER_BIN="/Users/heomin/Projects/ouroboros-infranodus-gateway/dist/stdio.js"

api_key="$(/usr/bin/security find-generic-password -a "$USER" -s "$SERVICE_NAME" -w)"
if [[ -z "$api_key" ]]; then
  print -u2 "Missing Keychain item: $SERVICE_NAME"
  exit 78
fi

export INFRANODUS_API_KEY="$api_key"
exec "$NODE_BIN" "$SERVER_BIN"
