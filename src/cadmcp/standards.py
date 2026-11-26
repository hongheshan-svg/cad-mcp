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


# GB/T 1095 轮毂槽深 t2（按键宽 b）
HUB_T2 = {2: 1.0, 3: 1.4, 4: 1.8, 5: 2.3, 6: 2.8, 8: 3.3, 10: 3.3, 12: 3.3, 14: 3.8, 16: 4.3, 18: 4.4, 20: 4.9,
          22: 5.4, 25: 5.4, 28: 6.4}


def hub_keyway(d: float, width: float | None = None) -> tuple[float, float]:
    """孔径 d 的轮毂键槽 (b, t2)：b 缺省按 GB/T 1095 查表。"""
    b = width if width is not None else keyway(d)[0]
    if b not in HUB_T2:
        raise CadError(SPEC_INVALID, f"键宽 {b:g} 不在 GB/T 1095 系列 {sorted(HUB_T2)}")
    return float(b), HUB_T2[b]


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


# ---------------- GB/T 1800.1/1800.2（ISO 286）极限与配合 ----------------
# 公称尺寸分段（mm，上限，含）：≤3、>3–6、…、>400–500
_SIZE_STEPS = (3, 6, 10, 18, 30, 50, 80, 120, 180, 250, 315, 400, 500)
# 标准公差 IT5–IT11（µm）
_IT = {
    5: (4, 5, 6, 8, 9, 11, 13, 15, 18, 20, 23, 25, 27),
    6: (6, 8, 9, 11, 13, 16, 19, 22, 25, 29, 32, 36, 40),
    7: (10, 12, 15, 18, 21, 25, 30, 35, 40, 46, 52, 57, 63),
    8: (14, 18, 22, 27, 33, 39, 46, 54, 63, 72, 81, 89, 97),
    9: (25, 30, 36, 43, 52, 62, 74, 87, 100, 115, 130, 140, 155),
    10: (40, 48, 58, 70, 84, 100, 120, 140, 160, 185, 210, 230, 250),
    11: (60, 75, 90, 110, 130, 160, 190, 220, 250, 290, 320, 360, 400),
}
# 轴的基本偏差（µm）：d–h 为上偏差 es，k–p 为下偏差 ei（k 取 IT4–IT7 的值）
_SHAFT_ES = {
    "d": (-20, -30, -40, -50, -65, -80, -100, -120, -145, -170, -190, -210, -230),
    "e": (-14, -20, -25, -32, -40, -50, -60, -72, -85, -100, -110, -125, -135),
    "f": (-6, -10, -13, -16, -20, -25, -30, -36, -43, -50, -56, -62, -68),
    "g": (-2, -4, -5, -6, -7, -9, -10, -12, -14, -15, -17, -18, -20),
    "h": (0,) * 13,
}
_SHAFT_EI = {
    "k": (0, 1, 1, 1, 2, 2, 2, 3, 3, 4, 4, 4, 5),
    "m": (2, 4, 6, 7, 8, 9, 11, 13, 15, 17, 20, 21, 23),
    "n": (4, 8, 10, 12, 15, 17, 20, 23, 27, 31, 34, 37, 40),
    "p": (6, 12, 15, 18, 22, 26, 32, 37, 43, 50, 56, 62, 68),
}


def _size_index(d: float) -> int:
    if d <= 0 or d > 500:
        raise ValueError(f"公称尺寸 {d:g} 超出 GB/T 1800 表格范围（0–500 mm）")
    return next(i for i, hi in enumerate(_SIZE_STEPS) if d <= hi + 1e-9)


def limit_deviations(d: float, code: str) -> tuple[float, float] | None:
    """公差带代号（如 H7、k6、JS7、g6）→ (上极限偏差, 下极限偏差)，单位 mm（GB/T 1800.2）。
    支持轴 d e f g h js k m n p、孔 D E F G H JS K M N P，标准公差等级 IT5–IT11；表外返回 None。"""
    import re as _re
    m = _re.fullmatch(r"([A-Za-z]{1,2})(\d{1,2})", code.strip())
    if not m:
        return None
    letter, grade = m.group(1), int(m.group(2))
    if grade not in _IT:
        return None
    try:
        i = _size_index(d)
    except ValueError:
        return None
    it = _IT[grade][i]
    lo = letter.lower()
    hole = letter[0].isupper()
    if lo == "js":
        half = (it - 1) / 2 if it % 2 and grade >= 7 else it / 2  # IT7–IT11 且 IT 为奇数时取 (IT−1)/2（GB/T 1800.1）
        es, ei = half, -half
    elif lo in _SHAFT_ES:
        es = _SHAFT_ES[lo][i]
        ei = es - it
        if hole:  # A–H：EI = −es
            es, ei = -ei, -es
    elif lo in _SHAFT_EI:
        ei = _SHAFT_EI[lo][i] if not (lo == "k" and (grade > 7 or grade < 4)) else 0
        es = ei + it
        if hole and ((lo in ("k", "m", "n") and grade > 8) or (lo == "p" and grade > 7)):
            # 高于 IT8 的 K、N：ES = 0；M：ES = −m；高于 IT7 的 P：ES = −p（无 Δ）
            up = 0 if lo in ("k", "n") else -_SHAFT_EI[lo][i]
            return up / 1000.0, (up - it) / 1000.0
        if hole:  # K/M/N ≤ IT8、P ≤ IT7：ES = −ei + Δ，Δ = ITn − IT(n−1)（≤3 mm 时 Δ = 0）
            delta = 0 if i == 0 or grade - 1 not in _IT else it - _IT[grade - 1][i]
            if lo == "k" and grade <= 3:
                delta = 0
            up = -_SHAFT_EI[lo][i] + delta
            if lo == "k" and i == 0:
                up = 0
            es, ei = up, up - it
    else:
        return None
    return es / 1000.0, ei / 1000.0


def fmt_deviation(v: float) -> str:
    """极限偏差文字：零写 0，其余带符号保留三位小数（GB/T 4458.5）。"""
    if abs(v) < 5e-7:
        return "0"
    t = f"{v:+.4f}"
    return t[:-1] if t.endswith("0") else t  # 一般三位小数；js5/js6 的半微米保留第四位


def tol_text(v: float, code: str) -> str:
    """无 Φ 的配合尺寸（键槽宽等）：6N9 → 6N9(0/-0.030)；表外只写代号。"""
    lim = limit_deviations(v, code)
    return f"{v:g}{code}" + (f"({fmt_deviation(lim[0])}/{fmt_deviation(lim[1])})" if lim else "")


def fit_text(d: float, code: str | None) -> str:
    """Φ20k6 → Φ20k6(+0.015/+0.002)；无公差带或表外时只写代号。"""
    if not code:
        return f"Φ{d:g}"
    lim = limit_deviations(d, code)
    if lim is None:
        return f"Φ{d:g}{code}"
    return f"Φ{d:g}{code}({fmt_deviation(lim[0])}/{fmt_deviation(lim[1])})"


# 中心孔（GB/T 145-2001，等同 ISO 866）：d → (A 型 D, A 型 l1, B 型 D2, B 型 l1)，mm
# A 型 D 与 B 型 D2 已对照 IS 2473:1975（等同 ISO 866）表 2.1/2.2 核实；l1 未核实（出图不用）
CENTER_HOLES = {1.0: (2.12, 0.97, 3.15, 1.27), 1.6: (3.35, 1.52, 5.0, 1.99), 2.0: (4.25, 1.95, 6.3, 2.54),
                2.5: (5.3, 2.42, 8.0, 3.2), 3.15: (6.7, 3.07, 10.0, 4.03), 4.0: (8.5, 3.9, 12.5, 5.05),
                6.3: (13.2, 5.98, 18.0, 7.36), 10.0: (21.2, 9.7, 28.0, 11.66)}
# 按轴端直径推荐中心孔 d（常用经验，选用参考）
_CH_BY_DIA = ((8, 1.0), (12, 1.6), (18, 2.0), (30, 2.5), (50, 3.15), (80, 4.0), (120, 6.3))


def center_hole(kind: str, d: float | None, end_dia: float) -> dict:
    """中心孔尺寸与标记：kind A | B；d=None 时按轴端直径推荐。返回 {type, d, D, l1, code}，code 如 B2.5/8。"""
    if d is None:
        d = next((v for lim, v in _CH_BY_DIA if end_dia <= lim), 10.0)
    if d not in CENTER_HOLES:
        raise ValueError(f"中心孔 d={d:g} 不在 GB/T 145 系列 {sorted(CENTER_HOLES)} 中")
    Da, la, Db, lb = CENTER_HOLES[d]
    D, l1 = (Da, la) if kind == "A" else (Db, lb)
    return {"type": kind, "d": d, "D": D, "l1": l1, "code": f"{kind}{d:g}/{D:g}"}
