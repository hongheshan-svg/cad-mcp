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


# ---------- 轴类 ----------
# GB/T 1095-2003 普通平键键槽：轴径 (d_min, d_max] → (键宽 b, 键高 h, 轴槽深 t1)
KEYWAY = [
    (6, 8, 2, 2, 1.2), (8, 10, 3, 3, 1.8), (10, 12, 4, 4, 2.5), (12, 17, 5, 5, 3.0), (17, 22, 6, 6, 3.5),
    (22, 30, 8, 7, 4.0), (30, 38, 10, 8, 5.0), (38, 44, 12, 8, 5.0), (44, 50, 14, 9, 5.5), (50, 58, 16, 10, 6.0),
    (58, 65, 18, 11, 7.0), (65, 75, 20, 12, 7.5), (75, 85, 22, 14, 9.0), (85, 95, 25, 14, 9.0),
    (95, 110, 28, 16, 10.0),
]
# GB/T 1096 平键长度系列
KEY_LENGTHS = [6, 8, 10, 12, 14, 16, 18, 20, 22, 25, 28, 32, 36, 40, 45, 50, 56, 63, 70, 80, 90, 100, 110, 125,
               140, 160, 180, 200, 220, 250]
# GB/T 894 轴用弹性挡圈沟槽：轴径 d → (槽底径 d2, 槽宽 m)
CIRCLIP_GROOVE = {
    10: (9.6, 1.1), 12: (11.5, 1.1), 14: (13.4, 1.1), 15: (14.3, 1.1), 16: (15.2, 1.1), 17: (16.2, 1.1),
    18: (17.0, 1.3), 20: (19.0, 1.3), 22: (21.0, 1.3), 25: (23.9, 1.3), 28: (26.6, 1.6), 30: (28.6, 1.6),
    32: (30.3, 1.6), 35: (33.0, 1.6), 40: (37.5, 1.85), 45: (42.5, 1.85), 50: (47.0, 2.15), 55: (52.0, 2.15),
    60: (57.0, 2.15), 65: (62.0, 2.65), 70: (67.0, 2.65), 75: (72.0, 2.65), 80: (76.5, 2.65),
}


def keyway(d: float) -> tuple[float, float, float]:
    for lo, hi, b, h, t1 in KEYWAY:
        if lo < d <= hi:
            return float(b), float(h), t1
    raise CadError(SPEC_INVALID, f"no GB/T 1095 keyway for shaft Φ{d:g}", hint="轴径范围 6–110；或显式给出 width/depth")


def circlip_groove(d: float) -> tuple[float, float]:
    key = round(d)
    if abs(d - key) > 1e-6 or key not in CIRCLIP_GROOVE:
        raise CadError(SPEC_INVALID, f"no GB/T 894 circlip groove for shaft Φ{d:g}",
                       hint=f"可用轴径：{sorted(CIRCLIP_GROOVE)}")
    return CIRCLIP_GROOVE[key]


# GB/T 193 普通螺纹粗牙螺距；GB/T 196 基本小径 d1 = d − 1.0825·P
COARSE_PITCH = {3: 0.5, 4: 0.7, 5: 0.8, 6: 1.0, 8: 1.25, 10: 1.5, 12: 1.75, 14: 2.0, 16: 2.0, 18: 2.5, 20: 2.5,
                22: 2.5, 24: 3.0, 27: 3.0, 30: 3.5, 33: 3.5, 36: 4.0, 39: 4.0, 42: 4.5, 45: 4.5, 48: 5.0,
                52: 5.0, 56: 5.5, 60: 5.5, 64: 6.0}


def ext_thread(size: str) -> tuple[float, float, float, bool]:
    """M20 / M20x1.5 → (公称直径 d, 螺距 P, 小径 d1, 是否细牙)。"""
    import re
    m = re.fullmatch(r"\s*M\s*(\d+(?:\.\d+)?)\s*(?:[x×X*]\s*(\d+(?:\.\d+)?))?\s*", size)
    if not m:
        raise CadError(SPEC_INVALID, f"thread size {size!r}: expected like M20 or M20x1.5")
    d = float(m.group(1))
    if m.group(2):
        pitch, fine = float(m.group(2)), True
    else:
        if d not in COARSE_PITCH:
            raise CadError(SPEC_INVALID, f"thread {size}: no coarse pitch for M{d:g}", hint="写明螺距，如 M{d:g}x1.5")
        pitch, fine = COARSE_PITCH[d], False
    return d, pitch, round(d - 1.0825 * pitch, 3), fine
