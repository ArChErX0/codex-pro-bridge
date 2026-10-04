#!/usr/bin/env bash
set -euo pipefail
PACKAGE_ROOT="$(cd "$(dirname "$0")" && pwd)"
bridge_python="${BRIDGE_PYTHON:-}"
if [[ -z "$bridge_python" ]]; then
  for candidate in python3.13 python3.12 python3.11 python3; do
    if command -v "$candidate" >/dev/null && "$candidate" -c 'import sys; sys.exit(sys.version_info < (3, 10))' >/dev/null 2>&1; then
      bridge_python="$candidate"
      break
    fi
  done
fi
[[ -n "$bridge_python" ]] || { echo 'Python is missing; full setup needs 3.11+, skills-only needs 3.10+.' >&2; exit 2; }
case "${1:-}" in
  --setup) shift; exec "$bridge_python" "$PACKAGE_ROOT/setup_bridge.py" install "$@" ;;
  --global) shift; exec "$bridge_python" "$PACKAGE_ROOT/setup_bridge.py" skills "$@" ;;
  --repo) shift; exec "$bridge_python" "$PACKAGE_ROOT/setup_bridge.py" skills --repo-local --repo "$@" ;;
  *) echo 'Usage: install.sh --setup --repo PATH [setup options] | --global | --repo PATH' >&2; exit 2 ;;
esac
