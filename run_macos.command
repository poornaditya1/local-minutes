#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

if [[ ! -d .venv ]]; then
  echo "Creating Python virtual environment..."
  python3 -m venv .venv
fi

source .venv/bin/activate
python -m pip install -q -U pip setuptools wheel
python -m pip install -q -e .

(sleep 2; open http://127.0.0.1:${LOCAL_MINUTES_PORT:-8765}) >/dev/null 2>&1 &
python run.py
