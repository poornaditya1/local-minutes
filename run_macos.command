#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

if ! command -v uv >/dev/null 2>&1; then
  echo "uv is required but not found. Install it from https://docs.astral.sh/uv/getting-started/installation/."
  exit 1
fi

if [[ ! -d .venv ]]; then
  echo "Creating Python virtual environment with uv..."
  uv venv .venv
fi

uv sync --quiet

(sleep 2; open http://127.0.0.1:${LOCAL_MINUTES_PORT:-8765}) >/dev/null 2>&1 &
uv run python run.py
