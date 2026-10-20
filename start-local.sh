#!/bin/bash
set -euo pipefail
PROJECT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
exec "$PROJECT_DIR/vibe-local.sh" serve --host 127.0.0.1 --port "${VIBE_LOCAL_PORT:-8899}"
