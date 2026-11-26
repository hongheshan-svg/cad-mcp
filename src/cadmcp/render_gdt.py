"""精度符号的 SVG 画法（GB/T 1182 几何公差框格、基准符号；GB/T 131 表面结构符号）。"""
from __future__ import annotations

import math

THIN = 0.25
CELL_H = 6.0


def _tri(s, pts):
    s.parts.append("<polygon points='" + " ".join(f"{x:.2f},{s.y(y):.2f}" for x, y in pts) + "' fill='#111'/>")


def datum(s, ax, ay, n, letter):
    """基准：涂黑三角形贴在边线上 + 引线 + 方框字母。"""
    nx, ny = n
    tx, ty = -ny, nx  # 沿边方向
    _tri(s, [(ax + tx * 1.8, ay + ty * 1.8), (ax - tx * 1.8, ay - ty * 1.8), (ax + nx * 2.6, ay + ny * 2.6)])
    ex, ey = ax + nx * 7, ay + ny * 7
    s.line(ax + nx * 2.6, ay + ny * 2.6, ex, ey, THIN)
    bx, by = ex + nx * CELL_H / 2, ey + ny * CELL_H / 2
    s.rect(bx - CELL_H / 2, by - CELL_H / 2, CELL_H, CELL_H, THIN)
    s.text(bx, by - 1.3, letter, 3.5, "middle")


def _widths(cells):
    return [7.0] + [max(7.0, len(c) * 2.2 + 3) if i == 0 else 6.0 for i, c in enumerate(cells[1:])]


def fcf_box(s, x0, y0, cells):
    """无引线框格（附在孔标注下方：顶边贴着标注横线）。"""
    x = x0
    for w, c in zip(_widths(cells), cells):
        s.rect(x, y0 - CELL_H, w, CELL_H, THIN)
        s.text(x + w / 2, y0 - CELL_H + 1.6, c, 3.5, "middle")
        x += w


def fcf(s, ax, ay, n, cells, reach: float = 10.0):
    """几何公差框格：引线箭头指向被测要素，框格放在外法向一侧（距离 reach）。"""
    nx, ny = n
    ex, ey = ax + nx * reach, ay + ny * reach
    s.line(ax, ay, ex, ey, THIN)
    a = math.atan2(-ny, -nx)
    _tri(s, [(ax, ay), (ax + 2.2 * math.cos(a + 2.85), ay + 2.2 * math.sin(a + 2.85)),
             (ax + 2.2 * math.cos(a - 2.85), ay + 2.2 * math.sin(a - 2.85))])
    widths = _widths(cells)
    total = sum(widths)
    if ny != 0:
        x0, y0 = ex - 3.5, ey + (0 if ny > 0 else -CELL_H)
    else:
        x0, y0 = (ex if nx > 0 else ex - total), ey - CELL_H / 2
    x = x0
    for w, c in zip(widths, cells):
        s.rect(x, y0, w, CELL_H, THIN)
        s.text(x + w / 2, y0 + 1.6, c, 3.5, "middle")
        x += w


def roughness(s, ax, ay, n, text):
    """表面结构符号：√ 尖端触及表面（或其延长线），上方横线写 Ra 值；非朝上的面用引线引出。"""
    nx, ny = n
    if (nx, ny) != (0, 1):
        ex, ey = ax + nx * 8, ay + ny * 8
        s.line(ax, ay, ex, ey, THIN)
        ax, ay = ex, ey + (0 if ny >= 0 else -9)
        if ny < 0:
            s.line(ex, ey, ex, ay, THIN)
    h = 4.2
    s.line(ax, ay, ax - h * 0.58, ay + h, THIN)
    s.line(ax - h * 0.58, ay + h, ax - h * 0.58 + 1.2, ay + h, THIN)
    s.line(ax, ay, ax + 2 * h * 0.58, ay + 2 * h, THIN)
    w = len(text) * 2.0 + 2
    s.line(ax + 2 * h * 0.58, ay + 2 * h, ax + 2 * h * 0.58 + w, ay + 2 * h, THIN)
    s.text(ax + 2 * h * 0.58 + 0.8, ay + 2 * h - 3.6, text, 3)


def general_roughness(s, x, y, ra_text):
    """"其余"表面结构（标题栏附近，GB/T 131）：其余 √Ra 3.2 (√)，x 为左端。"""
    s.text(x, y + 1, "其余", 3.5)
    roughness(s, x + 13, y, (0, 1), ra_text)
    s.text(x + 40, y + 1, "(√)", 3.5)


def draw(s, kind, px, py, extra, text):
    n = tuple(extra.get("normal", (0, 1))) if extra else (0, 1)
    if kind == "gtol" and extra and extra.get("attach") == "callout":
        fcf_box(s, px + 12, py + 9, extra["cells"])  # 孔标注横线起点 (px+12, py+9)
    elif kind == "datum":
        datum(s, px, py, n, text)
    elif kind == "gtol":
        fcf(s, px, py, n, extra["cells"])
    else:
        roughness(s, px, py, n, text)
