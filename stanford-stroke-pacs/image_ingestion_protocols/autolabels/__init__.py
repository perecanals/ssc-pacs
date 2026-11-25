"""Explicit registry of versioned study rules."""

from .crisp2 import BASELINE_TIMEPOINT, BASELINE_TIMEPOINT_V2

RULES = {(rule.name, rule.version): rule for rule in (BASELINE_TIMEPOINT, BASELINE_TIMEPOINT_V2)}
