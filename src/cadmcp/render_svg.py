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
        self.outlines: list[tuple[float, float, float, float]] = []  # 粗实线（可见轮廓），查压字

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
            for (ax, ay, cx, cy) in self.leaders + self.outlines:
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
              (x - 0.8 * size, y - w / 2 if anchor == "middle" else y - w, x + 0.2 * size, y + w / 2 if anchor == "middle" else y)
        self.texts.append((s, box))
        rot = f" transform='rotate({rotate} {x:.2f} {self.y(y):.2f})'" if rotate else ""
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
    return sum(size if ord(ch) > 0x2E80 else 0.6 * size for ch in s)


HIDDEN = "3,1"  # 细虚线：不可见轮廓


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
            s.text((x0 + x1) / 2, yy + 1, d.text, 3.5, "middle")
        elif d.ref == "flat_h":
            xx = x1 + 10
            s.line(xx, y0, xx, y1)
            s.line(x1 + 2, y0, xx + 1.5, y0)
            s.line(x1 + 2, y1, xx + 1.5, y1)
            s.text(xx + 4.5, (y0 + y1) / 2, d.text, 3.5, "middle", -90)
        elif d.kind == "callout" and d.at is not None:
            px, py = F(*d.at)
            s.text(px, py + 1, d.text, 3, "middle")
        refs.append(d.ref)


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


def _draw_weld_bodies(s, part, vtype, ha, va, P):
    """焊接件视图：每个杆件/板件按包围盒投影；端视的管材画内壁（细实线），圆管画圆。"""
    from .ir.resolve import Box
    depth_axis = next(a for a in "xyz" if a not in (ha[-1], va[-1]))
    for body in sorted(part.weld_bodies, key=lambda b: b["kind"] != "plate"):
        bb = Box(*body["box"])
        (u0, u1), (w0, w1) = vrange(bb, ha), vrange(bb, va)
        x0, y0 = P(u0, w0)
        x1, y1 = P(u1, w1)
        sec = body.get("section")
        if body["kind"] == "member" and body["axis"] == depth_axis and sec and sec["kind"] == "CHS":
            r = (x1 - x0) / 2
            s.circle((x0 + x1) / 2, (y0 + y1) / 2, r)
            s.circle((x0 + x1) / 2, (y0 + y1) / 2, r * (1 - 2 * sec["t"] / sec["h"]), THIN)
            continue
        s.rect(x0, y0, x1 - x0, y1 - y0)
        if body["kind"] == "member" and body["axis"] == depth_axis and sec and sec["hollow"]:
            k = (x1 - x0) * sec["t"] / (bb.xmax - bb.xmin if ha[-1] == "x" else (bb.ymax - bb.ymin if ha[-1] == "y" else bb.zmax - bb.zmin))
            s.rect(x0 + k, y0 + k, x1 - x0 - 2 * k, y1 - y0 - 2 * k, THIN)


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
    孔口处不画轮廓线；阶梯剖转折处不画线；半剖以中心线为界。"""
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
    openings = [(V(a), V(b)) for a, b in g["openings"]]  # 孔口/型腔口：实体轮廓上去掉
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
    g2["openings"] = list(g["openings"]) + [
        (poly[k], poly[(k + 1) % len(poly)]) for poly in g["solid"] for k in range(len(poly))
        if on_break(poly[k]) and on_break(poly[(k + 1) % len(poly)]) and abs(poly[k][2] - poly[(k + 1) % len(poly)][2]) > 1e-9]
    _draw_section(s, sec, V, None, 0, 0, g2)
    for w in g["breaks"]:
        s.poly([V(q) for q in w], THIN, close=False)


def _draw_cut_line(s, sec, part, V):
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
        L = math.dist(a, b) or 1
        ox, oy = (a[0] - b[0]) / L, (a[1] - b[1]) / L  # 沿剖切线朝外：字母在箭头外侧，不压视图轮廓
        s.text(a[0] + dx * 9 + ox * 2.5, a[1] + dy * 9 + oy * 2.5 - 1.5, sec.label, 5, "middle")
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

        cands = [(a[0] + sx * 4.5, a[1] + sy * 4.5) for sx, sy in ((-1, -1), (1, -1), (-1, 1), (1, 1))]
        tx, ty = max(cands, key=clearance)
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
    for h in part.holes:
        if h.slot:
            continue
        for face, (opp, ax, val) in vis.items():
            if not (h.face == face or (h.through and h.face == opp)):
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
        pts = pk["profile"].outline
        s.poly([iso(x, y, ztop) for x, y in pts], THICK)
        if pk["face"] == "top" and not pk.get("through") and pk["profile"].shape != "circle":
            zb = ztop - pk["depth"]
            s.poly([iso(x, y, zb) for x, y in pts], THIN)


def _draw_iso(s, cx, cy, sc, box, part, asm):
    iso = _iso_xy(cx, cy, sc, box)
    if part is not None and part.base_type not in ("shaft",) and not part.weld_bodies:
        _iso_top_features(s, iso, part, box.zmax)
    if part is not None and part.profile is not None and part.base_type in ("extrude", "sheet_metal"):
        pts = part.profile.outline
        z0, z1 = 0.0, part.thickness
        s.poly([iso(x, y, z0) for x, y in pts], THIN)
        s.poly([iso(x, y, z1) for x, y in pts], THICK)
        if part.profile.shape == "circle":  # 只画轮廓切线
            xs = [iso(x, y, z0)[0] for x, y in pts]
            for k in (xs.index(min(xs)), xs.index(max(xs))):
                s.line(*iso(*pts[k], z0), *iso(*pts[k], z1), THICK)
        else:
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
    boxes = [i.world_bbox for i in asm.instances] if asm is not None else \
        ([Box(*b["box"]) for b in part.weld_bodies] if part is not None and part.weld_bodies else [box])
    for bb in boxes:
        _iso_box(s, iso, bb, THIN if asm is not None or len(boxes) > 1 else THICK)


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
    for v in ([] if part is not None and part.base_type == "shaft" else plan.views):
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
                s.poly([V((x, y, 0.0)) for x, y in pr.outline])
            if part.base_type == "sheet_metal":  # 翻边在俯/仰视图中积聚为板厚条
                for bd, bb in _sheet_flange_boxes(part):
                    vis = (bd["direction"] == "up") == (v.type == "top")
                    x0, y0 = V((bb.xmin, bb.ymin, 0.0))
                    x1, y1 = V((bb.xmax, bb.ymax, 0.0))
                    s.rect(min(x0, x1), min(y0, y1), abs(x1 - x0), abs(y1 - y0), THICK if vis else THIN)
            for pk in part.pockets:
                if pk["face"] in ("top", "bottom"):  # 背面的盲型腔不可见：细虚线
                    hid = pk["face"] != v.type and not pk.get("through")
                    s.poly([V((x, y, 0.0)) for x, y in pk["profile"].outline], THIN, dash=HIDDEN if hid else None)
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
        elif asm is not None:
            for inst in asm.instances:
                (bu0, bu1), (bw0, bw1) = vrange(inst.world_bbox, ha), vrange(inst.world_bbox, va)
                x0, y0 = P(bu0, bw0)
                x1, y1 = P(bu1, bw1)
                s.rect(x0, y0, x1 - x0, y1 - y0, THICK if inst.source.kind in ("part", "assembly") else THIN)
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
            for inst in h.instances:
                c = V(inst.xyz)
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
                    s.circle(*c, h.nominal / 2 * sc, THIN, "1,0.8")
                if (h.cbore or h.csink) and h.face == v.type:  # 沉孔口只在孔口一侧可见
                    s.circle(*c, h.max_dia / 2 * sc, THIN)
                s.line(c[0] - h.max_dia * 0.7 * sc, c[1], c[0] + h.max_dia * 0.7 * sc, c[1], 0.18, "4,1,1,1")
                s.line(c[0], c[1] - h.max_dia * 0.7 * sc, c[0], c[1] + h.max_dia * 0.7 * sc, 0.18, "4,1,1,1")

        for sec_ in plan.sections:
            if sec_.kind == "local" and sec_.view == v.type:
                _draw_local(s, sec_, V)
                rendered_refs.append(f"section:{sec_.label}")
            elif sec_.parent == v.type and part is not None:
                _draw_cut_line(s, sec_, part, V)
        # 尺寸
        vx0, vy0 = P(ulo, wlo)
        vx1, vy1 = P(uhi, whi)
        ord_h = ord_v = 0
        has_ord_h = any(d.view == v.type and d.kind == "ordinate" and d.axis == ha for d in plan.dims)
        prec = [d.extra["normal"] for d in plan.dims if d.view == v.type and d.kind in ("datum", "gtol", "roughness")
                and d.extra and not d.extra.get("attach")]
        prec_below = any(tuple(n) == (0, -1) for n in prec)
        prec_left = any(tuple(n) == (-1, 0) for n in prec)
        for d in plan.dims:
            if d.view != v.type:
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
                s.line(vx0, yy, vx1, yy)
                s.line(vx0, vy0 - 2, vx0, yy - 1.5)
                s.line(vx1, vy0 - 2, vx1, yy - 1.5)
                s.text((vx0 + vx1) / 2, yy + 1, d.text, 3.5, "middle")
            elif d.kind == "linear" and d.axis == va and d.at is None:
                xx = vx0 - (22 if prec_left else 10)  # 纵向坐标尺寸画在右侧；左侧让开基准符号
                s.line(xx, vy0, xx, vy1)
                s.line(vx0 - 2, vy0, xx - 1.5, vy0)
                s.line(vx0 - 2, vy1, xx - 1.5, vy1)
                s.text(xx - 1, (vy0 + vy1) / 2, d.text, 3.5, "middle", -90)
            elif d.kind == "linear" and d.at is not None:
                px, py = P(*d.at)
                s.text(vx1 + 6, py, d.text, 3, "start")
            elif d.kind == "ordinate" and d.at is not None:
                px, py = P(*d.at)
                if d.axis == ha:
                    s.line(px, vy0 - 1, px, vy0 - 4 - (ord_h % 2) * 3, 0.18)
                    s.text(px, vy0 - 6.5 - (ord_h % 2) * 3, d.text, 2.5, "middle", -90)
                    ord_h += 1
                else:
                    s.line(vx1 + 1, py, vx1 + 4 + (ord_v % 2) * 3, py, 0.18)
                    s.text(vx1 + 5 + (ord_v % 2) * 3, py - 0.8, d.text, 2.5)
                    ord_v += 1
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
                s.parts.append(f"<circle cx='{lb['anchor'][0]:.2f}' cy='{s.y(lb['anchor'][1]):.2f}' r='0.5' fill='{STROKE}'/>")
                s.text(lb["text_at"][0], lb["text_at"][1], d.text, 3)
            elif d.kind in ("callout", "radius", "diameter"):
                px, py = P(*d.at) if d.at is not None else (vx1, vy1)
                tx, ty = px + 12, py + 9
                s.line(px, py, tx, ty, 0.18)
                s.line(tx, ty, tx + _text_w(d.text, 3), ty, 0.18)
                s.text(tx + 0.5, ty + 0.8, d.text, 3)
            rendered_refs.append(d.ref)

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
    for n in plan.notes:
        s.text(x0, ny, n, 3.5)
        ny -= 5
    if plan.notes:
        rendered_refs.append("notes")
    if plan.cut_list:
        _draw_cut_list(s, x0, y0 + tbh + 12 + 5 * len(plan.notes), plan.cut_list, cut_rows)
        rendered_refs.append("cut_list")
    # 明细表（标题栏上方）
    if plan.bom:  # GB/T 10609.2：表头在下，序号自下而上
        by = y0 + tbh + 12 + 5 * len(plan.notes)
        cols = [("序号", 12), ("图号/编码", 44), ("名称", 54), ("数量", 14), ("材料/规格", 56)]
        xx = x0
        for name, w in cols:
            s.rect(xx, by, w, 7, THIN)
            s.text(xx + 1.5, by + 2.2, name, 3)
            xx += w
        for r in sorted(plan.bom, key=lambda r: r["item"]):
            by += 7
            xx = x0
            for (name, w), val in zip(cols, [r["item"], r["part_no"], r["title"], r["qty"], r["material"] or r["spec"]]):
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
