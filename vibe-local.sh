#!/bin/bash
set -euo pipefail
PROJECT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
export VIBE_TRADING_HOME="$PROJECT_DIR/.runtime"
export VIBE_TRADING_ENABLE_SHELL_TOOLS=0
export VIBE_TRADING_CHANNELS_AUTO_START=0
export VIBE_TRADING_ALLOWED_FILE_ROOTS="$PROJECT_DIR/.runtime/workspace"
export VIBE_TRADING_ALLOWED_WRITE_ROOTS="$PROJECT_DIR/.runtime/workspace"
export VIBE_TRADING_ALLOWED_RUN_ROOTS="$PROJECT_DIR/.runtime/runs"
export MPLCONFIGDIR="$PROJECT_DIR/.runtime/cache/matplotlib"
export XDG_CACHE_HOME="$PROJECT_DIR/.runtime/cache"
export PYTHONPATH="$PROJECT_DIR/agent${PYTHONPATH:+:$PYTHONPATH}"
export PATH="$PROJECT_DIR/.venv/bin:$PROJECT_DIR/.bootstrap/node-runtime/node_modules/node/bin:$PATH"
mkdir -p "$VIBE_TRADING_HOME/workspace" "$MPLCONFIGDIR"
cd "$PROJECT_DIR/agent"
exec "$PROJECT_DIR/.venv/bin/python" -m cli._legacy "$@"
