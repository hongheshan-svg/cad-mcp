"""Metric fastener hole standards used to expand hole callouts.

Clearance: ISO 273 medium. Counterbore: for ISO 4762 / GB/T 70.1 socket head cap screws.
Tap drill: ISO coarse thread.
"""
from __future__ import annotations

from .errors import SPEC_INVALID, CadError

# size: (tap_drill, clearance_medium, cbore_dia, cbore_depth, csink_dia)
METRIC = {
    "M3": (2.5, 3.4, 6.5, 3.3, 6.3),
    "M4": (3.3, 4.5, 8.0, 4.4, 8.4),
    "M5": (4.2, 5.5, 9.5, 5.4, 10.4),
    "M6": (5.0, 6.6, 11.0, 6.5, 12.6),
    "M8": (6.8, 9.0, 14.0, 8.6, 17.3),
    "M10": (8.5, 11.0, 17.5, 10.8, 20.0),
    "M12": (10.2, 13.5, 20.0, 13.0, 24.0),
    "M16": (14.0, 17.5, 26.0, 17.5, 32.0),
    "M20": (17.5, 22.0, 33.0, 21.5, 40.0),
}


def _row(size: str):
    key = size.upper()
    if key not in METRIC:
        raise CadError(SPEC_INVALID, f"unknown metric size {size!r}", hint=f"supported: {sorted(METRIC)}")
    return METRIC[key]


def tap_drill(size: str) -> float:
    return _row(size)[0]


def clearance(size: str) -> float:
    return _row(size)[1]


def counterbore(size: str) -> tuple[float, float]:
    r = _row(size)
    return r[2], r[3]


def countersink(size: str) -> float:
    return _row(size)[4]


def is_metric(size: str) -> bool:
    return size.upper() in METRIC
