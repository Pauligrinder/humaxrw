#!/usr/bin/env bash
# Run the HumaxRW GUI from source (Linux/macOS). Needs python3 + tkinter.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"
exec python3 "$ROOT/humaxrw_gui.py" "$@"
