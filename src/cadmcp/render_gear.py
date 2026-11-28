"""直齿圆柱齿轮零件图 SVG 渲染（GB/T 4459.2）：主视图过轴线全剖、轮齿不剖；左视图三圆 + 轴孔键槽；参数表。"""
from __future__ import annotations

import math

from .gear_drawing import gear_parts, param_table
from .detailing import DrawingPlan
from .ir.resolve import ResolvedPart
from .render_shaft import _arrow, _hdim
from .render_svg import _text_w

THIN, THICK = 0.25, 0.5
CENTER = "6,1.5,1,1.5"


def _hatch(s, cid, poly, x0, y0, x1, y1, step=2.5):
    path = " ".join(f"{x:.2f},{s.y(y):.2f}" for x, y in poly)
    s.parts.append(f"<clipPath id='{cid}'><polygon points='{path}'/></clipPath>")
    lines = []
    span = (x1 - x0) + (y1 - y0)
    k = -span
    while k < span:
        lines.append(f"<line x1='{x0 + k:.2f}' y1='{s.y(y0):.2f}' x2='{x0 + k + (y1 - y0):.2f}' "
                     f"y2='{s.y(y1):.2f}' stroke='#111' stroke-width='0.18'/>")
        k += step
    s.parts.append(f"<g clip-path='url(#{cid})'>{''.join(lines)}</g>")


def _vdim_text(s, x_line, x_from, ya, yb, text):
    """竖直尺寸：尺寸界线从视图边线引到 x_line，双箭头，文字在线左侧竖写。"""
    for y in (ya, yb):
        s.line(x_from - 1, y, x_line - 1.5, y, THIN)
    s.line(x_line, ya, x_line, yb, THIN)
    _arrow(s, x_line, ya, 0, -1)
    _arrow(s, x_line, yb, 0, 1)
    s.text(x_line - 1, (ya + yb) / 2, text, 3.5, "middle", -90)


def render_gear(s, plan: DrawingPlan, part: ResolvedPart, refs: list[str]) -> None:
    from . import render_gdt
    g = part.gear
    main, end = plan.views[0], plan.views[1]
    sc = main.scale
    b = part.bbox.zmax - part.bbox.zmin
    cx, cy = main.origin_on_sheet
    xl, xr = cx - b * sc / 2, cx + b * sc / 2
    ra, r, rf = g["da"] / 2 * sc, g["d"] / 2 * sc, g["df"] / 2 * sc
    parts = gear_parts(part)
    bore, kw = parts["bore"], parts["keyway"]
    rb = bore.max_dia / 2 * sc if bore is not None else 0.0
    ytop_hole = cy + (kw["top"] * sc if kw else rb)  # 剖切面过键槽：上半孔壁在键槽底
    ybot_hole = cy - rb

    # ---- 主视图：全剖，轮齿不剖 ----
    if bore is not None:
        up = [(xl, cy + rf), (xr, cy + rf), (xr, ytop_hole), (xl, ytop_hole)]
        dn = [(xl, ybot_hole), (xr, ybot_hole), (xr, cy - rf), (xl, cy - rf)]
        _hatch(s, "gearU", up, xl, ytop_hole, xr, cy + rf)
        _hatch(s, "gearD", dn, xl, cy - rf, xr, ybot_hole)
        s.line(xl, ytop_hole, xr, ytop_hole, THICK)
        s.line(xl, ybot_hole, xr, ybot_hole, THICK)
        if kw:  # 剖切面之后键槽侧壁与孔壁的交线（可见）
            hw = kw["width"] / 2 * sc
            yk0 = cy + math.sqrt(max(rb * rb - hw * hw, 0.0))
            s.line(xl, yk0, xr, yk0, THICK)
    s.line(xl, cy + ra, xr, cy + ra, THICK)                   # 齿顶线
    s.line(xl, cy - ra, xr, cy - ra, THICK)
    for x in (xl, xr):                                        # 端面：剖视中只画有材料处（孔里没有线）
        if bore is not None:
            s.line(x, ytop_hole, x, cy + ra, THICK)
            s.line(x, cy - ra, x, ybot_hole, THICK)
        else:
            s.line(x, cy - ra, x, cy + ra, THICK)
    s.line(xl, cy + rf, xr, cy + rf, THICK)                   # 齿根线（剖视中粗实线）
    s.line(xl, cy - rf, xr, cy - rf, THICK)
    for sign in (1, -1):                                      # 分度线：细点画线，超出端面 2–3 mm
        s.line(xl - 3, cy + sign * r, xr + 3, cy + sign * r, THIN, CENTER)
    s.line(xl - 4, cy, xr + 4, cy, THIN, CENTER)              # 轴线

    # ---- 左视图 ----
    ex, ey = end.origin_on_sheet
    s.circle(ex, ey, ra, THICK)
    s.circle(ex, ey, r, THIN, CENTER)
    s.circle(ex, ey, rf, THIN)
    s.line(ex - ra - 4, ey, ex + ra + 4, ey, THIN, CENTER)
    s.line(ex, ey - ra - 4, ex, ey + ra + 4, THIN, CENTER)
    if bore is not None:
        if kw:
            hw = kw["width"] / 2 * sc
            a0 = math.asin(min(1.0, hw / rb))
            pts = [(ex + rb * math.sin(a), ey + rb * math.cos(a))
                   for a in [a0 + (2 * math.pi - 2 * a0) * k / 96 for k in range(97)]]
            yk = ey + kw["top"] * sc
            pts += [(ex - hw, yk), (ex + hw, yk)]
            s.poly(pts, THICK)
        else:
            s.circle(ex, ey, rb, THICK)
    for h in parts["others"]:  # 腹板孔/均布孔：孔圆（沉孔加孔口圆）；均布孔画分布圆与径向中心线，其余画十字中心线
        c = getattr(h, "circular", None)
        if c and len(h.instances) > 1:
            s.circle(ex, ey, c["pcd"] / 2 * sc, THIN, CENTER)
        for inst in h.instances:
            hx, hy = ex + inst.xyz[0] * sc, ey + inst.xyz[1] * sc
            s.circle(hx, hy, h.max_dia / 2 * sc, THICK)
            if h.drill < h.max_dia - 1e-6:
                s.circle(hx, hy, h.drill / 2 * sc, THICK)
            e_ = h.max_dia / 2 * sc + 2
            if c and len(h.instances) > 1:
                L_ = math.hypot(hx - ex, hy - ey) or 1.0
                ux, uy = (hx - ex) / L_, (hy - ey) / L_
                s.line(hx - ux * e_, hy - uy * e_, hx + ux * e_, hy + uy * e_, THIN, CENTER)
            else:
                s.line(hx - e_, hy, hx + e_, hy, THIN, CENTER)
                s.line(hx, hy - e_, hx, hy + e_, THIN, CENTER)

    # ---- 尺寸与精度符号 ----
    for d in plan.dims:
        ex_ = d.extra or {}
        if d.kind == "diameter" and "sheet_pts" in ex_:
            (xf, ya), (_, yb) = ex_["sheet_pts"]
            _vdim_text(s, ex_["sheet_pos"][0], xf, ya, yb, d.text)
        elif d.kind == "linear" and ex_.get("orient") in ("h", "v") and "sheet_pts" in ex_:
            draw_sheet_linear(s, d)
        elif d.kind == "roughness" and "sheet_at" in ex_:
            if ex_.get("ext_pts"):  # 轮廓线的延长线（细实线），符号尖端在其上
                (xa_, ya_), (xb_, yb_) = ex_["ext_pts"]
                s.line(xa_, ya_, xb_, yb_, THIN)
            render_gdt.draw(s, "roughness", *ex_["sheet_at"], ex_, d.text)
        elif d.kind == "datum" and "sheet_at" in ex_:
            render_gdt.datum(s, *ex_["sheet_at"], tuple(ex_["normal"]), d.text, ex_.get("reach", 7.0))
        elif d.kind == "gtol" and ex_.get("elbow_h"):
            from .detailing import draw_elbow_fcf
            draw_elbow_fcf(s, ex_)
        elif d.kind == "gtol" and "sheet_at" in ex_:
            render_gdt.fcf(s, *ex_["sheet_at"], tuple(ex_["normal"]), ex_["cells"], reach=ex_.get("reach", 10.0))
        elif d.kind == "callout" and ex_.get("label"):
            lb = ex_["label"]
            ax, ay = lb["anchor"]
            exb, eyb = lb["elbow"]
            s.line(ax, ay, exb, eyb, THIN)
            s.line(exb, eyb, lb["end"][0], lb["end"][1], THIN)
            if lb.get("dot"):  # 指在中心线（分布圆）上：圆点
                s.parts.append(f"<circle cx='{ax:.2f}' cy='{s.y(ay):.2f}' r='0.6' fill='black'/>")
            else:
                _arrow(s, ax, ay, ax - exb, ay - eyb)
            s.text(*lb["text_at"], d.text, 3.5)
        else:
            continue
        refs.append(d.ref)

    draw_param_table(s, plan.gear_table or param_table(plan, part))
    refs.append("gear_table")


def draw_sheet_linear(s, d) -> None:
    """按规划层图纸坐标画线性尺寸（extra: sheet_pts / sheet_pos / orient h|v）；齿轮图与轴类端视图共用。"""
    ex_ = d.extra or {}
    if ex_.get("orient") == "h":
        (xa, ya), (xb, yb) = ex_["sheet_pts"]
        px, py = ex_["sheet_pos"]
        if abs(px - (xa + xb) / 2) < 0.5:
            _hdim(s, xa, xb, ya, yb, py, d.text)
        else:  # 文字偏到一侧：尺寸线延长到文字下方
            tw = _text_w(d.text, 3.5)
            for x, yf in ((xa, ya), (xb, yb)):
                sg = 1 if py > yf else -1  # 尺寸线在被注点上方 / 下方
                s.line(x, yf + sg, x, py + sg * 1.5, THIN)
            s.line(min(xa, px - tw / 2), py, max(xb, px + tw / 2), py, THIN)
            _arrow(s, xa, py, 1, 0)
            _arrow(s, xb, py, -1, 0)
            s.text(px, py + 0.8, d.text, 3.5, "middle")
    else:
        (xa, ya), (xb, yb) = ex_["sheet_pts"]
        xl_ = ex_["sheet_pos"][0]
        s.line(xa + 1, ya, xl_ + 1.5, ya, THIN)
        s.line(xb + 1, yb, xl_ + 1.5, yb, THIN)
        s.line(xl_, ya, xl_, max(yb, ex_["sheet_pos"][1] + _text_w(d.text, 3.5) / 2 + 1), THIN)
        _arrow(s, xl_, ya, 0, -1)
        _arrow(s, xl_, yb, 0, 1)
        ty = ex_["sheet_pos"][1]
        s.text(xl_ - 1, ty, d.text, 3.5, "middle", -90)


def draw_param_table(s, t) -> None:
    """齿轮参数表（右上角，表头在上）。"""
    x0, ytop = t["origin"]
    rh = t["row_height"]
    widths = [w for _, w in t["columns"]]
    rows = [tuple(c for c, _ in t["columns"])] + t["rows"]
    tw, th = t["size"]
    s.rect(x0, ytop - th, tw, th, THICK)
    for i, row in enumerate(rows):
        y = ytop - (i + 1) * rh
        if i < len(rows) - 1:
            s.line(x0, y, x0 + tw, y, THIN)
        x = x0
        for j, (w, c) in enumerate(zip(widths, row)):
            if j:
                s.line(x, ytop, x, ytop - th, THIN)
            s.text(x + w / 2, y + 2.2, str(c), 3.0 if len(str(c)) < 14 else 2.5, "middle")
            x += w
