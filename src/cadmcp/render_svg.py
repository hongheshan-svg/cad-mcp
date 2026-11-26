"""把 DrawingPlan 渲染为 SVG 预览（Mock 适配器的"工程图"，也用于 AI 目视自检）。

不是生产图纸——生产图纸由 SolidWorks/Creo 渲染。这里保证：方案里的每个尺寸/标注
都画出来，并回报 dim_refs 供 check_drawing 校验。
"""
from __future__ import annotations

import math
from html import escape
from typing import Any

from .detailing import VIEW_AXES, DrawingPlan
from .ir.assembly_resolve import ResolvedAssembly
from .ir.resolve import ResolvedPart

STROKE = "#111"
THIN = 0.25
THICK = 0.5
FONT = "font-family='Noto Sans CJK SC, PingFang SC, sans-serif'"


class _Svg:
    def __init__(self, W: float, H: float):
        self.W, self.H = W, H
        self.parts: list[str] = []

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
        rot = f" transform='rotate({rotate} {x:.2f} {self.y(y):.2f})'" if rotate else ""
        self.parts.append(f"<text x='{x:.2f}' y='{self.y(y):.2f}' font-size='{size}' text-anchor='{anchor}' "
                          f"{FONT}{rot}>{escape(s)}</text>")

    def render(self) -> str:
        return (f"<svg xmlns='http://www.w3.org/2000/svg' width='{self.W}mm' height='{self.H}mm' "
                f"viewBox='0 0 {self.W} {self.H}'><rect width='100%' height='100%' fill='white'/>"
                + "".join(self.parts) + "</svg>")


def _coord(pt3, axis):
    return pt3["xyz".index(axis)]


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

    for v in plan.views:
        sc = v.scale * (0.5 if v.type == "iso" else 1)
        cx, cy = v.origin_on_sheet
        s.text(cx, cy + v.size_mm[1] / 2 + 4, v.type.upper(), 3, "middle")
        if box is None or v.type in ("iso", "flat_pattern"):
            if v.type == "iso" and box is not None:
                sx, sy, sz = box.size
                def iso(x, y, z):
                    return (cx + (x - y) * 0.866 * sc - (sx - sy) * 0.433 * sc,
                            cy + ((x + y) * 0.5 + z) * sc - ((sx + sy) * 0.25 + sz / 2) * sc)
                boxes = [i.world_bbox for i in asm.instances] if asm is not None else [box]
                for bb in boxes:
                    x0, y0, z0 = bb.xmin - box.xmin, bb.ymin - box.ymin, bb.zmin - box.zmin
                    bx, by, bz = bb.size
                    P = {(i, j, k): iso(x0 + i * bx, y0 + j * by, z0 + k * bz) for i in (0, 1) for j in (0, 1) for k in (0, 1)}
                    for a, b in [((0,0,0),(1,0,0)),((1,0,0),(1,1,0)),((0,0,1),(1,0,1)),((1,0,1),(1,1,1)),((0,1,1),(1,1,1)),
                                 ((0,0,1),(0,1,1)),((0,0,0),(0,0,1)),((1,0,0),(1,0,1)),((1,1,0),(1,1,1))]:
                        s.line(*P[a], *P[b], THICK if asm is None else THIN)
            else:
                w, h = v.size_mm
                s.rect(cx - w / 2, cy - h / 2, w, h)
            continue
        ha, va = VIEW_AXES[v.type]
        lo = {"x": box.xmin, "y": box.ymin, "z": box.zmin}
        hi = {"x": box.xmax, "y": box.ymax, "z": box.zmax}
        mid = {a: (lo[a] + hi[a]) / 2 for a in "xyz"}

        def P(u, w_):
            return (cx + (u - mid[ha]) * sc, cy + (w_ - mid[va]) * sc)

        if part is not None and v.type in ("top", "bottom") and part.profile is not None:
            pr = part.profile
            if pr.shape == "circle":
                c = P(*pr.data["center"])
                s.circle(*c, pr.data["diameter"] / 2 * sc)
            else:
                s.poly([P(x, y) for x, y in pr.outline])
            for pk in part.pockets:
                if pk["face"] in ("top", "bottom"):
                    s.poly([P(x, y) for x, y in pk["profile"].outline], THIN)
        elif part is not None and part.base_type == "shaft" and v.type in ("front", "back", "left", "right"):
            z = 0.0
            for L, d in part.shaft_steps:
                x0, y0 = P(-d / 2, z)
                x1, y1 = P(d / 2, z + L)
                s.rect(x0, y0, x1 - x0, y1 - y0)
                z += L
            s.line(*P(0, box.zmin - 3), *P(0, box.zmax + 3), THIN, "6,1.5,1,1.5")
        elif asm is not None:
            for inst in asm.instances:
                bb = inst.world_bbox
                blo = {"x": bb.xmin, "y": bb.ymin, "z": bb.zmin}
                bhi = {"x": bb.xmax, "y": bb.ymax, "z": bb.zmax}
                x0, y0 = P(blo[ha], blo[va])
                x1, y1 = P(bhi[ha], bhi[va])
                s.rect(x0, y0, x1 - x0, y1 - y0, THICK if inst.source.kind in ("part", "assembly") else THIN)
        else:
            x0, y0 = P(lo[ha], lo[va])
            x1, y1 = P(hi[ha], hi[va])
            s.rect(x0, y0, x1 - x0, y1 - y0)
        # 序号气泡（主视图）
        if asm is not None and v.type == plan.views[0].type:
            placed = 0
            anchors = []
            for b_ in plan.balloons:
                inst = next(i for i in asm.instances if i.id == b_["instance"])
                bb = inst.world_bbox
                anchors.append((b_, P((getattr(bb, ha + "min") + getattr(bb, ha + "max")) / 2,
                                      (getattr(bb, va + "min") + getattr(bb, va + "max")) / 2)))
            anchors.sort(key=lambda a: -a[1][1])  # 自上而下排列，引线不交叉
            for b_, cpt in anchors:
                bx_ = cx + v.size_mm[0] / 2 + 15
                by_ = cy + v.size_mm[1] / 2 - 10 - placed * 12
                s.line(*cpt, bx_ - 4, by_, 0.18)
                s.parts.append(f"<circle cx='{cpt[0]:.2f}' cy='{s.y(cpt[1]):.2f}' r='0.7' fill='{STROKE}'/>")
                s.circle(bx_, by_, 4, THIN)
                s.text(bx_, by_ - 1.2, str(b_["item"]), 3.5, "middle")
                rendered_refs.append(f"balloon:{b_['item']}")
                placed += 1
        # 孔
        for h in ([] if part is None else part.holes):
            for inst in h.instances:
                p3 = inst.xyz
                if inst.face in ("top", "bottom") and v.type in ("top", "bottom") or \
                   inst.face in ("front", "back") and v.type in ("front", "back") or \
                   inst.face in ("left", "right") and v.type in ("left", "right"):
                    c = P(_coord(p3, ha), _coord(p3, va))
                    s.circle(*c, h.drill / 2 * sc)
                    if h.type == "tapped":
                        s.circle(*c, h.nominal / 2 * sc, THIN, "1,0.8")
                    if h.cbore or h.csink:
                        s.circle(*c, h.max_dia / 2 * sc, THIN)
                    s.line(c[0] - h.max_dia * 0.7 * sc, c[1], c[0] + h.max_dia * 0.7 * sc, c[1], 0.18, "4,1,1,1")
                    s.line(c[0], c[1] - h.max_dia * 0.7 * sc, c[0], c[1] + h.max_dia * 0.7 * sc, 0.18, "4,1,1,1")

        # 尺寸
        vx0, vy0 = P(lo[ha], lo[va])
        vx1, vy1 = P(hi[ha], hi[va])
        ord_h = ord_v = 0
        has_ord_h = any(d.view == v.type and d.kind == "ordinate" and d.axis == ha for d in plan.dims)
        has_ord_v = any(d.view == v.type and d.kind == "ordinate" and d.axis == va for d in plan.dims)
        for d in plan.dims:
            if d.view != v.type:
                continue
            if d.kind == "linear" and d.axis == ha and d.at is None:
                yy = vy0 - (22 if has_ord_h else 10)  # 坐标尺寸区之外
                s.line(vx0, yy, vx1, yy)
                s.line(vx0, vy0 - 2, vx0, yy - 1.5)
                s.line(vx1, vy0 - 2, vx1, yy - 1.5)
                s.text((vx0 + vx1) / 2, yy + 1, d.text, 3.5, "middle")
            elif d.kind == "linear" and d.axis == va and d.at is None:
                xx = vx0 - 10  # 纵向坐标尺寸画在右侧，左侧不冲突
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
            elif d.kind in ("callout", "radius", "diameter"):
                if d.at is not None:
                    px, py = P(*d.at)
                else:
                    px, py = vx1, vy1
                tx, ty = px + 12, py + 9
                s.line(px, py, tx, ty, 0.18)
                s.line(tx, ty, tx + len(d.text) * 1.9, ty, 0.18)
                s.text(tx + 0.5, ty + 0.8, d.text, 3)
            rendered_refs.append(d.ref)

    # 标题栏（右下）
    tbw, tbh = 180, 56
    x0, y0 = W - 5 - tbw, 5
    s.rect(x0, y0, tbw, tbh)
    tb = plan.title_block
    rows = [("名称", tb.get("name", "")), ("图号", tb.get("part_no", "")), ("材料", tb.get("material", "")),
            ("比例", tb.get("scale", "")), ("质量", tb.get("mass", "")), ("投影", tb.get("projection", "")),
            ("设计", tb.get("designer", "")), ("日期", tb.get("date", ""))]
    for i, (k, val) in enumerate(rows):
        c, r = i % 2, i // 2
        cx0, cy0 = x0 + c * 90, y0 + tbh - (r + 1) * 14
        s.rect(cx0, cy0, 90, 14, THIN)
        s.text(cx0 + 2, cy0 + 5, k, 3)
        s.text(cx0 + 18, cy0 + 5, val, 4 if k in ("名称", "图号") else 3.5)
    # 技术要求
    ny = y0 + tbh + 6 + 5 * len(plan.notes)
    for n in plan.notes:
        s.text(x0, ny, n, 3.5)
        ny -= 5
    if plan.notes:
        rendered_refs.append("notes")
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
    return s.render(), {"dim_refs": rendered_refs, "views": [v.type for v in plan.views]}
