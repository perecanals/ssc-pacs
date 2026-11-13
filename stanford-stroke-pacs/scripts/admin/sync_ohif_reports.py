#!/usr/bin/env python3
"""Catalogue existing OHIF reports; dry-run unless --execute is supplied."""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "web-app"))
from report_catalog import catalog_reports  # noqa: E402

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--study")
    args = parser.parse_args()
    print(json.dumps(catalog_reports(args.study, execute=args.execute), indent=2))
