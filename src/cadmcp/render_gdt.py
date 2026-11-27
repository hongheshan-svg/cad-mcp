"""精度符号的 SVG 画法（GB/T 1182 几何公差框格、基准符号；GB/T 131 表面结构符号）。"""
from __future__ import annotations

import math

THIN = 0.25
CELL_H = 6.0


def _tri(s, pts):
    s.parts.append("<polygon points='" + " ".join(f"{x:.2f},{s.y(y):.2f}" for x, y in pts) + "' fill='#111'/>")


def datum(s, ax, ay, n, letter, reach: float = 7.0):
    """基准：涂黑三角形贴在边线上 + 引线（长 reach）+ 方框字母。"""
    nx, ny = n
    tx, ty = -ny, nx  # 沿边方向
    _tri(s, [(ax + tx * 1.8, ay + ty * 1.8), (ax - tx * 1.8, ay - ty * 1.8), (ax + nx * 2.6, ay + ny * 2.6)])
    ex, ey = ax + nx * reach, ay + ny * reach
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
        _cell(s, x + w / 2, y0 - CELL_H, c)
        x += w


def fcf(s, ax, ay, n, cells, reach: float = 10.0, arrow: bool = True, dx: float = 0.0):
    """几何公差框格：引线箭头指向被测要素，框格放在外法向一侧（距离 reach）。arrow=False：折线引线的竖段（箭头另画）。
    dx：框格整体左移量（规划层 frame_dx，右端出图框时）。"""
    nx, ny = n
    ex, ey = ax + nx * reach, ay + ny * reach
    s.line(ax, ay, ex, ey, THIN)
    a = math.atan2(-ny, -nx)
    if arrow:
        _tri(s, [(ax, ay), (ax + 2.2 * math.cos(a + 2.85), ay + 2.2 * math.sin(a + 2.85)),
                 (ax + 2.2 * math.cos(a - 2.85), ay + 2.2 * math.sin(a - 2.85))])
    widths = _widths(cells)
    total = sum(widths)
    if ny != 0:
        x0, y0 = ex - 3.5 - dx, ey + (0 if ny > 0 else -CELL_H)
    else:
        x0, y0 = (ex if nx > 0 else ex - total), ey - CELL_H / 2
    x = x0
    for w, c in zip(widths, cells):
        s.rect(x, y0, w, CELL_H, THIN)
        _cell(s, x + w / 2, y0, c)
        x += w


def _cell(s, xc, y0, c) -> None:
    """框格内容；对称度符号常用字体里没有 ⌯，按 GB/T 1182 画三条横线（中间长）。"""
    if c == "⌯":
        ym = y0 + CELL_H / 2
        for dy, hw in ((-1.0, 1.0), (0.0, 1.8), (1.0, 1.0)):
            s.line(xc - hw, ym + dy, xc + hw, ym + dy, THIN)
        return
    s.text(xc, y0 + 1.6, c, 3.5, "middle")


def roughness(s, ax, ay, n, text, reach=None, rot: int = 0):
    """表面结构符号：√ 尖端触及表面（或其延长线），上方横线写 Ra 值；非朝上的面用引线引出。
    rot=90：整个符号逆时针转 90°（标在竖直尺寸线上，与竖写尺寸文字同向）。"""
    if rot == 90:
        h = 4.2
        R = lambda dx, dy: (ax - dy, ay + dx)  # noqa: E731  图纸坐标（y 向上）逆时针 90°
        w = len(text) * 2.0 + 2
        for a, b in (((0, 0), (-h * 0.58, h)), ((-h * 0.58, h), (-h * 0.58 + 1.2, h)), ((0, 0), (2 * h * 0.58, 2 * h)),
                     ((2 * h * 0.58, 2 * h), (2 * h * 0.58 + w, 2 * h))):
            s.line(*R(*a), *R(*b), THIN)
        tw = len(text) * 2.0  # 与横线上方文字同位置：起点 (2h·0.58 + 0.8, 2h − 3.6)，转后按中点放
        cx, cy = R(2 * h * 0.58 + 0.8 + tw / 2, 2 * h - 3.6)
        s.text(cx, cy, text, 3, "middle", -90)
        return
    nx, ny = n
    if (nx, ny) != (0, 1):
        from .detailing import ra_reach
        r = ra_reach(nx, text) if reach is None else reach
        ex, ey = ax + nx * r, ay + ny * r
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
    """"其余"表面结构（标题栏附近，GB/T 131）：其余 √Ra 3.2 (√)，x 为左端。
    ra_text="cast"：铸件其余表面不去除材料（√ 内加圆）。"""
    s.text(x, y + 1, "其余", 3.5)
    if ra_text == "cast":
        h = 4.2
        ax, ay = x + 13, y
        s.line(ax, ay, ax - h * 0.58, ay + h, THIN)
        s.line(ax, ay, ax + 2 * h * 0.58, ay + 2 * h, THIN)
        s.circle(ax, ay + h * 0.62, h * 0.36, THIN)
        s.text(x + 22, y + 1, "（铸造面）", 3)
        return
    roughness(s, x + 13, y, (0, 1), ra_text)
    s.text(x + 40, y + 1, "(√)", 3.5)


def draw(s, kind, px, py, extra, text):
    n = tuple(extra.get("normal", (0, 1))) if extra else (0, 1)
    if kind == "gtol" and extra and extra.get("attach") == "callout":
        lb = extra.get("callout_label")  # 孔标注的引线布置：框格顶边贴着标注横线，左端对齐文字
        if lb:
            fcf_box(s, min(lb["elbow"][0], lb["end"][0]), lb["elbow"][1], extra["cells"])
        else:
            fcf_box(s, px + 12, py + 9, extra["cells"])  # 无布置信息：孔标注横线起点 (px+12, py+9)
    elif kind == "datum":
        datum(s, px, py, n, text, (extra or {}).get("reach") or 7.0)
    elif kind == "gtol":
        fcf(s, px, py, n, extra["cells"])
    else:
        roughness(s, px, py, n, text, (extra or {}).get("reach"))
