"""Put the package dir on sys.path so tests import the modules directly.

The ingestion protocol is a flat script directory, not an installed package;
`pytest` from either the package dir or the repo root works with this shim.
"""

import sys
from pathlib import Path

_PKG_DIR = str(Path(__file__).resolve().parents[1])
if _PKG_DIR not in sys.path:
    sys.path.insert(0, _PKG_DIR)
