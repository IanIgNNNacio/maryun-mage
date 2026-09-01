"""Temporal gate — enforces the closed-months-only rule across the pipeline."""
from app.temporal.gate import (
    TemporalGate,
    TemporalWindow,
    build_temporal_gate,
)
from app.temporal.validators import TemporalViolation, assert_within_window

__all__ = [
    "TemporalGate",
    "TemporalWindow",
    "TemporalViolation",
    "assert_within_window",
    "build_temporal_gate",
]
