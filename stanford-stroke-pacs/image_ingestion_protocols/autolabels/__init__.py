"""Trusted, installation-owned rule packages."""

import importlib.util
import os
import re
import sys
from pathlib import Path

from .model import EnrollmentRule, StudyRule

DEFAULT_DIRECTORY = Path(__file__).resolve().parents[2] / "local_rules"


def load_packages(directory):
    rules, anchors = {}, {}
    for entry in sorted(directory.glob("*/__init__.py")):
        name = entry.parent.name
        if not name.isidentifier():
            raise ValueError(f"Invalid local rule package name: {name}")
        module_name = f"ssc_local_{name}"
        spec = importlib.util.spec_from_file_location(module_name, entry)
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)
        for rule in module.RULES:
            key = (rule.name, rule.version)
            if not isinstance(rule, (StudyRule, EnrollmentRule)) or key in rules:
                raise ValueError(f"Invalid or duplicate local rule {key} in {entry}")
            rules[key] = rule
        for strategy, offsets in getattr(module, "ANCHORS", {}).items():
            if (
                strategy in anchors
                or not offsets
                or any(not re.fullmatch("[a-z_][a-z0-9_]*", col) for col, _ in offsets)
            ):
                raise ValueError(f"Invalid or duplicate anchor strategy {strategy} in {entry}")
            anchors[strategy] = tuple(offsets)
    return rules, anchors


RULES, ANCHORS = load_packages(Path(os.environ.get("SSC_LOCAL_RULES_DIR", DEFAULT_DIRECTORY)))
