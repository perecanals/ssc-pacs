#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PYTHON="/home/perecanals/miniconda3/envs/pacs/bin/python"

if [ ! -x "$PYTHON" ]; then
    echo "Error: Python not found at $PYTHON" >&2
    exit 1
fi

exec "$PYTHON" "$SCRIPT_DIR/bulk_set_label_values.py" "$@"
