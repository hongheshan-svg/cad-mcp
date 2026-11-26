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
    boxes = [i.world_bbox for i in asm.instances] if asm is not None else [box]
    for bb in boxes:
        P = {(i, j, k): iso(bb.xmin + i * (bb.xmax - bb.xmin), bb.ymin + j * (bb.ymax - bb.ymin),
                            bb.zmin + k * (bb.zmax - bb.zmin)) for i in (0, 1) for j in (0, 1) for k in (0, 1)}
        for e1, e2 in [((0,0,0),(1,0,0)),((1,0,0),(1,1,0)),((0,0,1),(1,0,1)),((1,0,1),(1,1,1)),((0,1,1),(1,1,1)),
                       ((0,0,1),(0,1,1)),((0,0,0),(0,0,1)),((1,0,0),(1,0,1)),((1,1,0),(1,1,1))]:
            s.line(*P[e1], *P[e2], THIN if asm is not None else THICK)


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
        if v.type == "flat_pattern":  # 基本视图按 GB 不标名称；展开图需注明
            s.text(cx, cy + v.size_mm[1] / 2 + 4, "展开图", 3.5, "middle")
        if box is None or v.type in ("iso", "flat_pattern"):
            if v.type == "iso" and box is not None:
                _draw_iso(s, cx, cy, sc, box, part, asm)
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
                (bu0, bu1), (bw0, bw1) = vrange(inst.world_bbox, ha), vrange(inst.world_bbox, va)
                x0, y0 = P(bu0, bw0)
                x1, y1 = P(bu1, bw1)
                s.rect(x0, y0, x1 - x0, y1 - y0, THICK if inst.source.kind in ("part", "assembly") else THIN)
        else:
            x0, y0 = P(ulo, wlo)
            x1, y1 = P(uhi, whi)
            s.rect(x0, y0, x1 - x0, y1 - y0)
        # 序号气泡（主视图）
        if asm is not None and v.type == plan.views[0].type:
            anchors = []
            for b_ in plan.balloons:
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
                px, py = P(*d.at) if d.at is not None else (vx1, vy1)
                tx, ty = px + 12, py + 9
                s.line(px, py, tx, ty, 0.18)
                s.line(tx, ty, tx + _text_w(d.text, 3), ty, 0.18)
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
    return s.render(), {"dim_refs": rendered_refs, "views": [v.type for v in plan.views],
                        "collisions": s.collisions()}
