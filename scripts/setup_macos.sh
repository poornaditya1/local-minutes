#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "This setup script is intended for macOS."
fi

PYTHON_BIN="${PYTHON_BIN:-python3}"

if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
  echo "python3 was not found. Install Python 3.10 or newer, then run this script again."
  exit 1
fi

"$PYTHON_BIN" - <<'PY'
import sys
if sys.version_info < (3, 10):
    raise SystemExit("Python 3.10 or newer is required.")
print(f"Using Python {sys.version.split()[0]}")
PY

if ! command -v uv >/dev/null 2>&1; then
  echo "uv is required but not found. Install it from https://docs.astral.sh/uv/getting-started/installation/."
  exit 1
fi

if [[ ! -d .venv ]]; then
  uv venv .venv
fi

uv sync

cat <<'TXT'

Local Minutes is installed.

Next steps:
1. Start LM Studio and load a chat model.
2. Start the LM Studio Local Server.
3. For system audio, install BlackHole 2ch, Loopback, VB-Cable, or another virtual audio device.
4. Run ./run_macos.command, then open http://127.0.0.1:8765

Running a quick device check now.
TXT

python scripts/macos_audio_check.py || true
