"""把 DrawingPlan 渲染为 SVG 预览（Mock 适配器的"工程图"，也用于 AI 目视自检）。

不是生产图纸——生产图纸由 SolidWorks/Creo 渲染。这里保证：方案里的每个尺寸/标注
都画出来，并回报 dim_refs 供 check_drawing 校验。
"""
from __future__ import annotations

import math
from html import escape
from typing import Any

from . import detailing
from .detailing import VIEW_AXES, DrawingPlan, vc, vrange
from .ir.assembly_resolve import ResolvedAssembly
from .ir.resolve import ResolvedPart, face_point

STROKE = "#111"
THIN = 0.25
THICK = 0.5
FONT = "font-family='Noto Sans CJK SC, PingFang SC, sans-serif'"


class _Svg:
    def __init__(self, W: float, H: float):
        self.W, self.H = W, H
        self.parts: list[str] = []
        self.texts: list[tuple[str, tuple[float, float, float, float]]] = []
        self.leaders: list[tuple[float, float, float, float]] = []  # 细实线（引线/尺寸线/界线），查穿字
        self.thins: list[tuple[float, float, float, float]] = []  # 线宽 = THIN 的实线（尺寸线、尺寸界线、短引线）
        self.outlines: list[tuple[float, float, float, float]] = []  # 粗实线（可见轮廓），查压字
        self.text_bg = False  # True：文字下垫白底（剖面线遇尺寸数字断开，GB/T 4457.5）

    def collisions(self) -> list[tuple[str, str]]:
        """文字框两两重叠（重叠面积 > 较小框的 15%）。"""
        out = []
        tx = self.texts
        for i in range(len(tx)):
            for j in range(i + 1, len(tx)):
                (sa, a), (sb, b) = tx[i], tx[j]
                ox = min(a[2], b[2]) - max(a[0], b[0])
                oy = min(a[3], b[3]) - max(a[1], b[1])
                if ox > 0 and oy > 0:
                    small = min((a[2] - a[0]) * (a[3] - a[1]), (b[2] - b[0]) * (b[3] - b[1]))
                    if small > 0 and ox * oy > 0.15 * small:
                        out.append((sa, sb))
        return out

    def crossings(self, pad: float = 0.4) -> list[str]:
        """细实线穿过文字（文字框内缩 pad 后与线段相交）：引线/尺寸线压字，GB/T 4458.4 不允许。"""
        out = []
        for s_, (x0, y0, x1, y1) in self.texts:
            bx0, by0, bx1, by1 = x0 + pad, y0 + pad, x1 - pad, y1 - pad
            if bx1 <= bx0 or by1 <= by0:
                continue
            for (ax, ay, cx, cy) in self.leaders + self.outlines + self.thins:
                if _seg_hits_box(ax, ay, cx, cy, bx0, by0, bx1, by1):
                    out.append(s_)
                    break
        return out

    def y(self, y: float) -> float:
        return self.H - y

    def line(self, x1, y1, x2, y2, w=THIN, dash=None):
        d = f" stroke-dasharray='{dash}'" if dash else ""
        if w < THIN and not dash:
            self.leaders.append((x1, y1, x2, y2))
        elif w == THIN and not dash:  # 尺寸线、尺寸界线
            self.thins.append((x1, y1, x2, y2))
        elif w >= THICK and not dash:
            self.outlines.append((x1, y1, x2, y2))
        self.parts.append(f"<line x1='{x1:.2f}' y1='{self.y(y1):.2f}' x2='{x2:.2f}' y2='{self.y(y2):.2f}' "
                          f"stroke='{STROKE}' stroke-width='{w}'{d}/>")

    def poly(self, pts, w=THICK, close=True, dash=None):
        if w >= THICK and not dash:
            ring = list(pts) + ([pts[0]] if close else [])
            self.outlines += [(a[0], a[1], b[0], b[1]) for a, b in zip(ring, ring[1:])]
        d = " ".join(f"{x:.2f},{self.y(y):.2f}" for x, y in pts)
        tag = "polygon" if close else "polyline"
        da = f" stroke-dasharray='{dash}'" if dash else ""
        self.parts.append(f"<{tag} points='{d}' fill='none' stroke='{STROKE}' stroke-width='{w}'{da}/>")

    def face(self, pts, w=THICK):
        """白色填充的多边形（轴测图画家算法消隐用）：轮廓不进 outlines（被后画的面遮住的部分并不可见）。"""
        d = " ".join(f"{x:.2f},{self.y(y):.2f}" for x, y in pts)
        self.parts.append(f"<polygon points='{d}' fill='white' stroke='{STROKE}' stroke-width='{w}' stroke-linejoin='round'/>")

    def rect(self, x, y, w, h, sw=THICK):
        self.poly([(x, y), (x + w, y), (x + w, y + h), (x, y + h)], sw)

    def circle(self, cx, cy, r, w=THICK, dash=None):
        da = f" stroke-dasharray='{dash}'" if dash else ""
        self.parts.append(f"<circle cx='{cx:.2f}' cy='{self.y(cy):.2f}' r='{r:.2f}' fill='none' "
                          f"stroke='{STROKE}' stroke-width='{w}'{da}/>")

    def text(self, x, y, s, size=3.5, anchor="start", rotate=0):
        w = _text_w(s, size)
        x0 = x - (w / 2 if anchor == "middle" else 0)
        box = (x0, y - 0.2 * size, x0 + w, y + 0.8 * size) if not rotate else \
              (x - 0.8 * size, y - w / 2 if anchor == "middle" else y - w, x + 0.1 * size, y + w / 2 if anchor == "middle" else y)
        self.texts.append((s, box))
        if self.text_bg:
            self.parts.append(f"<rect x='{box[0]:.2f}' y='{self.y(box[3]):.2f}' width='{box[2] - box[0]:.2f}' "
                              f"height='{box[3] - box[1]:.2f}' fill='white' stroke='none'/>")
        rot = f" transform='rotate({rotate} {x:.2f} {self.y(y):.2f})'" if rotate else ""
        from .detailing import DEV_GAP, DEV_RATIO, _plain_w, split_deviation
        sp = split_deviation(s)
        if sp:  # 极限偏差：代号后上下叠写、小一号（GB/T 4458.5），整体按原锚点对齐
            main, up, lo, tail = sp
            ds = size * DEV_RATIO
            Y = self.y(y)
            wm = _plain_w(main, size)
            x0 = x - w if anchor == "end" else x0
            xd = x0 + wm + DEV_GAP * size  # 代号与偏差的间隙（随字高）
            wd = max(_plain_w(up, ds), _plain_w(lo, ds))
            t = (f"<g{rot}><text x='{x0:.2f}' y='{Y:.2f}' font-size='{size}' {FONT}>{escape(main)}</text>"
                 f"<text x='{xd:.2f}' y='{Y - 0.72 * ds - 0.3:.2f}' font-size='{ds:.2f}' {FONT}>{escape(up)}</text>"
                 f"<text x='{xd:.2f}' y='{Y:.2f}' font-size='{ds:.2f}' {FONT}>{escape(lo)}</text>")  # 下偏差与尺寸数字同基线
            if tail:
                t += f"<text x='{xd + wd:.2f}' y='{Y:.2f}' font-size='{size}' {FONT}>{escape(tail)}</text>"
            self.parts.append(t + "</g>")
            return
        self.parts.append(f"<text x='{x:.2f}' y='{self.y(y):.2f}' font-size='{size}' text-anchor='{anchor}' "
                          f"{FONT}{rot}>{escape(s)}</text>")

    def render(self) -> str:
        return (f"<svg xmlns='http://www.w3.org/2000/svg' width='{self.W}mm' height='{self.H}mm' "
                f"viewBox='0 0 {self.W} {self.H}'><rect width='100%' height='100%' fill='white'/>"
                + "".join(self.parts) + "</svg>")


def _seg_hits_box(x1, y1, x2, y2, bx0, by0, bx1, by1) -> bool:
    """线段与轴对齐矩形相交（Liang–Barsky 裁剪）。"""
    dx, dy = x2 - x1, y2 - y1
    t0, t1 = 0.0, 1.0
    for p_, q_ in ((-dx, x1 - bx0), (dx, bx1 - x1), (-dy, y1 - by0), (dy, by1 - y1)):
        if p_ == 0:
            if q_ < 0:
                return False
            continue
        r = q_ / p_
        if p_ < 0:
            t0 = max(t0, r)
        else:
            t1 = min(t1, r)
        if t0 > t1:
            return False
    return True


FACE_AXIS = {"top": "z", "bottom": "z", "front": "y", "back": "y", "left": "x", "right": "x"}


def _text_w(s: str, size: float) -> float:
    from .detailing import text_width
    return text_width(s, size)


HIDDEN = "3,1"  # 细虚线：不可见轮廓
CHAIN = "6,1.5,1,1.5"  # 细点画线：轴线、分度线


def _iso_xy(cx, cy, sc, box):
    sx, sy, sz = box.size

    def iso(x, y, z):
        x, y, z = x - box.xmin, y - box.ymin, z - box.zmin
        return (cx + (x - y) * 0.866 * sc - (sx - sy) * 0.433 * sc,
                cy + ((x + y) * 0.5 + z) * sc - ((sx + sy) * 0.25 + sz / 2) * sc)
    return iso


def _draw_flat(s, cx, cy, sc, part, plan, refs):
    """展开图：外轮廓、折弯线（细双点画线）、底板上的孔、展开尺寸与折弯说明。"""
    fu0, fu1, fv0, fv1 = part.flat_box

    def F(u, v):
        return cx + (u - (fu0 + fu1) / 2) * sc, cy + (v - (fv0 + fv1) / 2) * sc

    x0, y0 = F(fu0, fv0)
    x1, y1 = F(fu1, fv1)
    s.rect(x0, y0, x1 - x0, y1 - y0)
    for bd in part.bends:
        if bd["flat_axis"] == "v":
            s.line(*F(fu0, bd["flat_pos"]), *F(fu1, bd["flat_pos"]), THIN, "8,1.5,1,1.5,1,1.5")
        else:
            s.line(*F(bd["flat_pos"], fv0), *F(bd["flat_pos"], fv1), THIN, "8,1.5,1,1.5,1,1.5")
    for h in part.holes:
        for u, v in (h.flat_uv or []):
            s.circle(*F(u, v), h.drill / 2 * sc)
    for d in plan.dims:
        if d.view != "flat_pattern":
            continue
        if d.ref == "flat_w":
            yy = y0 - 10
            s.line(x0, yy, x1, yy)
            s.line(x0, y0 - 2, x0, yy - 1.5)
            s.line(x1, y0 - 2, x1, yy - 1.5)
            _arrow_pair(s, (x0, yy), (x1, yy))
            s.text((x0 + x1) / 2, yy + 1, d.text, 3.5, "middle")
        elif d.ref == "flat_h":
            xx = x1 + 10
            s.line(xx, y0, xx, y1)
            s.line(x1 + 2, y0, xx + 1.5, y0)
            s.line(x1 + 2, y1, xx + 1.5, y1)
            _arrow_pair(s, (xx, y0), (xx, y1))
            s.text(xx + 4.5, (y0 + y1) / 2, d.text, 3.5, "middle", -90)
        elif d.ref.startswith("flat_bend") and d.extra.get("sheet_pts"):
            (ax, ay), (bx, by) = d.extra["sheet_pts"]
            px, py = d.extra["sheet_pos"]
            tw_ = _text_w(d.text, 3.5)
            if d.extra["orient"] == "v":  # 尺寸界线自外轮廓左边引出
                s.line(px, ay, px, by)
                s.line(x0 - 2, ay, px - 1.5, ay)
                s.line(x0 - 2, by, px - 1.5, by)
                _arrow_pair(s, (px, ay), (px, by))
                if tw_ > abs(by - ay) - 1:  # 太短：数字移到外轮廓一端之外（该侧没有别的界线）
                    sg = -1 if by > ay else 1
                    s.line(px, ay, px, ay + sg * (1.5 + tw_))
                    s.text(px - 1, ay + sg * (1.5 + tw_ / 2), d.text, 3.5, "middle", -90)
                else:
                    s.text(px - 1, (ay + by) / 2, d.text, 3.5, "middle", -90)
            else:
                s.line(ax, py, bx, py)
                s.line(ax, y1 + 2, ax, py + 1.5)
                s.line(bx, y1 + 2, bx, py + 1.5)
                _arrow_pair(s, (ax, py), (bx, py))
                if tw_ > abs(bx - ax) - 1:
                    sg = -1 if bx > ax else 1
                    s.line(ax, py, ax + sg * (1.5 + tw_), py)
                    s.text(ax + sg * (1.5 + tw_ / 2), py + 1, d.text, 3.5, "middle")
                else:
                    s.text((ax + bx) / 2, py + 1, d.text, 3.5, "middle")
        elif d.kind == "callout" and d.at is not None and d.ref.startswith("bend"):
            _bend_note(s, d, F, x0, y0, x1, y1, part)
        elif d.kind == "callout" and d.at is not None:
            px, py = F(*d.at)
            s.text(px, py + 1, d.text, 3, "middle")
        refs.append(d.ref)


def _bend_note(s, d, F, x0, y0, x1, y1, part):
    """折弯说明：位置由规划层 detailing._bend_note 给出（与 CAD 一致）。"""
    ex = d.extra or {}
    if "text_at" not in ex:
        px, py = F(*d.at)
        s.text(px, py + 1, d.text, 3, "middle")
        return
    for seg in ex.get("leader", []):
        s.line(*seg[0], *seg[1], THIN)
    s.text(*ex["text_at"], d.text, 3, ex["anchor"], -90 if ex["rot"] == 90 else 0)


def _draw_sheet_side(s, part, vtype, ha, P):
    """钣金侧视图：与视线平行的折弯边显示为截面（板厚条），正对视线的翻边显示为面。"""
    t = part.thickness
    b = part.bbox
    hax = ha[-1]
    sign = -1 if ha[0] == "-" else 1
    lo, hi = (b.xmin, b.xmax) if hax == "x" else (b.ymin, b.ymax)
    # 底板截面
    a0, a1 = sorted((sign * lo, sign * hi))
    s.poly([P(a0, 0), P(a1, 0), P(a1, t), P(a0, t)])
    for bd in part.bends:
        L = bd["length"]
        z0, z1 = (0.0, L) if bd["direction"] == "up" else (-(L - t), t)
        edge_axis = "y" if bd["edge"] in ("front", "back") else "x"
        if edge_axis == hax:  # 截面：在视图横轴两端之一
            at_hi = bd["edge"] in ("back", "right")
            pos = sign * (hi if at_hi else lo)
            inner = pos - t if (pos == max(a0, a1)) else pos + t
            s.poly([P(min(pos, inner), z0), P(max(pos, inner), z0), P(max(pos, inner), z1), P(min(pos, inner), z1)])
        else:  # 翻边正对或背对视线：显示为面
            s.poly([P(a0, z0), P(a1, z0), P(a1, z1), P(a0, z1)], THIN)


def _sheet_flange_boxes(part):
    """钣金翻边实体包围盒（模型坐标）：沿整条底板边线，厚 t，Z 向按翻边方向。"""
    from .ir.resolve import Box
    t = part.thickness
    u0, u1, v0, v1 = part.profile.bbox2d
    out = []
    for bd in part.bends:
        L = bd["length"]
        z0, z1 = (0.0, L) if bd["direction"] == "up" else (-(L - t), t)
        e = bd["edge"]
        xs = (u0, u1) if e in ("front", "back") else ((u0, u0 + t) if e == "left" else (u1 - t, u1))
        ys = (v0, v1) if e in ("left", "right") else ((v0, v0 + t) if e == "front" else (v1 - t, v1))
        out.append((bd, Box(*xs, *ys, z0, z1)))
    return out


VIEW_LOOK = {"front": ("y", -1), "back": ("y", 1), "top": ("z", 1), "bottom": ("z", -1),
             "left": ("x", -1), "right": ("x", 1)}  # 视向：(深度轴, 观察者所在一侧)


def _occluder_rects(boxes, i, vtype, ha, va, P, solid=None):
    """整体位于 boxes[i] 之前（更靠近观察者）的其他包围盒在图上的投影矩形（消隐用）；solid[j] 为假者不作遮挡体。"""
    ax, sg = VIEW_LOOK.get(vtype, ("z", 1))
    bb = boxes[i]
    lo, hi = getattr(bb, ax + "min"), getattr(bb, ax + "max")
    out = []
    for j, ob in enumerate(boxes):
        if j == i or (solid is not None and not solid[j]):
            continue
        olo, ohi = getattr(ob, ax + "min"), getattr(ob, ax + "max")
        if (sg > 0 and olo >= hi - 1e-6) or (sg < 0 and ohi <= lo + 1e-6):
            (u0, u1), (w0, w1) = vrange(ob, ha), vrange(ob, va)
            (x0, y0), (x1, y1) = P(u0, w0), P(u1, w1)
            out.append((min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1)))
    return out


def _split_hidden(a, b, occ, eps=1e-6):
    """水平/竖直线段 a→b 按遮挡矩形切成 [(t0, t1, hidden)]（t ∈ [0, 1]）。线段落在矩形边界上不算被遮挡。"""
    (xa, ya), (xb, yb) = a, b
    horiz = abs(ya - yb) < eps
    c, s0, s1 = (ya, xa, xb) if horiz else (xa, ya, yb)
    L = s1 - s0
    if abs(L) < eps:
        return [(0.0, 1.0, False)]
    iv = []
    for x0, y0, x1, y1 in occ:
        clo, chi, plo, phi = (y0, y1, x0, x1) if horiz else (x0, x1, y0, y1)
        if clo + eps < c < chi - eps:
            t0, t1 = sorted(((plo - s0) / L, (phi - s0) / L))
            t0, t1 = max(t0, 0.0), min(t1, 1.0)
            if t1 > t0 + eps:
                iv.append((t0, t1))
    iv.sort()
    merged: list[list[float]] = []
    for t0, t1 in iv:
        if merged and t0 <= merged[-1][1] + eps:
            merged[-1][1] = max(merged[-1][1], t1)
        else:
            merged.append([t0, t1])
    out, t = [], 0.0
    for t0, t1 in merged:
        if t0 > t + eps:
            out.append((t, t0, False))
        out.append((t0, t1, True))
        t = t1
    if t < 1 - eps:
        out.append((t, 1.0, False))
    return out


def _rect_hidden(s, x0, y0, x1, y1, occ, w=THICK, show_hidden=True):
    """矩形按遮挡画：可见段实线，被遮挡段细虚线（show_hidden=False 时省略）。"""
    corners = [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]
    for i in range(4):
        a, b = corners[i], corners[(i + 1) % 4]
        for t0, t1, hid in _split_hidden(a, b, occ):
            pa = (a[0] + (b[0] - a[0]) * t0, a[1] + (b[1] - a[1]) * t0)
            pb = (a[0] + (b[0] - a[0]) * t1, a[1] + (b[1] - a[1]) * t1)
            if not hid:
                s.poly([pa, pb], w, close=False)
            elif show_hidden:
                s.line(*pa, *pb, THIN, dash=HIDDEN)


def _hatch_dir(s, cid, cell, angle, step):
    """矩形单元内的剖面线（45° 或 135°，间隔 step），clipPath 裁剪。"""
    x0, y0, x1, y1 = cell
    s.parts.append(f"<clipPath id='{cid}'><rect x='{x0:.2f}' y='{s.y(y1):.2f}' width='{x1 - x0:.2f}' "
                   f"height='{y1 - y0:.2f}'/></clipPath>")
    lines = []
    h = y1 - y0
    # 全局相位：45° 线满足 x − y = n·step，135° 线满足 x + y = n·step，相邻单元的剖面线首尾相接
    if angle == 45:
        n0, n1 = math.floor((x0 - y1) / step), math.ceil((x1 - y0) / step)
        for n in range(n0, n1 + 1):
            c = n * step  # 线：x = y + c，y ∈ [y0, y1]
            lines.append(f"<line x1='{y0 + c:.2f}' y1='{s.y(y0):.2f}' x2='{y1 + c:.2f}' y2='{s.y(y1):.2f}' "
                         f"stroke='#111' stroke-width='0.18'/>")
    else:
        n0, n1 = math.floor((x0 + y0) / step), math.ceil((x1 + y1) / step)
        for n in range(n0, n1 + 1):
            c = n * step  # 线：x = c − y
            lines.append(f"<line x1='{c - y0:.2f}' y1='{s.y(y0):.2f}' x2='{c - y1:.2f}' y2='{s.y(y1):.2f}' "
                         f"stroke='#111' stroke-width='0.18'/>")
    del h
    s.parts.append(f"<g clip-path='url(#{cid})'>{''.join(lines)}</g>")


def _encloser_rects(boxes, i, ax, ha, va, P, solid):
    """在视向上把 boxes[i] 整个包住的实体块（螺钉拧入板内：板在螺杆前后都有材料）的投影矩形。"""
    bb = boxes[i]
    lo, hi = getattr(bb, ax + "min"), getattr(bb, ax + "max")
    out = []
    for j, ob in enumerate(boxes):
        if j == i or not solid[j]:
            continue
        if getattr(ob, ax + "min") < lo - 1e-6 and getattr(ob, ax + "max") > hi + 1e-6:
            (u0, u1), (w0, w1) = vrange(ob, ha), vrange(ob, va)
            (x0, y0), (x1, y1) = P(u0, w0), P(u1, w1)
            out.append((min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1)))
    return out


def _seg_hidden(s, a, b, occ, w, dash_hidden=True):
    """水平/竖直线段按遮挡画：可见段线宽 w，被挡段细虚线（dash_hidden=False 时省略）。"""
    if occ is None:
        if w == THIN:
            s.line(*a, *b, THIN)
        else:
            s.poly([a, b], w, close=False)
        return
    for t0, t1, hid in _split_hidden(a, b, occ):
        pa = (a[0] + (b[0] - a[0]) * t0, a[1] + (b[1] - a[1]) * t0)
        pb = (a[0] + (b[0] - a[0]) * t1, a[1] + (b[1] - a[1]) * t1)
        if not hid:
            _seg_hidden(s, pa, pb, None, w)
        elif dash_hidden:
            s.line(*pa, *pb, THIN, dash=HIDDEN)


def _thread_lines(s, inst, vtype, ha, va, P, occ=None):
    """外螺纹（螺钉）在轴线平行于图面的视图中：小径（≈0.85d）细实线沿螺纹长度，螺纹未到头部时画终止线（粗）。"""
    from .ir.assembly_resolve import apply
    ax_d = VIEW_LOOK.get(vtype, ("z", 1))[0]
    di = "xyz".index(ax_d)
    o = apply(inst.transform, (0.0, 0.0, 0.0))
    zt = apply(inst.transform, (0.0, 0.0, 1.0))
    if abs(zt[di] - o[di]) > 1e-6:
        return  # 端视
    xt = apply(inst.transform, (1.0, 0.0, 0.0))
    side = (1.0, 0.0) if abs(xt[di] - o[di]) < 1e-6 else (0.0, 1.0)  # 零件坐标中平行于图面的横向
    zs = [z for sg in inst.source.revolve for z in sg[:2]]
    for z0, z1, d in inst.source.threads:
        r1 = 0.85 * d / 2
        for sg in (1, -1):
            a = apply(inst.transform, (side[0] * r1 * sg, side[1] * r1 * sg, z0))
            b = apply(inst.transform, (side[0] * r1 * sg, side[1] * r1 * sg, z1))
            _seg_hidden(s, P(vc(a, ha), vc(a, va)), P(vc(b, ha), vc(b, va)), occ, THIN, dash_hidden=False)
        for z in (z0, z1):
            if zs and min(zs) + 1e-6 < z < max(zs) - 1e-6 and not any(abs(z - sg[0]) < 1e-6 or abs(z - sg[1]) < 1e-6
                                                                        for sg in inst.source.revolve):
                a = apply(inst.transform, (side[0] * d / 2, side[1] * d / 2, z))
                b = apply(inst.transform, (-side[0] * d / 2, -side[1] * d / 2, z))
                _seg_hidden(s, P(vc(a, ha), vc(a, va)), P(vc(b, ha), vc(b, va)), occ, THICK)


def _draw_asm_section(s, asm, v, sec):
    """装配图全剖主视图：被剖零件画剖面线（相邻零件方向/间隔错开）与剖面轮廓；不剖件（轴、标准件）按段画外形；
    剖切面后方零件只画未被剖面挡住的可见轮廓（剖视图不画虚线）。"""
    from .detailing import asm_section_regions
    regs = asm_section_regions(asm, v, sec)
    occ = [c for r in regs if r["state"] in ("cut", "bearing") for c in r["cells"] + r.get("plain", [])] + \
        [q for r in regs if r["state"] == "excluded" for q in r["rects"]]
    for r in regs:
        if r["state"] == "behind":
            for x0, y0, x1, y1 in r["rects"]:
                _rect_hidden(s, x0, y0, x1, y1, occ, THICK, show_hidden=False)
    gear_occ = []
    for r in regs:
        if r["state"] == "cut":
            ang, step = r["hatch"]
            for n, c in enumerate(r["cells"]):
                _hatch_dir(s, f"as{r['id']}_{n}", c, ang, step)
            for a_, b_ in r["edges"]:  # 齿轮啮合区（GB/T 4459.2）：先画的齿轮轮齿可见，后画齿轮落在其轮齿区内的齿顶线画细虚线
                _seg_hidden(s, a_, b_, gear_occ if r.get("plain") else None, THICK)
            gear_occ += r.get("plain", [])
            for cone in r.get("cones", []):  # 螺纹底孔锥底：白底盖住剖面线，两斜边粗实线
                s.face(cone, 0)
                s.poly([cone[0], cone[2], cone[1]], THICK, close=False)
            for a_, b_ in r.get("chain", []):  # 齿轮分度线
                s.line(*a_, *b_, THIN, dash=CHAIN)
            for a_, b_ in r.get("thin", []):  # 内螺纹大径
                s.line(*a_, *b_, THIN)
            for a_, b_ in r.get("thick", []):  # 螺纹终止线
                s.poly([a_, b_], THICK, close=False)
    for r in regs:
        if r["state"] == "bearing":
            for x0, y0, x1, y1 in r["cells"]:
                s.face([(x0, y0), (x1, y0), (x1, y1), (x0, y1)])
            for a_, b_ in r["crosses"]:
                s.poly([a_, b_], THICK, close=False)
    ha, va = VIEW_AXES[v.type]
    cx, cy = v.origin_on_sheet
    (ulo, uhi), (wlo, whi) = vrange(asm.bbox, ha), vrange(asm.bbox, va)

    def P(u, w_):
        return cx + (u - (ulo + uhi) / 2) * v.scale, cy + (w_ - (wlo + whi) / 2) * v.scale
    for r in regs:
        if r["state"] == "excluded":  # 不剖件：白底盖住所在零件的剖面线（螺钉拧入螺孔处按螺钉画，GB/T 4459.1）
            for x0, y0, x1, y1 in r["rects"]:
                s.face([(x0, y0), (x1, y0), (x1, y1), (x0, y1)])
            inst = next(i for i in asm.instances if i.id == r["id"])
            for a_, b_ in r.get("chain", []):
                s.line(*a_, *b_, THIN, dash=CHAIN)
            if inst.source.threads:
                _thread_lines(s, inst, v.type, ha, va, P)
    if sec.get("label"):
        cx, cy = v.origin_on_sheet
        s.text(cx, cy + v.size_mm[1] / 2 + 4, f"{sec['label']}-{sec['label']}", 5, "middle")


def _draw_asm_view(s, asm, vtype, ha, va, P, sc):
    """装配视图：零件按实体块消隐（被前方零件挡住的轮廓画细虚线）。回转件（轴、轴承）按段画阶梯轮廓；
    轴线朝向观察者时画同心圆（某段外圆被更靠近观察者的更大段挡住则不画）。标准件（螺钉等）不作遮挡体。"""
    from .ir.assembly_resolve import apply, solid_boxes
    blocks = []  # (实例序号, 世界包围盒, 段参数)
    for k, inst in enumerate(asm.instances):
        blocks += [(k, bb, seg) for bb, seg in solid_boxes(inst)]
    boxes = [b for _, b, _ in blocks]
    solid = [asm.instances[k].source.kind != "standard" for k, _, _ in blocks]
    ax_d, sg_d = VIEW_LOOK.get(vtype, ("z", 1))
    for bi, (k, bb, seg) in enumerate(blocks):
        inst = asm.instances[k]
        if seg is not None:
            o = apply(inst.transform, (0.0, 0.0, 0.0))
            zt = apply(inst.transform, (0.0, 0.0, 1.0))
            axis = [zt[i] - o[i] for i in range(3)]
            if abs(axis["xyz".index(ax_d)]) > 0.999:  # 端视：画圆
                z0, z1, do, di = seg
                near = z1 if axis["xyz".index(ax_d)] * sg_d > 0 else z0
                c = apply(inst.transform, (0.0, 0.0, near))
                dep = c["xyz".index(ax_d)] * sg_d  # 越大越靠近观察者
                cx_, cy_ = P(vc(c, ha), vc(c, va))

                def covered(r_):  # 更靠近观察者的块完全盖住该圆
                    for bj, (k2, bb2, sg2) in enumerate(blocks):
                        if bj == bi or not solid[bj] and k2 != k:
                            continue
                        lo2 = min(getattr(bb2, ax_d + "min") * sg_d, getattr(bb2, ax_d + "max") * sg_d)
                        if lo2 < dep - 1e-6:
                            continue
                        (u0_, u1_), (w0_, w1_) = vrange(bb2, ha), vrange(bb2, va)
                        (X0, Y0), (X1, Y1) = P(u0_, w0_), P(u1_, w1_)
                        X0, X1, Y0, Y1 = min(X0, X1), max(X0, X1), min(Y0, Y1), max(Y0, Y1)
                        if sg2 is not None:  # 回转段：按圆判断（同轴且外径不小于）
                            ccx, ccy = (X0 + X1) / 2, (Y0 + Y1) / 2
                            if math.hypot(ccx - cx_, ccy - cy_) < 1e-3 and sg2[2] / 2 * sc >= r_ - 1e-6:
                                return True
                        elif X0 <= cx_ - r_ and X1 >= cx_ + r_ and Y0 <= cy_ - r_ and Y1 >= cy_ + r_:
                            return True
                    return False
                for r_ in (do / 2 * sc, di / 2 * sc):
                    if r_ > 0 and not covered(r_):
                        s.circle(cx_, cy_, r_, THICK)
                continue
        (bu0, bu1), (bw0, bw1) = vrange(bb, ha), vrange(bb, va)
        x0, y0 = P(bu0, bw0)
        x1, y1 = P(bu1, bw1)
        own = [j for j, (k2, _, _) in enumerate(blocks) if k2 == k]
        occ = _occluder_rects(boxes, bi, vtype, ha, va, P, [sv and j not in own for j, sv in enumerate(solid)])
        if not solid[bi]:
            occ += _encloser_rects(boxes, bi, ax_d, ha, va, P, solid)
        _rect_hidden(s, min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1), occ, THICK)  # 可见轮廓一律粗实线（GB/T 4457.4）
    for k, inst in enumerate(asm.instances):
        if inst.source.threads:
            bi = next(j for j, (k2, _, _) in enumerate(blocks) if k2 == k)
            occ = _occluder_rects(boxes, bi, vtype, ha, va, P, solid) + _encloser_rects(boxes, bi, ax_d, ha, va, P, solid)
            _thread_lines(s, inst, vtype, ha, va, P, occ)


def _draw_weld_bodies(s, part, vtype, ha, va, P):
    """焊接件视图：每个杆件/板件按包围盒投影并消隐（被前方杆件/板件挡住的轮廓画细虚线，GB/T 4457.4）；
    端视的管材画内壁（细实线，被挡时省略），圆管画圆。"""
    from .ir.resolve import Box
    depth_axis = next(a for a in "xyz" if a not in (ha[-1], va[-1]))
    bodies = sorted(part.weld_bodies, key=lambda b: b["kind"] != "plate")
    boxes = [Box(*b["box"]) for b in bodies]
    for bi, body in enumerate(bodies):
        bb = boxes[bi]
        (u0, u1), (w0, w1) = vrange(bb, ha), vrange(bb, va)
        x0, y0 = P(u0, w0)
        x1, y1 = P(u1, w1)
        occ = _occluder_rects(boxes, bi, vtype, ha, va, P)
        sec = body.get("section")
        if body["kind"] == "member" and body["axis"] == depth_axis and sec and sec["kind"] == "CHS":
            r = (x1 - x0) / 2
            cx_, cy_ = (x0 + x1) / 2, (y0 + y1) / 2
            hid = any(ox0 <= cx_ - r and cx_ + r <= ox1 and oy0 <= min(cy_ - r, cy_ + r) and max(cy_ - r, cy_ + r) <= oy1
                      for ox0, oy0, ox1, oy1 in occ)
            s.circle(cx_, cy_, r, THIN if hid else THICK, dash=HIDDEN if hid else None)
            if not hid:
                s.circle(cx_, cy_, r * (1 - 2 * sec["t"] / sec["h"]), THIN)
            continue
        X0, X1, Y0, Y1 = min(x0, x1), max(x0, x1), min(y0, y1), max(y0, y1)
        _rect_hidden(s, X0, Y0, X1, Y1, occ)
        if body["kind"] == "member" and body["axis"] == depth_axis and sec and sec["hollow"]:
            k = (X1 - X0) * sec["t"] / (bb.xmax - bb.xmin if ha[-1] == "x" else (bb.ymax - bb.ymin if ha[-1] == "y" else bb.zmax - bb.zmin))
            _rect_hidden(s, X0 + k, Y0 + k, X1 - k, Y1 - k, occ, THIN, show_hidden=False)


def _weld_symbol(s, px, py, d):
    """GB/T 324 角焊缝符号：箭头引线 → 基准线（实线 + 虚线识别线），三角形在基准线上（箭头侧），
    双面焊上下各一个三角形（不画虚线）；左侧焊脚 K，右侧处数；周围焊在转折处画小圆；尾部焊接方法代号。"""
    from .render_gdt import _tri
    ex_ = d.extra or {}
    if "sheet_at" in ex_:
        (px, py), (ex, ey) = ex_["sheet_at"], ex_["sheet_pos"]
    else:
        ex, ey = px + 10, py + 12
    s.line(px, py, ex, ey, THIN)
    a = math.atan2(py - ey, px - ex)
    _tri(s, [(px, py), (px - 2.2 * math.cos(a - 0.3), py - 2.2 * math.sin(a - 0.3)),
             (px - 2.2 * math.cos(a + 0.3), py - 2.2 * math.sin(a + 0.3))])
    L = 26
    s.line(ex, ey, ex + L, ey, THIN)
    both = ex_.get("both_sides")
    if not both:
        s.line(ex, ey - 1.2, ex + L, ey - 1.2, THIN, "1.5,1")
    tx = ex + 9
    s.poly([(tx, ey), (tx + 3.5, ey), (tx, ey + 3.5)], THIN)  # 角焊缝 ◣（箭头侧）
    if both:
        s.poly([(tx, ey), (tx + 3.5, ey), (tx, ey - 3.5)], THIN)  # 另一侧
        s.text(ex + 1, ey - 3.6, d.text, 3)
    s.text(ex + 1, ey + 0.8, d.text, 3)
    if ex_.get("process"):  # 尾部：焊接方法代号
        s.line(ex + L, ey, ex + L + 2.5, ey + 2.5, THIN)
        s.line(ex + L, ey, ex + L + 2.5, ey - 2.5, THIN)
        s.text(ex + L + 3.2, ey - 1, ex_["process"], 3)
    if d.extra and d.extra.get("count", 1) > 1:
        s.text(tx + 5, ey + 0.8, f"{d.extra['count']}处", 3)
    if d.extra and d.extra.get("all_around"):
        s.circle(ex, ey, 1.4, THIN)


def _sub_segments(a, b, cuts):
    """线段 a-b 去掉与之共线的若干开口段，返回剩余小段。"""
    L = math.dist(a, b)
    if L < 1e-9:
        return []
    ux, uy = (b[0] - a[0]) / L, (b[1] - a[1]) / L
    holes = []
    for c, d in cuts:
        off = [abs((q[0] - a[0]) * uy - (q[1] - a[1]) * ux) for q in (c, d)]
        if max(off) > 0.05:
            continue
        t0, t1 = sorted(((q[0] - a[0]) * ux + (q[1] - a[1]) * uy) for q in (c, d))
        if t1 > 0 and t0 < L:
            holes.append((max(t0, 0), min(t1, L)))
    holes.sort()
    out, t = [], 0.0
    for t0, t1 in holes:
        if t0 > t + 1e-6:
            out.append((t, t0))
        t = max(t, t1)
    if t < L - 1e-6:
        out.append((t, L))
    return [((a[0] + ux * s0, a[1] + uy * s0), (a[0] + ux * s1, a[1] + uy * s1)) for s0, s1 in out]


def _draw_section(s, sec, V, v, cx, cy, g=None):
    """剖视图：剖面线（45° 细实线，间距 3，GB/T 4457.5 金属材料）只画在实体剖面区，孔/型腔为空；
    孔口处画后方可见棱线（轮廓连续）；阶梯剖转折处不画线；半剖以中心线为界。"""
    g = g or sec.geometry
    solid = [[V(q) for q in poly] for poly in g["solid"]]
    voids = [[V(q) for q in poly] for poly in g["voids"]]
    mid = f"sec{sec.label}{id(sec) % 10000}"
    allpts = [q for poly in solid for q in poly]
    if allpts:
        x0, x1 = min(q[0] for q in allpts), max(q[0] for q in allpts)
        y0, y1 = min(q[1] for q in allpts), max(q[1] for q in allpts)
        path = lambda polys: " ".join("M " + " L ".join(f"{q[0]:.2f},{s.y(q[1]):.2f}" for q in pl) + " Z" for pl in polys)  # noqa: E731
        s.parts.append(
            f"<defs><pattern id='h{mid}' patternUnits='userSpaceOnUse' width='3' height='3' patternTransform='rotate(45)'>"
            f"<line x1='0' y1='0' x2='0' y2='3' stroke='{STROKE}' stroke-width='0.18'/></pattern>"
            f"<mask id='m{mid}'><path d='{path(solid)}' fill='white'/>"
            + "".join(f"<path d='{path([v_])}' fill='black'/>" for v_ in voids) + "</mask></defs>"  # 逐个画：重叠空腔不因绕向相反而抵消
            f"<rect x='{x0:.2f}' y='{s.y(y1):.2f}' width='{x1 - x0:.2f}' height='{y1 - y0:.2f}' "
            f"fill='url(#h{mid})' mask='url(#m{mid})'/>")
    # 孔口/型腔口（g["openings"]）照画：剖切面后方的半个孔口/型腔后壁棱线可见，轮廓线连续（GB/T 4458.6）；
    # 只去掉局部剖波浪线上的边（g["erase"]）与阶梯剖各段的共享边
    openings = [(V(a), V(b)) for a, b in g.get("erase", [])]
    # 合并相邻实体（阶梯剖各段、相同高度）：共享的竖边不画
    shared = []
    for i, a in enumerate(solid):
        for b in solid[i + 1:]:
            for k in range(len(a)):
                p1, p2 = a[k], a[(k + 1) % len(a)]
                for m in range(len(b)):
                    q1, q2 = b[m], b[(m + 1) % len(b)]
                    if (math.dist(p1, q2) < 0.05 and math.dist(p2, q1) < 0.05) or \
                       (math.dist(p1, q1) < 0.05 and math.dist(p2, q2) < 0.05):
                        shared.append((p1, p2))
    half_x = V((sec.half_from, 0, 0) if sec.view == "front" else (0, sec.half_from, 0))[0] if sec.half_from is not None else None
    for poly in solid:
        for k in range(len(poly)):
            a, b = poly[k], poly[(k + 1) % len(poly)]
            if any(math.dist(a, c) < 0.05 and math.dist(b, d) < 0.05 or math.dist(a, d) < 0.05 and math.dist(b, c) < 0.05
                   for c, d in shared):
                continue
            if half_x is not None and abs(a[0] - half_x) < 0.05 and abs(b[0] - half_x) < 0.05:
                continue
            for c, d in _sub_segments(a, b, openings + shared):
                s.line(*c, *d, THICK)
    for pl in g["edges"]:
        s.poly([V(q) for q in pl], THICK, close=False)
    for seg in g["thin"]:
        s.line(*V(seg[0]), *V(seg[1]), THIN)
    for seg in g["thick"]:
        s.line(*V(seg[0]), *V(seg[1]), THICK)
    for seg in g["axes"]:
        s.line(*V(seg[0]), *V(seg[1]), 0.18, "4,1,1,1")
    for pl in g["outline"]:  # 半剖：未剖的一半画外形（开口：中心线一侧不画）
        s.poly([V(q) for q in pl], THICK, close=False)
    if half_x is not None and allpts:
        s.line(half_x, y0 - 3, half_x, y1 + 3, 0.18, "4,1,1,1")
    if v is not None:
        s.text(cx, cy + v.size_mm[1] / 2 + 5, f"{sec.label}-{sec.label}", 5, "middle")


def _hole_cut_by(sec, h, inst) -> bool:
    """孔轴垂直剖切面的孔实例是否被剖切面切到（含钻尖）。"""
    from .section import AX, FRAME, hole_profile
    hi_, di_ = AX[FRAME[sec.view][0]], AX[FRAME[sec.view][1]]
    axis = [i for i in range(3) if inst.normal[i] != 0][0]
    if axis != di_:
        return False
    inward = -inst.normal[axis]
    total = hole_profile(h)[-1][0]
    x = inst.xyz
    return any(h0 - 1e-6 <= x[hi_] <= h1 + 1e-6 and 0 <= (d - x[di_]) * inward <= total for h0, h1, d in sec.path)


def _draw_local(s, sec, V):
    """局部剖：擦除原视图在波浪线区域内的线（含虚线），再画剖面；波浪线为细实线，不画成轮廓。"""
    g = sec.geometry
    for poly in g["region"]:
        pts = [V(q) for q in poly]
        s.parts.append("<path d='M " + " L ".join(f"{x:.2f},{s.y(y):.2f}" for x, y in pts) + " Z' fill='white' stroke='none'/>")
    from .section import AX, FRAME
    hi_ = AX[FRAME[sec.view][0]]
    h0, h1, _ = sec.path[0]
    amp = sec.wave_amp
    on_break = lambda q: abs(q[hi_] - h0) <= amp + 1e-6 or abs(q[hi_] - h1) <= amp + 1e-6  # noqa: E731
    g2 = dict(g)
    # 轮廓边中落在波浪线上的（两端点都在波浪线带内且非水平）不画粗线
    g2["erase"] = [
        (poly[k], poly[(k + 1) % len(poly)]) for poly in g["solid"] for k in range(len(poly))
        if on_break(poly[k]) and on_break(poly[(k + 1) % len(poly)]) and abs(poly[k][2] - poly[(k + 1) % len(poly)][2]) > 1e-9]
    _draw_section(s, sec, V, None, 0, 0, g2)
    for w in g["breaks"]:
        s.poly([V(q) for q in w], THIN, close=False)


def _box_overlap(a, b) -> bool:
    return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]


def _dim_boxes(plan, vtype) -> list[tuple[float, float, float, float]]:
    """规划层已定位尺寸（总体/节距/安装）的文字框与尺寸线（图纸坐标），供后画的符号避让。"""
    out = []
    for d in plan.dims:
        ex = d.extra or {}
        if d.view != vtype or not ex.get("sheet_pos") or not ex.get("sheet_pts"):
            continue
        (x1, y1), (x2, y2) = ex["sheet_pts"][0], ex["sheet_pts"][-1]
        px, py = ex["sheet_pos"]
        if d.kind == "ordinate":  # 坐标值：引线 + 数字（字高 2.5）
            tw = _text_w(d.text, 2.5)
            fx, fy = ex["sheet_pts"][-1]
            if ex.get("orient") == "h":  # 靠近剖切符号时数字再外移 8 mm（见坐标尺寸绘制），按外移后的范围占位
                out.append((min(fx, px) - 1.5, py - 8 - tw - 0.5, max(fx, px) + 1.5, fy))
            else:
                out.append((fx, min(fy, py) - 1.5, px + 8 + tw + 0.5, max(fy, py) + 1.5))
            continue
        tw = _text_w(d.text, 3.5)
        if ex.get("orient") == "h":
            out += [(px - tw / 2 - 1, py - 1, px + tw / 2 + 1, py + 4.5), (min(x1, x2), py - 0.5, max(x1, x2), py + 0.5)]
        else:
            out += [(px - 4.5, py - tw / 2 - 1, px + 1, py + tw / 2 + 1), (px - 0.5, min(y1, y2), px + 0.5, max(y1, y2))]
    return out


def _draw_cut_line(s, sec, part, V, avoid=None):
    """剖切符号（GB/T 4458.6）：起止和转折处画粗短画，起止处画箭头表示投射方向，并注字母。"""
    from .section import AX, FRAME
    pts = [V(q) for q in sec.cut_points]
    da = FRAME[sec.view][1]
    q0 = sec.cut_points[0]
    q1 = list(q0)
    q1[AX[da]] += 1.0
    d0, d1 = V(q0), V(tuple(q1))
    n = math.dist(d0, d1) or 1
    dx, dy = (d1[0] - d0[0]) / n, (d1[1] - d0[1]) / n  # 投射方向（图纸上）
    W = 0.7

    def stroke(a, b, length=5):
        L = math.dist(a, b) or 1
        return (a, (a[0] + (b[0] - a[0]) / L * length, a[1] + (b[1] - a[1]) / L * length))

    ends = [stroke(pts[0], pts[1]), stroke(pts[-1], pts[-2])]
    for a, b in ends:
        s.line(*a, *b, W)
        s.line(*a, a[0] + dx * 6, a[1] + dy * 6, THIN)
        s.parts.append(_arrow_head(s, (a[0] + dx * 6, a[1] + dy * 6), (dx, dy)))
        # 字母在箭头外端（剖切线已伸出轮廓 6 mm，字母不压视图轮廓）；与尺寸文字/尺寸线冲突时改放箭头旁
        ux_, uy_ = b[0] - a[0], b[1] - a[1]
        L_ = math.hypot(ux_, uy_) or 1.0
        ux_, uy_ = -ux_ / L_, -uy_ / L_  # 粗短画朝外的方向
        cands = [(a[0] + dx * t_ + ux_ * u_, a[1] + dy * t_ + uy_ * u_ - 1.5)
                 for t_, u_ in ((9, 0), (4, 4), (9, 4), (4, -4), (9, -4), (13, 0), (13, 4), (13, -4))]

        def bad(c):
            bx = (c[0] - 1.6, c[1] - 1.0, c[0] + 1.6, c[1] + 4.0)
            return sum(_box_overlap(bx, o) for o in (avoid or []) + [t[1] for t in s.texts]) + \
                sum(_seg_hits_box(*ln, *bx) for ln in s.leaders + s.thins)
        tx_, ty_ = min(cands, key=lambda c: (bad(c), cands.index(c)))
        s.text(tx_, ty_, sec.label, 5, "middle")
    for i in range(1, len(pts) - 1, 2):  # 阶梯转折：每个转角画 L 形粗短画（两臂各 4），转角外侧注字母
        a, b = pts[i], pts[i + 1]
        for corner, along, other in ((a, pts[i - 1], b), (b, pts[i + 2], a)):
            s.line(*stroke(corner, along, 4)[1], *corner, W)
            s.line(*corner, *stroke(corner, other, 4)[1], W)
        # 转角外侧注字母：在 4 个斜角候选中取离孔轮廓、剖切线最远的（字母不压孔）
        circles = [(V(i.xyz), h.max_dia / 2 * n) for h in (part.holes if part is not None else []) for i in h.instances]

        def clearance(c):
            dh = min((math.dist(c, o) - r for o, r in circles), default=99.0)
            dl = min(_seg_dist(c, pts[j], pts[j + 1]) for j in range(len(pts) - 1))
            return min(dh - 2.0, dl - 1.5)

        def hits(c):  # 字母框（与 Svg.text 同算法）被已画的线穿过的条数
            w_ = _text_w(sec.label, 3.5)
            bx0, by0, bx1, by1 = c[0] - w_ / 2 + 0.4, c[1] - 1.2 - 0.7 + 0.4, c[0] + w_ / 2 - 0.4, c[1] - 1.2 + 2.8 - 0.4
            return sum(_seg_hits_box(*ln, bx0, by0, bx1, by1) for ln in s.leaders + s.outlines + s.thins)

        cands = [(a[0] + sx * r_, a[1] + sy * r_) for r_ in (4.5, 6.5) for sx, sy in ((-1, -1), (1, -1), (-1, 1), (1, 1))]
        tx, ty = max(cands, key=lambda c: (-hits(c), clearance(c) - (c != cands[0] and math.dist(c, a) > 5) * 0.5))
        s.text(tx, ty - 1.2, sec.label, 3.5, "middle")


def _seg_dist(p, a, b) -> float:
    ax, ay = b[0] - a[0], b[1] - a[1]
    L = ax * ax + ay * ay
    t = 0.0 if L == 0 else max(0.0, min(1.0, ((p[0] - a[0]) * ax + (p[1] - a[1]) * ay) / L))
    return math.dist(p, (a[0] + ax * t, a[1] + ay * t))


def _arrow_head(s, tip, d):
    x, y = tip
    bx, by = x - d[0] * 2.5, y - d[1] * 2.5
    px, py = -d[1] * 0.8, d[0] * 0.8
    pts = [(x, y), (bx + px, by + py), (bx - px, by - py)]
    return f"<polygon points='{' '.join(f'{a:.2f},{s.y(b):.2f}' for a, b in pts)}' fill='{STROKE}'/>"


def _arrow_pair(s, a, b):
    """尺寸线两端箭头（指向尺寸界线）。"""
    L = math.dist(a, b) or 1.0
    d = ((b[0] - a[0]) / L, (b[1] - a[1]) / L)
    s.parts.append(_arrow_head(s, a, (-d[0], -d[1])))
    s.parts.append(_arrow_head(s, b, d))


def _draw_hole_table(s, x0, y_bottom, rows, out_rows) -> float:
    """孔表（技术要求上方）：表头在上，孔号 / X / Y / 规格。返回占用高度。"""
    cols = [("孔号", 14), ("X", 22), ("Y", 22), ("规格", 122)]
    rh = 5
    top = y_bottom + rh * (len(rows) + 1)
    y = top - rh
    xx = x0
    for name, w in cols:
        s.rect(xx, y, w, rh, THIN)
        s.text(xx + 1.5, y + 1.4, name, 2.8)
        xx += w
    for r in rows:
        y -= rh
        xx = x0
        for (_, w), val in zip(cols, [r["tag"], f"{r['x']:g}", f"{r['y']:g}", r["size"]]):
            s.rect(xx, y, w, rh, THIN)
            s.text(xx + 1.5, y + 1.4, str(val), 2.5)
            xx += w
        out_rows.append([r["tag"], r["x"], r["y"], r["size"]])
    s.text(x0, top + 2, "孔表", 3.5)
    return top - y_bottom + 8


def _draw_cut_list(s, x0, y_bottom, rows, out_rows=None) -> float:
    """切割清单（标题栏上方，GB/T 10609.2：表头在下，序号自下而上）。返回表顶 y。"""
    cols = [("序号", 12), ("规格", 52), ("长度/尺寸", 36), ("数量", 14), ("备注", 66)]
    by = y_bottom
    xx = x0
    for name, w in cols:
        s.rect(xx, by, w, 7, THIN)
        s.text(xx + 1.5, by + 2.2, name, 3)
        xx += w
    by += 7
    for r in sorted(rows, key=lambda r: r["item"]):
        size = f"L={r['length_mm']:g}" if r["kind"] == "member" else "×".join(f"{v:g}" for v in r["size_mm"])
        note = "两端平切 90°" if r["kind"] == "member" else ("焊后加工" if r.get("machined") else "")
        xx = x0
        for (_, w), val in zip(cols, [r["item"], r["spec"], size, r["qty"], note]):
            s.rect(xx, by, w, 7, THIN)
            s.text(xx + 1.5, by + 2.2, str(val), 3)
            xx += w
        if out_rows is not None:
            out_rows.append([r["item"], size, r["qty"]])
        by += 7
    s.text(x0, by + 2, "切割清单", 3.5)
    return by + 7


def _iso_top_features(s, iso, part, ztop):
    """轴测图上补画顶面可见的孔口与型腔（俯视可见面）；底面特征被遮挡不画。"""
    b = part.bbox
    # 相机方向 (-1,-1,1)：可见面为顶面、前面（y 最小）、左面（x 最小）；对面打通的通孔在可见面上也有孔口
    vis = {"top": ("bottom", 2, b.zmax), "front": ("back", 1, b.ymin), "left": ("right", 0, b.xmin)}
    from .ir.resolve import face_point
    outer = {"top": b.zmax, "bottom": b.zmin, "front": b.ymin, "back": b.ymax, "left": b.xmin, "right": b.xmax}
    for h in part.holes:
        inner = h.at is not None and abs(h.at - outer[h.face]) > 1e-6  # 孔在内侧台阶面上
        for face, (opp, ax, val) in vis.items():
            if not ((h.face == face and not inner) or (h.through and h.face == opp)):
                continue  # 内侧面上的孔口多被遮挡不画；通孔在对面外表面的出口照画
            if h.slot:  # 腰型孔：孔口轮廓（两半圆 + 两直边）
                for i in h.instances:
                    p1, p2, rad = h.capsule(i)
                    a_ = math.atan2(p2[1] - p1[1], p2[0] - p1[0])
                    uv = [(p2[0] + rad * math.cos(a_ + math.radians(d)), p2[1] + rad * math.sin(a_ + math.radians(d)))
                          for d in range(-90, 91, 10)]
                    uv += [(p1[0] + rad * math.cos(a_ + math.radians(d)), p1[1] + rad * math.sin(a_ + math.radians(d)))
                           for d in range(90, 271, 10)]
                    ring = []
                    for q2 in uv:
                        q = list(face_point(h.face, q2, b, h.at))
                        q[ax] = val
                        ring.append(iso(*q))
                    s.poly(ring, THICK)
                continue
            r = (h.max_dia if h.face == face else h.drill) / 2
            u, w = [k for k in range(3) if k != ax]
            for i in h.instances:
                ring = []
                for k in range(36):
                    q = list(i.xyz)
                    q[ax] = val
                    q[u] += r * math.cos(2 * math.pi * k / 36)
                    q[w] += r * math.sin(2 * math.pi * k / 36)
                    ring.append(iso(*q))
                s.poly(ring, THICK)
    for pk in part.pockets:
        if not (pk["face"] == "top" or (pk.get("through") and pk["face"] == "bottom")):
            continue
        pts = pk["profile"].drawn()
        s.poly([iso(x, y, ztop) for x, y in pts], THICK)
        if pk["face"] == "top" and not pk.get("through"):
            # 盲型腔：侧壁竖直、口部为凸多边形时，底面上的点可见 ⇔ 其投影落在口部投影之内（视线从口部进入）
            zb = ztop - pk["depth"]
            mouth = [iso(x, y, ztop) for x, y in pts]
            n = len(pts)
            segs = [(iso(*pts[i], zb), iso(*pts[(i + 1) % n], zb)) for i in range(n)]
            for i in range(n):  # 尖角处的竖棱（圆角/圆形型腔无棱）
                e1 = (pts[i][0] - pts[i - 1][0], pts[i][1] - pts[i - 1][1])
                e2 = (pts[(i + 1) % n][0] - pts[i][0], pts[(i + 1) % n][1] - pts[i][1])
                l1, l2 = math.hypot(*e1), math.hypot(*e2)
                if l1 and l2 and (e1[0] * e2[0] + e1[1] * e2[1]) / (l1 * l2) < math.cos(math.radians(20)):
                    segs.append((iso(*pts[i], ztop), iso(*pts[i], zb)))
            for a, b in segs:
                c = _clip_convex(a, b, mouth)
                if c:
                    s.line(*c[0], *c[1], THICK)


def _clip_convex(a, b, poly):
    """线段 ab 裁剪到凸多边形 poly 内（Cyrus–Beck），无交返回 None。"""
    n = len(poly)
    area = sum(poly[i][0] * poly[(i + 1) % n][1] - poly[(i + 1) % n][0] * poly[i][1] for i in range(n))
    sg = 1.0 if area > 0 else -1.0
    t0, t1 = 0.0, 1.0
    dx, dy = b[0] - a[0], b[1] - a[1]
    for i in range(n):
        p, q = poly[i], poly[(i + 1) % n]
        nx, ny = -(q[1] - p[1]) * sg, (q[0] - p[0]) * sg  # 内法向
        num = nx * (a[0] - p[0]) + ny * (a[1] - p[1])
        den = nx * dx + ny * dy
        if abs(den) < 1e-12:
            if num < -1e-9:
                return None
            continue
        t = -num / den
        if den > 0:
            t0 = max(t0, t)
        else:
            t1 = min(t1, t)
        if t0 > t1 - 1e-9:
            return None
    return (a[0] + t0 * dx, a[1] + t0 * dy), (a[0] + t1 * dx, a[1] + t1 * dy)


ISO_VIEW = (-1.0, -1.0)  # 轴测图视线水平分量（_iso_xy：观察者在 −X、−Y、+Z 方向）


def _iso_prism_visible(s, iso, pts, z0, z1) -> bool:
    """凸轮廓直棱柱的消隐画法：只画朝向观察者的侧面的底边，竖棱只画轮廓线（可见/不可见交界）和
    两可见侧面相交的尖角。非凸轮廓返回 False（由调用方按旧画法）。"""
    n = len(pts)
    if n < 3:
        return False
    area = sum(pts[i][0] * pts[(i + 1) % n][1] - pts[(i + 1) % n][0] * pts[i][1] for i in range(n)) / 2
    sg = 1.0 if area > 0 else -1.0
    cross = [((pts[(i + 1) % n][0] - pts[i][0]) * (pts[(i + 2) % n][1] - pts[(i + 1) % n][1])
              - (pts[(i + 1) % n][1] - pts[i][1]) * (pts[(i + 2) % n][0] - pts[(i + 1) % n][0])) * sg
             for i in range(n)]
    if any(c < -1e-9 for c in cross):
        return False
    vis = []
    for i in range(n):  # 边 i: pts[i] → pts[i+1]，外法向（逆时针）= (dy, −dx)
        dx, dy = pts[(i + 1) % n][0] - pts[i][0], pts[(i + 1) % n][1] - pts[i][1]
        vis.append((dy * sg) * ISO_VIEW[0] + (-dx * sg) * ISO_VIEW[1] > 1e-9)
    for i in range(n):
        if vis[i]:
            s.line(*iso(*pts[i], z0), *iso(*pts[(i + 1) % n], z0), THICK)
    for i in range(n):  # 顶点 i+1 在边 i 与边 i+1 之间
        a, b = vis[i], vis[(i + 1) % n]
        e1 = (pts[(i + 1) % n][0] - pts[i][0], pts[(i + 1) % n][1] - pts[i][1])
        e2 = (pts[(i + 2) % n][0] - pts[(i + 1) % n][0], pts[(i + 2) % n][1] - pts[(i + 1) % n][1])
        l1, l2 = math.hypot(*e1), math.hypot(*e2)
        sharp = l1 > 0 and l2 > 0 and (e1[0] * e2[0] + e1[1] * e2[1]) / (l1 * l2) < math.cos(math.radians(20))
        if a != b or (a and b and sharp):
            q = pts[(i + 1) % n]
            s.line(*iso(*q, z0), *iso(*q, z1), THICK)
    return True


def _draw_iso(s, cx, cy, sc, box, part, asm):
    iso = _iso_xy(cx, cy, sc, box)
    if part is not None and part.base_type not in ("shaft",) and not part.weld_bodies:
        _iso_top_features(s, iso, part, box.zmax)
    if part is not None and part.profile is not None and part.base_type == "sheet_metal" \
            and part.profile.shape == "rect" and not part.profile.data.get("corner_radius"):
        from .ir.resolve import Box  # 矩形底板 + 翻边：全是长方体 → 画家算法消隐
        u0, u1, v0, v1 = part.profile.bbox2d
        boxes = [Box(u0, u1, v0, v1, 0.0, part.thickness)] + [bb for _, bb in _sheet_flange_boxes(part)]
        for bb in _painter_order(boxes):
            _iso_box_faces(s, iso, bb)
        _iso_top_features(s, iso, part, part.thickness)  # 底板上的孔画在最上层
        return
    if part is not None and part.profile is not None and part.base_type in ("extrude", "sheet_metal"):
        pts = part.profile.drawn()
        z0, z1 = 0.0, part.thickness
        s.poly([iso(x, y, z1) for x, y in pts], THICK)
        if _iso_prism_visible(s, iso, pts, z0, z1):
            pass  # 凸轮廓：已按可见性画底边与竖棱（消隐）
        elif part.profile.shape == "circle" or pts is not part.profile.outline:  # 圆/圆角：只画轮廓切线
            s.poly([iso(x, y, z0) for x, y in pts], THIN)
            xs = [iso(x, y, z0)[0] for x, y in pts]
            for k in (xs.index(min(xs)), xs.index(max(xs))):
                s.line(*iso(*pts[k], z0), *iso(*pts[k], z1), THICK)
        else:
            s.poly([iso(x, y, z0) for x, y in pts], THIN)
            for x, y in pts:
                s.line(*iso(x, y, z0), *iso(x, y, z1), THICK)
        if part.base_type == "sheet_metal":
            for _, bb in _sheet_flange_boxes(part):
                _iso_box(s, iso, bb, THICK)
        return
    if part is not None and part.base_type == "shaft":
        z = 0.0
        for L, d in part.shaft_steps:
            ring = [(d / 2 * math.cos(2 * math.pi * k / 36), d / 2 * math.sin(2 * math.pi * k / 36)) for k in range(36)]
            s.poly([iso(x, y, z) for x, y in ring], THIN)
            s.poly([iso(x, y, z + L) for x, y in ring], THICK)
            xs = [iso(x, y, z)[0] for x, y in ring]
            for k in (xs.index(min(xs)), xs.index(max(xs))):
                s.line(*iso(*ring[k], z), *iso(*ring[k], z + L), THICK)
            z += L
        return
    from .ir.resolve import Box
    if asm is not None and any(_iso_axis(i) for i in asm.instances):
        _draw_asm_iso(s, iso, asm)
        return
    boxes = [i.world_bbox for i in asm.instances] if asm is not None else \
        ([Box(*b["box"]) for b in part.weld_bodies] if part is not None and part.weld_bodies else [box])
    if len(boxes) > 1:  # 焊接件杆件/板、装配零件包围盒 → 画家算法消隐（白色填充可见面，由远及近）
        for bb in _painter_order(_disjoint_boxes(boxes)):
            _iso_box_faces(s, iso, bb)
        return
    for bb in boxes:
        _iso_box(s, iso, bb, THIN if asm is not None or len(boxes) > 1 else THICK)


def _iso_axis(inst):
    """回转件（有 revolve 数据）的世界轴向：返回 (轴下标 0/1/2, 局部→世界变换) ；轴不平行于坐标轴则 None。"""
    rev = getattr(getattr(inst, "source", None), "revolve", None)
    if not rev:
        return None
    from .ir.assembly_resolve import apply
    o, e = apply(inst.transform, (0.0, 0.0, 0.0)), apply(inst.transform, (0.0, 0.0, 1.0))
    d = [e[k] - o[k] for k in range(3)]
    k = max(range(3), key=lambda k_: abs(d[k_]))
    if abs(abs(d[k]) - 1) > 1e-6:
        return None
    return k


def _hull(pts):
    pts = sorted(set((round(x, 4), round(y, 4)) for x, y in pts))
    if len(pts) < 3:
        return pts

    def half(ps):
        h = []
        for p in ps:
            while len(h) >= 2 and (h[-1][0] - h[-2][0]) * (p[1] - h[-2][1]) - (h[-1][1] - h[-2][1]) * (p[0] - h[-2][0]) <= 0:
                h.pop()
            h.append(p)
        return h
    lo, up = half(pts), half(pts[::-1])
    return lo[:-1] + up[:-1]


def _draw_asm_iso(s, iso, asm):
    """装配轴测图：回转件（销、轴、轴承、螺钉）画成圆柱段，其余零件画包围盒；全部按画家算法由远及近、白色填充消隐。
    圆柱段在别的零件包围盒内（销在孔里、轴在轴承内圈里）的部分不画。"""
    from .ir.assembly_resolve import apply
    from .ir.resolve import Box
    view = (-1.0, -1.0, 1.0)  # 观察者方向（_iso_xy）
    cyl_insts = {i.id: _iso_axis(i) for i in asm.instances}
    boxes = _disjoint_boxes([i.world_bbox for i in asm.instances if cyl_insts[i.id] is None])
    cyls = []  # (轴下标, 轴线上的另两坐标, z0, z1, 外半径, 内半径)
    for inst in asm.instances:
        k = cyl_insts[inst.id]
        if k is None:
            continue
        for z0, z1, do, di in inst.source.revolve:
            a, b = apply(inst.transform, (0.0, 0.0, z0)), apply(inst.transform, (0.0, 0.0, z1))
            o = [a[j] for j in range(3) if j != k]
            cyls.append((k, o, min(a[k], b[k]), max(a[k], b[k]), do / 2, di / 2))

    def cut(lo, hi, holes):  # 区间减去若干区间
        segs = [(lo, hi)]
        for h0, h1 in holes:
            segs = [q for s0, s1 in segs for q in ((s0, min(s1, h0)), (max(s0, h1), s1)) if q[1] - q[0] > 1e-6]
        return segs
    items = [("box", bb) for bb in boxes]
    for n, (k, o, lo, hi, r, ri) in enumerate(cyls):
        holes = []
        others = [j for j in range(3) if j != k]
        for bb in boxes:  # 轴线穿过的包围盒（销在孔里）
            mn, mx = (bb.xmin, bb.ymin, bb.zmin), (bb.xmax, bb.ymax, bb.zmax)
            if all(mn[j] - 1e-6 <= o[t] <= mx[j] + 1e-6 for t, j in enumerate(others)):
                holes.append((mn[k], mx[k]))
        for m, (k2, o2, lo2, hi2, r2, ri2) in enumerate(cyls):  # 同轴更粗的圆柱（轴在轴承内圈里）
            if m != n and k2 == k and math.dist(o, o2) < 1e-6 and (r2 > r + 1e-6 or (abs(r2 - r) < 1e-6 and m < n)):
                holes.append((lo2, hi2))
        for a0, a1 in cut(lo, hi, holes):
            mn, mx = [0.0] * 3, [0.0] * 3
            mn[k], mx[k] = a0, a1
            for t, j in enumerate(others):
                mn[j], mx[j] = o[t] - r, o[t] + r
            items.append(("cyl", Box(mn[0], mx[0], mn[1], mx[1], mn[2], mx[2]), (k, o, a0, a1, r, ri)))
    mouths = []  # 非回转零件上的孔口（座孔、螺纹孔、过孔）：(轴下标, 世界坐标, 半径)
    for inst in asm.instances:
        if cyl_insts[inst.id] is not None:
            continue
        for f in getattr(inst.source, "fits", []) or []:
            if f.get("role") not in ("hole", "tap", "bore"):
                continue
            for q in ("p0", "p1"):
                w = apply(inst.transform, tuple(f[q]))
                w2 = apply(inst.transform, tuple(f["p1" if q == "p0" else "p0"]))
                k = max(range(3), key=lambda j: abs(w2[j] - w[j]))
                mouths.append((k, w, f["d"] / 2))
    order = _painter_order([it[1] for it in items])
    by_box = {id(it[1]): it for it in items}
    for bb in order:
        it = by_box[id(bb)]
        if it[0] == "box":
            _iso_box_faces(s, iso, bb)
            lo_, hi_ = (bb.xmin, bb.ymin, bb.zmin), (bb.xmax, bb.ymax, bb.zmax)
            face = (lo_[0], lo_[1], hi_[2])  # 可见面：−X、−Y、+Z
            for k, w, r in mouths:
                if abs(w[k] - face[k]) > 1e-6:
                    continue
                oth = [j for j in range(3) if j != k]
                if not all(lo_[j] + r - 1e-6 <= w[j] <= hi_[j] - r + 1e-6 for j in oth):
                    continue
                pts = []
                for q in range(36):
                    p3 = list(w)
                    p3[oth[0]] += r * math.cos(2 * math.pi * q / 36)
                    p3[oth[1]] += r * math.sin(2 * math.pi * q / 36)
                    pts.append(iso(*p3))
                s.poly(pts, THICK)
            continue
        k, o, a0, a1, r, ri = it[2]
        others = [j for j in range(3) if j != k]

        def ring(z, rr):
            out = []
            for q in range(36):
                p3 = [0.0] * 3
                p3[k] = z
                p3[others[0]] = o[0] + rr * math.cos(2 * math.pi * q / 36)
                p3[others[1]] = o[1] + rr * math.sin(2 * math.pi * q / 36)
                out.append(iso(*p3))
            return out
        r0, r1 = ring(a0, r), ring(a1, r)
        s.face(_hull(r0 + r1))
        zc = a1 if view[k] > 0 else a0  # 朝向观察者的端面
        s.face(ring(zc, r))
        if ri > 1e-6:
            s.poly(ring(zc, ri), THICK)


def _box_minus(a, b):
    """长方体 a 减去 b，返回互不相交的子长方体（先 x 两侧、再 y 两侧、最后 z 上下）。"""
    from .ir.resolve import Box
    if not (a.xmin < b.xmax and b.xmin < a.xmax and a.ymin < b.ymax and b.ymin < a.ymax
            and a.zmin < b.zmax and b.zmin < a.zmax):
        return [a]
    out = []
    x0, x1 = max(a.xmin, b.xmin), min(a.xmax, b.xmax)
    y0, y1 = max(a.ymin, b.ymin), min(a.ymax, b.ymax)
    if a.xmin < x0:
        out.append(Box(a.xmin, x0, a.ymin, a.ymax, a.zmin, a.zmax))
    if x1 < a.xmax:
        out.append(Box(x1, a.xmax, a.ymin, a.ymax, a.zmin, a.zmax))
    if a.ymin < y0:
        out.append(Box(x0, x1, a.ymin, y0, a.zmin, a.zmax))
    if y1 < a.ymax:
        out.append(Box(x0, x1, y1, a.ymax, a.zmin, a.zmax))
    if a.zmin < b.zmin:
        out.append(Box(x0, x1, y0, y1, a.zmin, b.zmin))
    if b.zmax < a.zmax:
        out.append(Box(x0, x1, y0, y1, b.zmax, a.zmax))
    return out


def _disjoint_boxes(boxes):
    """相交的包围盒（装配：销穿过板）：小的减去大的，得到互不相交的块，画家算法才成立。"""
    vol = lambda b: (b.xmax - b.xmin) * (b.ymax - b.ymin) * (b.zmax - b.zmin)  # noqa: E731
    order = sorted(range(len(boxes)), key=lambda i: -vol(boxes[i]))
    out = []
    for k, i in enumerate(order):
        pieces = [boxes[i]]
        for j in order[:k]:  # 比它大的盒子
            pieces = [q for pc in pieces for q in _box_minus(pc, boxes[j])]
        out += pieces
    return out


def _painter_order(boxes):
    """互不相交的轴向长方体按远→近排序（观察者在 −X、−Y、+Z）：A 在某轴上整体位于 B 的远侧则先画 A。"""
    def behind(a, b):
        return a.xmin >= b.xmax - 1e-9 or a.ymin >= b.ymax - 1e-9 or a.zmax <= b.zmin + 1e-9

    def key(b):
        return -(b.xmin + b.xmax) / 2 - (b.ymin + b.ymax) / 2 + (b.zmin + b.zmax) / 2
    left = sorted(boxes, key=key)
    out = []
    while left:  # 每次取一个"没有别的盒子必须排在它前面"的，平局按中心深度
        pick = next((a for a in left if not any(behind(b, a) and not behind(a, b) for b in left if b is not a)), left[0])
        out.append(pick)
        left.remove(pick)
    return out


def _iso_box_faces(s, iso, bb):
    """长方体的三个可见面（−X、−Y、+Z）：白色填充遮住后面的线，粗实线轮廓。"""
    def P(i, j, k):
        return iso(bb.xmin + i * (bb.xmax - bb.xmin), bb.ymin + j * (bb.ymax - bb.ymin), bb.zmin + k * (bb.zmax - bb.zmin))
    for f in ([(0, 0, 0), (0, 1, 0), (0, 1, 1), (0, 0, 1)], [(0, 0, 0), (1, 0, 0), (1, 0, 1), (0, 0, 1)],
              [(0, 0, 1), (1, 0, 1), (1, 1, 1), (0, 1, 1)]):
        s.face([P(*q) for q in f])


def _iso_box(s, iso, bb, w):
    if True:
        P = {(i, j, k): iso(bb.xmin + i * (bb.xmax - bb.xmin), bb.ymin + j * (bb.ymax - bb.ymin),
                            bb.zmin + k * (bb.zmax - bb.zmin)) for i in (0, 1) for j in (0, 1) for k in (0, 1)}
        for e1, e2 in [((0,0,0),(1,0,0)),((1,0,0),(1,1,0)),((0,0,1),(1,0,1)),((1,0,1),(1,1,1)),((0,1,1),(1,1,1)),
                       ((0,0,1),(0,1,1)),((0,0,0),(0,0,1)),((1,0,0),(1,0,1)),((1,1,0),(1,1,1))]:
            s.line(*P[e1], *P[e2], w)


def render(plan: DrawingPlan, model: ResolvedPart | ResolvedAssembly | None = None) -> tuple[str, dict[str, Any]]:
    W, H = plan.sheet_size
    s = _Svg(W, H)
    s.rect(5, 5, W - 10, H - 10, THICK)  # 图框
    rendered_refs: list[str] = []
    balloon_items: list[int] = []
    cut_rows: list[list[Any]] = []
    bom_rows: list[tuple[Any, Any, Any]] = []
    part = model if isinstance(model, ResolvedPart) else None
    asm = model if isinstance(model, ResolvedAssembly) else None
    box = model.bbox if model is not None else None
    item_of = {}
    if asm is not None:
        for r in plan.bom:
            for iid in r["instances"]:
                item_of[iid] = r["item"]

    if part is not None and part.base_type == "shaft":
        from .render_shaft import render_shaft
        render_shaft(s, plan, part, rendered_refs)
        s.text_bg = False
        if plan.gear_table:
            from .render_gear import draw_param_table
            draw_param_table(s, plan.gear_table)
            rendered_refs.append("gear_table")
    if part is not None and plan.layout == "gear":
        from .render_gear import render_gear
        render_gear(s, plan, part, rendered_refs)
    cut_later: list[Any] = []
    for v in ([] if part is not None and (part.base_type == "shaft" or plan.layout == "gear") else plan.views):
        sc = v.scale * (0.5 if v.type == "iso" else 1)
        cx, cy = v.origin_on_sheet
        if v.type == "flat_pattern":  # 基本视图按 GB 不标名称；展开图需注明
            s.text(cx, cy + v.size_mm[1] / 2 + 4, "展开图", 3.5, "middle")
        if box is None or v.type in ("iso", "flat_pattern"):
            if v.type == "iso" and box is not None:
                _draw_iso(s, cx, cy, sc, box, part, asm)
            elif v.type == "flat_pattern" and part is not None and part.flat_box:
                _draw_flat(s, cx, cy, sc, part, plan, rendered_refs)
            else:
                w, h = v.size_mm
                s.rect(cx - w / 2, cy - h / 2, w, h)
            continue
        ha, va = VIEW_AXES[v.type]
        (ulo, uhi), (wlo, whi) = vrange(box, ha), vrange(box, va)
        mid_u, mid_w = (ulo + uhi) / 2, (wlo + whi) / 2

        def P(u, w_, cx=cx, cy=cy, sc=sc, mid_u=mid_u, mid_w=mid_w):
            return (cx + (u - mid_u) * sc, cy + (w_ - mid_w) * sc)

        def V(p3, ha=ha, va=va, P=P):
            return P(vc(p3, ha), vc(p3, va))

        sec = next((x for x in plan.sections if x.view == v.type and x.kind != "local"), None)
        if sec is not None:
            _draw_section(s, sec, V, v, cx, cy)
            rendered_refs.append(f"section:{sec.label}")
        elif part is not None and v.type in ("top", "bottom") and part.profile is not None:
            pr = part.profile
            if pr.shape == "circle":
                c3 = (*pr.data["center"], 0.0)
                s.circle(*V(c3), pr.data["diameter"] / 2 * sc)
            else:
                s.poly([V((x, y, 0.0)) for x, y in pr.drawn()])
            if part.base_type == "sheet_metal":  # 翻边在俯/仰视图中积聚为板厚条
                for bd, bb in _sheet_flange_boxes(part):
                    vis = (bd["direction"] == "up") == (v.type == "top")
                    x0, y0 = V((bb.xmin, bb.ymin, 0.0))
                    x1, y1 = V((bb.xmax, bb.ymax, 0.0))
                    s.rect(min(x0, x1), min(y0, y1), abs(x1 - x0), abs(y1 - y0), THICK if vis else THIN)
            for pk in part.pockets:
                if pk["face"] in ("top", "bottom"):  # 背面的盲型腔不可见：细虚线
                    hid = pk["face"] != v.type and not pk.get("through")
                    s.poly([V((x, y, 0.0)) for x, y in pk["profile"].drawn()], THIN if hid else THICK,
                           dash=HIDDEN if hid else None)
        elif part is not None and part.base_type == "sheet_metal" and v.type in ("front", "back", "left", "right"):
            _draw_sheet_side(s, part, v.type, ha, P)
        elif part is not None and part.base_type == "shaft" and v.type in ("front", "back", "left", "right"):
            z = 0.0
            for L, d in part.shaft_steps:
                x0, y0 = P(-d / 2, z)
                x1, y1 = P(d / 2, z + L)
                s.rect(x0, y0, x1 - x0, y1 - y0)
                z += L
            s.line(*P(0, box.zmin - 3), *P(0, box.zmax + 3), THIN, "6,1.5,1,1.5")
        elif part is not None and part.weld_bodies:
            _draw_weld_bodies(s, part, v.type, ha, va, P)
        elif asm is not None and plan.asm_section and plan.asm_section["view"] == v.type:
            _draw_asm_section(s, asm, v, plan.asm_section)
            rendered_refs.append("asm_section")
        elif asm is not None:
            _draw_asm_view(s, asm, v.type, ha, va, P, sc)
            if plan.asm_section and plan.asm_section.get("label") and plan.asm_section["parent"] == v.type:
                (ax_, ay_), (bx_, by_) = plan.asm_section["cut_line"]  # 剖切符号：两端粗短画 + 字母
                lb = plan.asm_section["label"]
                for (px_, py_), dx in (((ax_, ay_), 1), ((bx_, by_), -1)):
                    s.line(px_, py_, px_ + dx * 5, py_, 0.7)
                    s.text(px_ + dx * 2, py_ + 2, lb, 5, "middle")
        else:
            x0, y0 = P(ulo, wlo)
            x1, y1 = P(uhi, whi)
            s.rect(x0, y0, x1 - x0, y1 - y0)
        # 序号气泡（主视图）
        if plan.balloons and v.type == plan.views[0].type:
            anchors = []
            if all("pos" in b_ for b_ in plan.balloons):  # 规划层已排布（装配图）
                for b_ in plan.balloons:
                    (ax, ay), (bx_, by_) = b_["anchor"], b_["pos"]
                    d = math.hypot(bx_ - ax, by_ - ay) or 1
                    s.line(ax, ay, bx_ - (bx_ - ax) / d * 4, by_ - (by_ - ay) / d * 4, 0.18)
                    s.parts.append(f"<circle cx='{ax:.2f}' cy='{s.y(ay):.2f}' r='0.7' fill='{STROKE}'/>")
                    s.circle(bx_, by_, 4, THIN)
                    s.text(bx_, by_ - 1.2, str(b_["item"]), 3.5, "middle")
                    rendered_refs.append(f"balloon:{b_['item']}")
                    balloon_items.append(b_["item"])
                plan_balloons_done = True
            else:
                plan_balloons_done = False
            for b_ in ([] if plan_balloons_done else plan.balloons):
                if "at" in b_:
                    anchors.append((b_, P(*b_["at"])))
                    continue
                inst = next(i for i in asm.instances if i.id == b_["instance"])
                (bu0, bu1), (bw0, bw1) = vrange(inst.world_bbox, ha), vrange(inst.world_bbox, va)
                anchors.append((b_, P((bu0 + bu1) / 2, (bw0 + bw1) / 2)))
            anchors.sort(key=lambda a: -a[1][1])  # 自上而下排列，引线不交叉
            for placed, (b_, cpt) in enumerate(anchors):
                bx_ = cx + v.size_mm[0] / 2 + 15
                by_ = cy + v.size_mm[1] / 2 - 10 - placed * 12
                s.line(*cpt, bx_ - 4, by_, 0.18)
                s.parts.append(f"<circle cx='{cpt[0]:.2f}' cy='{s.y(cpt[1]):.2f}' r='0.7' fill='{STROKE}'/>")
                s.circle(bx_, by_, 4, THIN)
                s.text(bx_, by_ - 1.2, str(b_["item"]), 3.5, "middle")
                rendered_refs.append(f"balloon:{b_['item']}")
                balloon_items.append(b_["item"])
        # 孔（只在与孔所在面平行的视图中画实形）
        for h in ([] if part is None else part.holes):
            if FACE_AXIS[h.face] != FACE_AXIS.get(v.type):
                continue
            hid = HIDDEN if (h.face != v.type and not h.through) else None  # 背面盲孔不可见：细虚线
            if h.circular and not (sec is not None and sec.kind in ("full", "step")):  # 分布圆：细点画线圆
                cc = h.circular
                cpt = V(face_point(h.face, (cc["center_u"], cc["center_v"]), box, h.at))
                s.circle(*cpt, cc["pcd"] / 2 * sc, 0.18, "4,1,1,1")
            for inst in h.instances:
                c = V(inst.xyz)
                if sec is not None and sec.kind in ("full", "step"):
                    # 全剖/阶梯剖：剖切面前方已移去；剖到的孔截面由剖面几何画出，这里只补中心线；未剖到的孔不可见，不画
                    if _hole_cut_by(sec, h, inst):
                        s.line(c[0] - h.drill * 0.7 * sc, c[1], c[0] + h.drill * 0.7 * sc, c[1], 0.18, "4,1,1,1")
                        s.line(c[0], c[1] - h.drill * 0.7 * sc, c[0], c[1] + h.drill * 0.7 * sc, 0.18, "4,1,1,1")
                    continue
                if hid:
                    s.circle(*c, h.drill / 2 * sc, THIN, hid)
                    s.line(c[0] - h.drill * 0.7 * sc, c[1], c[0] + h.drill * 0.7 * sc, c[1], 0.18, "4,1,1,1")
                    s.line(c[0], c[1] - h.drill * 0.7 * sc, c[0], c[1] + h.drill * 0.7 * sc, 0.18, "4,1,1,1")
                    continue
                if h.slot:
                    p1, p2, rad = h.capsule(inst)
                    a_ = math.atan2(p2[1] - p1[1], p2[0] - p1[0])
                    uv = [(p2[0] + rad * math.cos(a_ + math.radians(d)), p2[1] + rad * math.sin(a_ + math.radians(d)))
                          for d in range(-90, 91, 15)]
                    uv += [(p1[0] + rad * math.cos(a_ + math.radians(d)), p1[1] + rad * math.sin(a_ + math.radians(d)))
                           for d in range(90, 271, 15)]
                    s.poly([V(face_point(h.face, q, box, h.at)) for q in uv])
                    s.line(*V(face_point(h.face, p1, box, h.at)), *V(face_point(h.face, p2, box, h.at)), 0.18, "4,1,1,1")
                    continue
                s.circle(*c, h.drill / 2 * sc)
                if h.type == "tapped":
                    rr = h.nominal / 2 * sc  # 内螺纹大径：约 3/4 圈细实线圆弧（GB/T 4459.1），缺口在右上象限
                    s.poly([(c[0] + rr * math.cos(math.radians(a_)), c[1] + rr * math.sin(math.radians(a_)))
                            for a_ in range(100, 351, 10)], THIN, close=False)
                if (h.cbore or h.csink) and h.face == v.type and h.type != "tapped":
                    # 沉孔/锥口外圆是可见轮廓（粗实线），只在孔口一侧可见；螺纹孔端视图的倒角圆省略不画（GB/T 4459.1）
                    s.circle(*c, h.max_dia / 2 * sc, THICK)
                s.line(c[0] - h.max_dia * 0.7 * sc, c[1], c[0] + h.max_dia * 0.7 * sc, c[1], 0.18, "4,1,1,1")
                s.line(c[0], c[1] - h.max_dia * 0.7 * sc, c[0], c[1] + h.max_dia * 0.7 * sc, 0.18, "4,1,1,1")

        for sec_ in plan.sections:
            if sec_.kind == "local" and sec_.view == v.type:
                _draw_local(s, sec_, V)
                rendered_refs.append(f"section:{sec_.label}")
            elif sec_.parent == v.type and part is not None:
                cut_later.append((sec_, V, v.type))  # 剖切符号最后画：字母避开已画好的尺寸、引线与文字
        # 尺寸
        vx0, vy0 = P(ulo, wlo)
        vx1, vy1 = P(uhi, whi)
        from .detailing import ordinate_layout
        ord_lay = ordinate_layout(plan, v, part.bbox if part is not None else (asm.bbox if asm is not None else None)) \
            if (part is not None or asm is not None) else {}
        has_ord_h = any(d.view == v.type and d.kind == "ordinate" and d.axis == ha for d in plan.dims)
        prec = [d.extra["normal"] for d in plan.dims if d.view == v.type and d.kind in ("datum", "gtol", "roughness")
                and d.extra and not d.extra.get("attach")]
        prec_below = any(tuple(n) == (0, -1) for n in prec)
        prec_left = any(tuple(n) == (-1, 0) for n in prec)
        for d in plan.dims:
            if d.view != v.type:
                continue
            if d.kind == "linear" and (d.extra or {}).get("fit"):  # 配合尺寸：横跨直径，延伸出视图后写配合代号
                (ax_, ay_), (bx_, by_) = d.extra["sheet_pts"]
                px_, py_ = d.extra["sheet_pos"]
                tw_ = _text_w(d.text, 3.5)
                right = d.extra.get("side", 1) > 0
                if d.extra["orient"] == "h":
                    s.line(min(ax_, px_), ay_, max(bx_, px_ + tw_), ay_, 0.18)
                    s.text(px_, ay_ + 0.8, d.text, 3.5)
                else:
                    s.line(ax_, min(ay_, py_), ax_, max(by_, py_ + tw_), 0.18)
                    s.text(ax_ - 0.8, py_, d.text, 3.5, "start", -90)
                del right
                _arrow_pair(s, (ax_, ay_), (bx_, by_))
                rendered_refs.append(d.ref)
                continue
            if d.kind == "linear" and ((d.extra or {}).get("install") or (d.extra or {}).get("pitch")) \
                    and d.extra.get("sheet_pts"):  # 安装尺寸 / 等距节距：规划层给定图纸坐标
                (ax_, ay_), (bx_, by_) = d.extra["sheet_pts"]
                px_, py_ = d.extra["sheet_pos"]
                if d.extra["orient"] == "h":  # 预览只画包络盒：补画安装孔（CAD 视图自带）
                    for hx, hy, hr in d.extra.get("holes", []):
                        s.circle(hx, hy, hr, THIN)
                if d.extra["orient"] == "h":
                    s.line(ax_, ay_ + 1, ax_, py_ + 2, 0.18)
                    s.line(bx_, by_ + 1, bx_, py_ + 2, 0.18)
                    s.line(ax_, py_, bx_, py_, 0.18)
                    s.text((ax_ + bx_) / 2, py_ + 1, d.text, 3.5, "middle")
                else:
                    s.line(ax_ + 1, ay_, px_ + 2, ay_, 0.18)
                    s.line(bx_ + 1, by_, px_ + 2, by_, 0.18)
                    s.line(px_, ay_, px_, by_, 0.18)
                    s.text(px_ - 1, (ay_ + by_) / 2, d.text, 3.5, "middle", -90)
                rendered_refs.append(d.ref)
                continue
            if d.kind == "linear" and d.axis == ha and d.at is None:
                yy = vy0 - (22 if has_ord_h or prec_below else 10)  # 坐标尺寸区、基准/框格之外
                if (d.extra or {}).get("pushed"):  # 规划层外移（让开配合尺寸文字）
                    yy = d.extra["sheet_pos"][1]
                s.line(vx0, yy, vx1, yy)
                s.line(vx0, vy0 - 2, vx0, yy - 1.5)
                s.line(vx1, vy0 - 2, vx1, yy - 1.5)
                tw_ = _text_w(d.text, 3.5)
                if tw_ > abs(vx1 - vx0) - 1:  # 太窄（板厚等）：数字移到右界线之外，尺寸线延长到数字下
                    xr_ = max(vx0, vx1)
                    s.line(xr_, yy, xr_ + 1.5 + tw_, yy)
                    s.text(xr_ + 1.5 + tw_ / 2, yy + 1, d.text, 3.5, "middle")
                else:
                    s.text((vx0 + vx1) / 2, yy + 1, d.text, 3.5, "middle")
            elif d.kind == "linear" and d.axis == va and d.at is None:
                xx = vx0 - (22 if prec_left else 10)  # 纵向坐标尺寸画在右侧；左侧让开基准符号
                if (d.extra or {}).get("pushed"):
                    xx = d.extra["sheet_pos"][0]
                s.line(xx, vy0, xx, vy1)
                s.line(vx0 - 2, vy0, xx - 1.5, vy0)
                s.line(vx0 - 2, vy1, xx - 1.5, vy1)
                tw_ = _text_w(d.text, 3.5)
                if tw_ > abs(vy1 - vy0) - 1:  # 太窄（板厚等）：数字移到上界线之外，尺寸线延长到数字旁
                    yt_ = min(vy0, vy1)
                    s.line(xx, yt_, xx, yt_ - 1.5 - tw_)
                    s.text(xx - 1, yt_ - 1.5 - tw_ / 2, d.text, 3.5, "middle", -90)
                else:
                    s.text(xx - 1, (vy0 + vy1) / 2, d.text, 3.5, "middle", -90)
            elif d.kind == "linear" and d.at is not None:
                px, py = P(*d.at)
                s.text(vx1 + 6, py, d.text, 3, "start")
            elif d.kind == "ordinate" and d.at is not None:
                px, py = P(*d.at)
                # 与剖切符号起止端（粗短画 + 箭头）同一位置的坐标值外移，避开剖切符号
                cut_ends = [V(sec_.cut_points[k]) for sec_ in plan.sections if sec_.parent == v.type and sec_.cut_points
                            for k in (0, -1)]
                if d.axis == ha:  # 引线：自轮廓外 1 mm 起，1 mm 直段后折到摊开后的数字位置
                    tx_ = ord_lay.get(id(d), px)
                    o = 8.0 if any(abs(ex_ - tx_) < 13 and ey_ < vy0 + 1 for ex_, ey_ in cut_ends) else 0.0  # 剖切箭头+字母约 13 mm
                    s.line(px, vy0 - 1 - o, px, vy0 - 2 - o, 0.18)
                    s.line(px, vy0 - 2 - o, tx_, vy0 - 4 - o, 0.18)
                    s.text(tx_, vy0 - 6.5 - o, d.text, 2.5, "middle", -90)
                else:
                    ty_ = ord_lay.get(id(d), py)
                    o = 8.0 if any(abs(ey_ - ty_) < 13 and ex_ > vx1 - 1 for ex_, ey_ in cut_ends) else 0.0
                    s.line(vx1 + 1 + o, py, vx1 + 2 + o, py, 0.18)
                    s.line(vx1 + 2 + o, py, vx1 + 4 + o, ty_, 0.18)
                    s.text(vx1 + 5 + o, ty_ - 0.8, d.text, 2.5)
            elif d.kind == "weld":
                _weld_symbol(s, *P(*d.at), d)
            elif d.kind in ("datum", "gtol", "roughness"):
                from . import render_gdt
                render_gdt.draw(s, d.kind, *P(*d.at), d.extra, d.text)
            elif d.kind == "tag":
                px, py = P(*d.at)
                hr = next((h.max_dia / 2 * sc for h in (part.holes if part else []) for i in h.instances
                           if abs(vc(i.xyz, ha) - d.at[0]) < 1e-6 and abs(vc(i.xyz, va) - d.at[1]) < 1e-6), 1.0)
                s.text(px + hr * 0.75 + 0.6, py + hr * 0.75 + 0.4, d.text, 2.5)
            elif d.kind == "origin":
                px, py = P(*d.at)
                s.line(px, py, px + 8, py, THIN)
                s.line(px, py, px, py + 8, THIN)
                s.parts.append(_arrow_head(s, (px + 8, py), (1, 0)))
                s.parts.append(_arrow_head(s, (px, py + 8), (0, 1)))
                s.text(px + 9, py - 3, "X", 2.5)  # 轴向箭头沿视图边线：字母放在边线外侧
                s.text(px - 3, py + 9, "Y", 2.5)
                s.text(px - 9, py - 4, d.text, 2.5)
            elif d.kind in ("callout", "radius", "diameter") and d.extra and "label" in d.extra:
                lb = d.extra["label"]
                s.line(*lb["anchor"], *lb["elbow"], 0.18)
                s.line(*lb["elbow"], *lb["end"], 0.18)
                if lb.get("on_edge"):  # 止于孔轮廓：箭头
                    (ax_, ay_), (ex_, ey_) = lb["anchor"], lb["elbow"]
                    n_ = math.hypot(ex_ - ax_, ey_ - ay_) or 1.0
                    s.parts.append(_arrow_head(s, (ax_, ay_), ((ax_ - ex_) / n_, (ay_ - ey_) / n_)))
                else:  # 止于区域内：圆点
                    s.parts.append(f"<circle cx='{lb['anchor'][0]:.2f}' cy='{s.y(lb['anchor'][1]):.2f}' r='0.5' fill='{STROKE}'/>")
                s.text(lb["text_at"][0], lb["text_at"][1], d.text, 3)
            elif d.kind in ("callout", "radius", "diameter"):
                px, py = P(*d.at) if d.at is not None else (vx1, vy1)
                tx, ty = px + 12, py + 9
                s.line(px, py, tx, ty, 0.18)
                s.line(tx, ty, tx + _text_w(d.text, 3), ty, 0.18)
                s.text(tx + 0.5, ty + 0.8, d.text, 3)
            rendered_refs.append(d.ref)

    for sec_, V_, vt_ in cut_later:
        _draw_cut_line(s, sec_, part, V_, _dim_boxes(plan, vt_))
    ht_rows: list[list[Any]] = []
    ht_h = 0.0
    if plan.hole_table and plan.hole_table_box:
        bx0, by0, bx1, by1 = plan.hole_table_box
        _draw_hole_table(s, bx0, by0, plan.hole_table, ht_rows)
        rendered_refs.append("hole_table")
        if bx0 > W / 2:  # 孔表在右侧栏：粗糙度移到孔表上方
            ht_h = by1 + 4
    if part is not None and plan.title_block.get("surface_finish"):
        from . import render_gdt
        render_gdt.general_roughness(s, *detailing.general_roughness_at(plan), plan.title_block["surface_finish"])
        rendered_refs.append("general_roughness")
    # 标题栏（右下）
    tbw, tbh = 180, 56
    x0, y0 = W - 5 - tbw, 5
    s.rect(x0, y0, tbw, tbh)
    tb = plan.title_block
    rows = [("名称", tb.get("name", "")), ("图号", tb.get("part_no", "") + (f"  版本 {tb['revision']}" if tb.get("revision") else "")), ("材料", tb.get("material", "")),
            ("比例", tb.get("scale", "")), ("质量", tb.get("mass", "")), ("投影", tb.get("projection", "")),
            ("设计", tb.get("designer", "")), ("日期", tb.get("date", ""))]
    for i, (k, val) in enumerate(rows):
        c, r = i % 2, i // 2
        cx0, cy0 = x0 + c * 90, y0 + tbh - (r + 1) * 14
        s.rect(cx0, cy0, 90, 14, THIN)
        s.text(cx0 + 2, cy0 + 5, k, 3)
        s.text(cx0 + 18, cy0 + 5, val, 4 if k in ("名称", "图号") else 3.5)
    # 更改记录（标题栏左侧，最新在上）
    if plan.revisions:
        cols = list(detailing.REV_COLUMNS)
        rx = x0 - sum(w for _, w in cols)
        ry = y0
        for row in [*detailing.revision_rows(plan), None]:
            vals = [n for n, _ in cols] if row is None else [row.get("rev", ""), row.get("description", "")[:26],
                                                             row.get("date", ""), row.get("by", "")]
            xx = rx
            for (_, w), val in zip(cols, vals):
                s.rect(xx, ry, w, 7, THIN)
                s.text(xx + 1.5, ry + 2.2, str(val), 3)
                xx += w
            ry += 7
        rendered_refs.append("revisions")
    # 技术要求
    ny = y0 + tbh + 6 + 5 * len(plan.notes)
    nx = detailing.notes_x(plan)
    for n in plan.notes:
        s.text(nx, ny, n, 3.5)
        ny -= 5
    if plan.notes:
        rendered_refs.append("notes")
    if plan.cut_list:
        _draw_cut_list(s, x0, y0 + tbh + 12 + 5 * len(plan.notes), plan.cut_list, cut_rows)
        rendered_refs.append("cut_list")
    for ax_ in plan.axes:  # 装配图回转件轴线（细点画线）
        (xa_, ya_), (xb_, yb_) = ax_["pts"]
        s.line(xa_, ya_, xb_, yb_, THIN, dash=CHAIN)
    # 明细表（标题栏上方）
    if plan.bom:  # GB/T 10609.2：表头在下，序号自下而上
        by = y0 + tbh + 12 + 5 * len(plan.notes)
        from .detailing import BOM_COLS, bom_display
        cols = BOM_COLS
        xx = x0
        for name, w in cols:
            s.rect(xx, by, w, 7, THIN)
            s.text(xx + 1.5, by + 2.2, name, 3)
            xx += w
        for r in sorted(plan.bom, key=lambda r: r["item"]):
            by += 7
            xx = x0
            for (name, w), val in zip(cols, bom_display(r)):
                s.rect(xx, by, w, 7, THIN)
                s.text(xx + 1.5, by + 2.2, str(val), 3)
                xx += w
            bom_rows.append((r["item"], r["part_no"], r["qty"]))
    out = {"dim_refs": rendered_refs, "views": [v.type for v in plan.views], "collisions": s.collisions(), "crossings": s.crossings()}
    if asm is not None:
        out.update(bom_rows=bom_rows, balloon_items=balloon_items)
    if plan.hole_table:
        out["hole_table_rows"] = ht_rows
    if plan.layout == "weldment":
        out.update(cut_list_rows=cut_rows, balloon_items=balloon_items,
                   weld_symbols=sum(1 for r in rendered_refs if r.startswith("weld:")))
    return s.render(), out
