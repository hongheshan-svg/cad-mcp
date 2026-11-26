"""把 DrawingPlan 渲染为 SVG 预览（Mock 适配器的"工程图"，也用于 AI 目视自检）。

不是生产图纸——生产图纸由 SolidWorks/Creo 渲染。这里保证：方案里的每个尺寸/标注
都画出来，并回报 dim_refs 供 check_drawing 校验。
"""
from __future__ import annotations

import math
from html import escape
from typing import Any

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

    def y(self, y: float) -> float:
        return self.H - y

    def line(self, x1, y1, x2, y2, w=THIN, dash=None):
        d = f" stroke-dasharray='{dash}'" if dash else ""
        self.parts.append(f"<line x1='{x1:.2f}' y1='{self.y(y1):.2f}' x2='{x2:.2f}' y2='{self.y(y2):.2f}' "
                          f"stroke='{STROKE}' stroke-width='{w}'{d}/>")

    def poly(self, pts, w=THICK, close=True, dash=None):
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


FACE_AXIS = {"top": "z", "bottom": "z", "front": "y", "back": "y", "left": "x", "right": "x"}


def _text_w(s: str, size: float) -> float:
    return sum(size if ord(ch) > 0x2E80 else 0.6 * size for ch in s)


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
    左侧焊脚 K，右侧处数；周围焊在转折处画小圆。"""
    from .render_gdt import _tri
    ex, ey = px + 10, py + 12
    s.line(px, py, ex, ey, THIN)
    a = math.atan2(py - ey, px - ex)
    _tri(s, [(px, py), (px - 2.2 * math.cos(a - 0.3), py - 2.2 * math.sin(a - 0.3)),
             (px - 2.2 * math.cos(a + 0.3), py - 2.2 * math.sin(a + 0.3))])
    L = 26
    s.line(ex, ey, ex + L, ey, THIN)
    s.line(ex, ey - 1.2, ex + L, ey - 1.2, THIN, "1.5,1")
    tx = ex + 9
    s.poly([(tx, ey), (tx + 3.5, ey), (tx, ey + 3.5)], THIN)  # 角焊缝 ◣
    s.text(ex + 1, ey + 0.8, d.text, 3)
    if d.extra and d.extra.get("count", 1) > 1:
        s.text(tx + 5, ey + 0.8, f"{d.extra['count']}处", 3)
    if d.extra and d.extra.get("all_around"):
        s.circle(ex, ey, 1.4, THIN)


def _draw_cut_list(s, x0, y_bottom, rows) -> float:
    """切割清单（标题栏上方，自下而上：表头在下）。返回表顶 y。"""
    cols = [("序号", 12), ("规格", 52), ("长度/尺寸", 36), ("数量", 14), ("备注", 66)]
    by = y_bottom
    for r in rows:
        size = f"L={r['length_mm']:g}" if r["kind"] == "member" else "×".join(f"{v:g}" for v in r["size_mm"])
        note = "两端平切 90°" if r["kind"] == "member" else ("焊后加工" if r.get("machined") else "")
        xx = x0
        for (_, w), val in zip(cols, [r["item"], r["spec"], size, r["qty"], note]):
            s.rect(xx, by, w, 7, THIN)
            s.text(xx + 1.5, by + 2.2, str(val), 3)
            xx += w
        by += 7
    xx = x0
    for name, w in cols:
        s.rect(xx, by, w, 7, THIN)
        s.text(xx + 1.5, by + 2.2, name, 3)
        xx += w
    s.text(x0, by + 9, "切割清单", 3.5)
    return by + 14


def _draw_iso(s, cx, cy, sc, box, part, asm):
    iso = _iso_xy(cx, cy, sc, box)
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
        P = {(i, j, k): iso(bb.xmin + i * (bb.xmax - bb.xmin), bb.ymin + j * (bb.ymax - bb.ymin),
                            bb.zmin + k * (bb.zmax - bb.zmin)) for i in (0, 1) for j in (0, 1) for k in (0, 1)}
        for e1, e2 in [((0,0,0),(1,0,0)),((1,0,0),(1,1,0)),((0,0,1),(1,0,1)),((1,0,1),(1,1,1)),((0,1,1),(1,1,1)),
                       ((0,0,1),(0,1,1)),((0,0,0),(0,0,1)),((1,0,0),(1,0,1)),((1,1,0),(1,1,1))]:
            s.line(*P[e1], *P[e2], THIN if asm is not None or len(boxes) > 1 else THICK)


def render(plan: DrawingPlan, model: ResolvedPart | ResolvedAssembly | None = None) -> tuple[str, dict[str, Any]]:
    W, H = plan.sheet_size
    s = _Svg(W, H)
    s.rect(5, 5, W - 10, H - 10, THICK)  # 图框
    rendered_refs: list[str] = []
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

        if part is not None and v.type in ("top", "bottom") and part.profile is not None:
            pr = part.profile
            if pr.shape == "circle":
                c3 = (*pr.data["center"], 0.0)
                s.circle(*V(c3), pr.data["diameter"] / 2 * sc)
            else:
                s.poly([V((x, y, 0.0)) for x, y in pr.outline])
            for pk in part.pockets:
                if pk["face"] in ("top", "bottom"):
                    s.poly([V((x, y, 0.0)) for x, y in pk["profile"].outline], THIN)
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
            for b_ in plan.balloons:
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
        # 孔（只在与孔所在面平行的视图中画实形）
        for h in ([] if part is None else part.holes):
            if FACE_AXIS[h.face] != FACE_AXIS.get(v.type):
                continue
            for inst in h.instances:
                c = V(inst.xyz)
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
                if h.cbore or h.csink:
                    s.circle(*c, h.max_dia / 2 * sc, THIN)
                s.line(c[0] - h.max_dia * 0.7 * sc, c[1], c[0] + h.max_dia * 0.7 * sc, c[1], 0.18, "4,1,1,1")
                s.line(c[0], c[1] - h.max_dia * 0.7 * sc, c[0], c[1] + h.max_dia * 0.7 * sc, 0.18, "4,1,1,1")

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
            elif d.kind in ("callout", "radius", "diameter"):
                px, py = P(*d.at) if d.at is not None else (vx1, vy1)
                tx, ty = px + 12, py + 9
                s.line(px, py, tx, ty, 0.18)
                s.line(tx, ty, tx + _text_w(d.text, 3), ty, 0.18)
                s.text(tx + 0.5, ty + 0.8, d.text, 3)
            rendered_refs.append(d.ref)

    if part is not None and plan.title_block.get("surface_finish"):
        from . import render_gdt
        render_gdt.general_roughness(s, W - 5 - 180, 5 + 56 + 14 + 5 * len(plan.notes) + 6
                                     + (7 * (len(plan.cut_list) + 1) + 16 if plan.cut_list else 0),
                                     plan.title_block["surface_finish"])
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
        cols = [("版本", 14), ("更改内容", 78), ("日期", 24), ("签名", 18)]
        rx = x0 - sum(w for _, w in cols)
        ry = y0
        for row in [*plan.revisions[::-1][:6], None]:
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
        _draw_cut_list(s, x0, y0 + tbh + 12 + 5 * len(plan.notes), plan.cut_list)
        rendered_refs.append("cut_list")
    # 明细表（标题栏上方）
    if plan.bom:
        by = y0 + tbh + 12 + 5 * len(plan.notes)
        cols = [("序号", 12), ("图号/编码", 44), ("名称", 54), ("数量", 14), ("材料/规格", 56)]
        for r in plan.bom:
            xx = x0
            for (name, w), val in zip(cols, [r["item"], r["part_no"], r["title"], r["qty"], r["material"] or r["spec"]]):
                s.rect(xx, by, w, 7, THIN)
                s.text(xx + 1.5, by + 2.2, str(val), 3)
                xx += w
            by += 7
        xx = x0
        for name, w in cols:
            s.rect(xx, by, w, 7, THIN)
            s.text(xx + 1.5, by + 2.2, name, 3)
            xx += w
    return s.render(), {"dim_refs": rendered_refs, "views": [v.type for v in plan.views],
                        "collisions": s.collisions()}
