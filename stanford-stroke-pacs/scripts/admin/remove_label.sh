#!/usr/bin/env bash
set -euo pipefail

if [ $# -lt 1 ]; then
    echo "Usage: bash remove_label.sh <label_name> [--yes]"
    exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=../_lib.sh
source "$SCRIPT_DIR/../_lib.sh"

PYTHON="$(resolve_python)" || { echo "Error: no usable Python found (set SSC_PYTHON to override)" >&2; exit 1; }

exec "$PYTHON" "$SCRIPT_DIR/remove_label.py" "$@"
