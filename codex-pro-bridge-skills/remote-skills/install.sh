#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"
SOURCE="$ROOT_DIR/gpt-pro-bridge-client"

if [[ "${1:-}" != "--global" || $# -ne 1 ]]; then
  echo "Usage: $0 --global" >&2
  exit 2
fi

[[ -d "$SOURCE" ]] || { echo "Missing remote client source: $SOURCE" >&2; exit 2; }
DEST_ROOT="${CODEX_HOME:-$HOME/.codex}/skills"
DEST="$DEST_ROOT/gpt-pro-bridge-client"
mkdir -p "$DEST_ROOT"
STAGE="$(mktemp -d "$DEST_ROOT/.gpt-pro-bridge-client-install.XXXXXX")"
trap 'rm -rf "$STAGE"' EXIT

cp -R "$SOURCE" "$STAGE/gpt-pro-bridge-client"
find "$STAGE/gpt-pro-bridge-client" -type d -name __pycache__ -prune -exec rm -rf {} +
find "$STAGE/gpt-pro-bridge-client" -type f \( -name '*.pyc' -o -name '*.pyo' \) -delete
rm -rf "$DEST"
mv "$STAGE/gpt-pro-bridge-client" "$DEST"
rmdir "$STAGE"
trap - EXIT

echo "Installed gpt-pro-bridge-client"
echo "Destination: $DEST"
echo "Run prepare_review_bundle.py configure with deployment-local dispatcher IDs before first use."
