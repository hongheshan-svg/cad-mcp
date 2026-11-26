"""渐开线直齿圆柱齿轮（GB/T 1356 基本齿廓、GB/T 4459.2 画法、GB/T 10095 精度）：几何与检验参数。CAD 无关。

标准齿廓：α = 20°，齿顶高系数 ha* = 1，顶隙系数 c* = 0.25，齿根圆角半径系数 ρf* = 0.38。
外齿轮，变位系数 x（正变位为正）。齿顶高按 ha = (ha* + x)·m（不计齿顶高变动系数，单个齿轮出图足够）。
"""
from __future__ import annotations

import math

HA = 1.0
C = 0.25

# GB/T 1357 第一系列 + 第二系列模数（mm）
MODULES = (0.5, 0.6, 0.8, 1, 1.25, 1.5, 2, 2.5, 3, 4, 5, 6, 8, 10, 12, 16, 20, 25,
           0.7, 0.9, 1.125, 1.375, 1.75, 2.25, 2.75, 3.5, 4.5, 5.5, 7, 9, 11, 14, 18, 22)


def inv(a: float) -> float:
    return math.tan(a) - a


def _inv_inverse(v: float) -> float:
    a = (3 * v) ** (1 / 3)
    for _ in range(30):
        a -= (inv(a) - v) / (math.tan(a) ** 2)
    return a


def geometry(m: float, z: int, alpha_deg: float = 20.0, x: float = 0.0) -> dict:
    """分度圆 d、齿顶圆 da、齿根圆 df、基圆 db、分度圆齿厚 s、公法线跨齿数 k 与长度 Wk。"""
    if m <= 0 or z < 6:
        raise ValueError("齿轮：模数须 > 0，齿数 ≥ 6")
    a = math.radians(alpha_deg)
    d = m * z
    da = d + 2 * (HA + x) * m
    df = d - 2 * (HA + C - x) * m
    db = d * math.cos(a)
    s = m * (math.pi / 2 + 2 * x * math.tan(a))
    # 公法线（GB/T 6443 近似式）：k = z·αx/180° + 0.5 取整，αx = arccos(d·cosα / (d + 2xm))
    ax = math.acos(d * math.cos(a) / (d + 2 * x * m))
    k = max(2, int(round(z * math.degrees(ax) / 180 + 0.5)))
    Wk = m * math.cos(a) * ((k - 0.5) * math.pi + z * inv(a)) + 2 * x * m * math.sin(a)
    return {"m": m, "z": z, "alpha": alpha_deg, "x": x, "d": d, "da": da, "df": df, "db": db, "s": s,
            "k": k, "Wk": Wk}


def outline(m: float, z: int, alpha_deg: float = 20.0, x: float = 0.0, n_flank: int = 6) -> list[tuple[float, float]]:
    """整圈齿廓折线（逆时针，中心在原点，mm）：每个齿 = 齿根圆弧 + 渐开线左齿面 + 齿顶圆弧 + 右齿面。
    基圆以内的齿根段用径向直线连到齿根圆（足够建模与质量估算；不建齿根过渡圆角）。"""
    g = geometry(m, z, alpha_deg, x)
    a = math.radians(alpha_deg)
    r, ra, rf, rb = g["d"] / 2, g["da"] / 2, g["df"] / 2, g["db"] / 2
    # 齿厚半角（分度圆处）→ 渐开线起点相对齿中线的角度
    psi = g["s"] / (2 * r)
    th0 = psi + inv(a)  # 基圆上渐开线起点到齿中线的角度
    r_start = max(rb, rf)
    radii = [r_start + (ra - r_start) * i / n_flank for i in range(n_flank + 1)]

    def flank_angle(rr: float) -> float:  # 半径 rr 处齿面点到齿中线的角度
        if rr <= rb:
            return th0
        return th0 - inv(math.acos(rb / rr))

    pitch = 2 * math.pi / z
    pts: list[tuple[float, float]] = []
    for i in range(z):
        c = i * pitch
        right = [(rr, c - flank_angle(rr)) for rr in radii]          # 从根到顶
        left = [(rr, c + flank_angle(rr)) for rr in reversed(radii)]  # 从顶到根
        if rf < r_start - 1e-9:
            right.insert(0, (rf, c - th0))
            left.append((rf, c + th0))
        for rr, t in right + left:
            pts.append((rr * math.cos(t), rr * math.sin(t)))
        # 齿根圆弧：到下一个齿的右齿面起点（中间加一点）
        t_mid = c + pitch / 2
        pts.append((rf * math.cos(t_mid), rf * math.sin(t_mid)))
    return [(round(px, 4), round(py, 4)) for px, py in pts]


def polygon_area(pts) -> float:
    return abs(sum(x0 * y1 - x1 * y0 for (x0, y0), (x1, y1) in zip(pts, pts[1:] + pts[:1]))) / 2


def param_rows(g: dict, accuracy: str, mate: str | None = None) -> list[tuple[str, str, str]]:
    """齿轮参数表（GB/T 4459.2 图样右上角）：(名称, 代号, 数值)。"""
    rows = [("模数", "m", f"{g['m']:g}"), ("齿数", "z", f"{g['z']}"), ("压力角", "α", f"{g['alpha']:g}°"),
            ("齿顶高系数", "ha*", "1"), ("顶隙系数", "c*", "0.25"), ("变位系数", "x", f"{g['x']:g}"),
            ("精度等级", "", accuracy),
            ("公法线长度", "Wk", f"{g['Wk']:.3f}"), ("跨齿数", "k", f"{g['k']}")]
    if mate:
        rows.append(("配对齿轮", "", mate))
    return rows
