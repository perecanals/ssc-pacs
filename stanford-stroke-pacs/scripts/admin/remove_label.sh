#!/usr/bin/env bash
set -euo pipefail

if [ $# -lt 1 ]; then
    echo "Usage: sudo bash remove_label.sh <label_name>"
    exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PYTHON="/home/perecanals/miniconda3/envs/pacs/bin/python"

if [ ! -x "$PYTHON" ]; then
    echo "Error: Python not found at $PYTHON" >&2
    exit 1
fi

exec "$PYTHON" "$SCRIPT_DIR/remove_label.py" "$@"
