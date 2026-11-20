#!/usr/bin/env sh
set -e

REPO_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
PY_BIN="$(command -v python3 || command -v /opt/homebrew/bin/python3 || command -v python)"

if [ -z "$PY_BIN" ]; then
  echo "Error: Python 3.10+ is required." >&2
  exit 1
fi

export PYTHONPATH="$REPO_DIR/src${PYTHONPATH:+:$PYTHONPATH}"
exec "$PY_BIN" -m remote_fleet bootstrap "$@"
