"""轴类零件图 SVG 渲染：主视图轴线水平、键槽朝前，移出断面图，链式长度尺寸、带公差直径。"""
from __future__ import annotations

import math

from .detailing import DrawingPlan, shaft_dia_z
from .ir.resolve import ResolvedPart

THIN, THICK = 0.25, 0.5
CENTER = "6,1.5,1,1.5"


def _arrow(s, x, y, dx, dy, size=2.2):
    n = math.hypot(dx, dy) or 1
    ux, uy = dx / n, dy / n
    px, py = -uy, ux
    pts = [(x, y), (x - ux * size + px * size * 0.3, y - uy * size + py * size * 0.3),
           (x - ux * size - px * size * 0.3, y - uy * size - py * size * 0.3)]
    s.parts.append("<polygon points='" + " ".join(f"{a:.2f},{s.y(b):.2f}" for a, b in pts) + "' fill='#111'/>")


def _hdim(s, xa, xb, y_from_a, y_from_b, y, text):
    """水平尺寸：两条尺寸界线 + 双箭头尺寸线 + 居中文字。"""
    for x, yf in ((xa, y_from_a), (xb, y_from_b)):
        s.line(x, yf + (1 if y > yf else -1), x, y + (1.5 if y > yf else -1.5), THIN)
    s.line(xa, y, xb, y, THIN)
    _arrow(s, xa, y, -1, 0)
    _arrow(s, xb, y, 1, 0)
    s.text((xa + xb) / 2, y + 0.8, text, 3.5, "middle")


def _vdim(s, x, ya, yb, text, text_left=True):
    """竖直直径尺寸。文字长于尺寸线（短小轴段上的螺纹代号等）时，尺寸线向外延长，文字放在轮廓之外。"""
    from .render_svg import _text_w
    tw = _text_w(text, 3.5)
    ym = (ya + yb) / 2
    if tw > yb - ya - 2:
        s.line(x, yb, x, yb + tw + 3, THIN)
        ym = yb + 2 + tw / 2
    s.line(x, ya, x, yb, THIN)
    _arrow(s, x, ya, 0, -1)
    _arrow(s, x, yb, 0, 1)
    s.text(x - 1 if text_left else x + 4.5, ym, text, 3.5, "middle", -90)


def _profile(part: ResolvedPart) -> list[tuple[float, float, float]]:
    """沿轴向的半径分段 [(z0, z1, r)]，已扣除环槽。"""
    segs: list[tuple[float, float, float]] = []
    z = 0.0
    for L, d in part.shaft_steps:
        segs.append((z, z + L, d / 2))
        z += L
    for g in (f for f in part.shaft_features if f["kind"] == "groove"):
        out = []
        for a, b, r in segs:
            if b <= g["z0"] or a >= g["z1"]:
                out.append((a, b, r))
                continue
            if a < g["z0"]:
                out.append((a, g["z0"], r))
            out.append((max(a, g["z0"]), min(b, g["z1"]), g["d2"] / 2))
            if b > g["z1"]:
                out.append((g["z1"], b, r))
        segs = out
    return segs


def render_shaft(s, plan: DrawingPlan, part: ResolvedPart, refs: list[str]) -> None:
    main = plan.views[0]
    sc = main.scale
    Ltot = part.bbox.zmax - part.bbox.zmin
    cx, cy = main.origin_on_sheet
    xl = cx - Ltot * sc / 2

    def P(z, r):
        return xl + z * sc, cy + r * sc

    segs = _profile(part)
    c = part.shaft_chamfer
    # 外形：上下轮廓线 + 轴肩/槽边竖线
    for sign in (1, -1):
        pts = [P(0, 0), P(0, sign * (segs[0][2] - c))]
        if c:
            pts.append(P(c, sign * segs[0][2]))
        for a, b, r in segs:
            pts += [P(a if not (c and a == 0) else c, sign * r), P(b if not (c and b == Ltot) else Ltot - c, sign * r)]
        if c:
            pts.append(P(Ltot, sign * (segs[-1][2] - c)))
        pts.append(P(Ltot, 0))
        s.poly(pts, THICK, close=False)
    for (a, b, r), (a2, b2, r2) in zip(segs, segs[1:]):
        if abs(r - r2) > 1e-9:
            rm = max(r, r2)
            s.line(*P(b, -rm), *P(b, rm), THICK)
    if c:
        for z, r in ((c, segs[0][2]), (Ltot - c, segs[-1][2])):
            s.line(*P(z, -r), *P(z, r), THICK)
    s.line(*P(-3, 0), *P(Ltot + 3, 0), THIN, CENTER)

    # 外螺纹（GB/T 4459.1）：大径粗实线（即轮廓），小径细实线画到螺纹终止处（倒角内也画），终止线粗实线
    for f in (f for f in part.shaft_features if f["kind"] == "thread"):
        r1 = f["d1"] / 2
        s.line(*P(f["z0"], r1), *P(f["z1"], r1), THIN)
        s.line(*P(f["z0"], -r1), *P(f["z1"], -r1), THIN)
        zt = f["z0"] if f["side"] == "end" else f["z1"]  # 终止线在螺纹靠轴内的一端
        if 1e-6 < zt < Ltot - 1e-6:
            s.line(*P(zt, -f["d"] / 2), *P(zt, f["d"] / 2), THICK)
    # 键槽（朝前，主视图显示其实形）
    kws = [f for f in part.shaft_features if f["kind"] == "keyway"]
    for f in kws:
        rad = f["width"] / 2
        za, zb = f["z0"] + rad, f["z1"] - rad
        pts = [P(zb + rad * math.cos(math.radians(a)), rad * math.sin(math.radians(a))) for a in range(-90, 91, 15)]
        pts += [P(za + rad * math.cos(math.radians(a)), rad * math.sin(math.radians(a))) for a in range(90, 271, 15)]
        s.poly(pts, THICK)

    R = max(d for _, d in part.shaft_steps) / 2
    y_top, y_bot = cy + R * sc, cy - R * sc
    r_at = {}

    def radius_at(z):
        for a, b, r in segs:
            if a - 1e-9 <= z <= b + 1e-9:
                return r
        return 0.0

    for d in plan.dims:
        if d.view != "front":
            continue
        if d.kind == "linear" and d.at is not None:
            za, zb = d.at
            xa, xb = P(za, 0)[0], P(zb, 0)[0]
            if d.ref == "overall_z":
                y = y_bot - 26
                _hdim(s, xa, xb, cy - radius_at(za) * sc, cy - radius_at(zb) * sc, y, d.text)
            elif d.ref.startswith("step"):
                y = y_bot - 15
                _hdim(s, xa, xb, cy - radius_at(za + 1e-6) * sc, cy - radius_at(zb - 1e-6) * sc, y, d.text)
            else:  # 键槽、环槽定位：画在主视图上方
                y = y_top + 14
                _hdim(s, xa, xb, cy + radius_at(za) * sc, cy + radius_at(zb) * sc, y, d.text)
        elif d.kind == "diameter" and d.at is not None:
            zm, dia = d.at
            i = int(d.ref[4:].split("_")[0])
            zz = shaft_dia_z(part, i)
            x = P(zz, 0)[0]
            _vdim(s, x, cy - dia / 2 * sc, cy + dia / 2 * sc, d.text)
        elif d.kind in ("datum", "gtol", "roughness"):
            from . import render_gdt
            z, r = d.at
            px, py = P(z, r)
            if d.kind == "gtol":  # 轴上框格放到键槽尺寸行之上
                render_gdt.fcf(s, px, py, (0, 1), d.extra["cells"], reach=y_top + 22 - py)
            else:
                render_gdt.draw(s, d.kind, px, py, d.extra, d.text)
        elif d.kind == "callout" and d.at is not None:
            z, r = d.at
            px, py = P(z, r)
            tx, ty = px + 6, y_top + 4 + (14 if d.ref != "chamfer" else 4)
            if d.ref == "chamfer":
                tx = px - 4 - len(d.text) * 2
            s.line(px, py, tx, ty, THIN)
            s.line(tx, ty, tx + len(d.text) * 2.4, ty, THIN)
            s.text(tx + 0.5, ty + 0.8, d.text, 3)
        refs.append(d.ref)

    # 断面图
    for v in plan.views[1:]:
        f = next(k for k in kws if k["name"] == v.target)
        vx, vy = v.origin_on_sheet
        r = f["d"] / 2 * sc
        hb, t1 = f["width"] / 2 * sc, f["depth"] * sc
        a0 = math.asin(min(1.0, hb / r))
        outline = [(vx + r * math.sin(a), vy + r * math.cos(a)) for a in
                   [a0 + (2 * math.pi - 2 * a0) * k / 72 for k in range(73)]]
        outline += [(vx - hb, vy + r - t1), (vx + hb, vy + r - t1)]
        outline = [(vx + hb, vy + r * math.cos(a0))] + outline
        cid = f"sec{v.label[0]}"
        path = " ".join(f"{x:.2f},{s.y(y):.2f}" for x, y in outline)
        s.parts.append(f"<clipPath id='{cid}'><polygon points='{path}'/></clipPath>")
        hatch = []
        k = -2 * r
        while k < 2 * r:
            hatch.append(f"<line x1='{vx + k - r:.2f}' y1='{s.y(vy - r):.2f}' x2='{vx + k + r:.2f}' "
                         f"y2='{s.y(vy + r):.2f}' stroke='#111' stroke-width='0.18'/>")
            k += 2.5
        s.parts.append(f"<g clip-path='url(#{cid})'>{''.join(hatch)}</g>")
        s.poly(outline, THICK)
        s.line(vx - r - 3, vy, vx + r + 3, vy, THIN, CENTER)
        s.line(vx, vy - r - 3, vx, vy + r + 3, THIN, CENTER)
        s.text(vx, vy + r + 12, v.label, 4, "middle")
        # 剖切位置符号（主视图上下两端短粗线 + 字母）
        zc = (f["z0"] + f["z1"]) / 2
        mx = P(zc, 0)[0]
        for yy, sign in ((y_top + 3, 1), (y_bot - 3, -1)):
            s.line(mx, yy, mx, yy + sign * 5, 0.7)
            s.text(mx + 1.5, yy + (sign * 2 if sign > 0 else -4.5), v.label[0], 3.5)
        for d in plan.dims:
            if d.view != "section" or not d.ref.startswith(f["name"] + "_"):
                continue
            if d.ref.endswith("_w"):
                _hdim(s, vx - hb, vx + hb, vy + r - t1, vy + r - t1, vy + r + 5, d.text)
            else:
                _vdim(s, vx + r + 6, vy - r, vy + r - t1, d.text, text_left=False)
                s.line(vx + hb, vy + r - t1, vx + r + 7.5, vy + r - t1, THIN)
                s.line(vx, vy - r, vx + r + 7.5, vy - r, THIN)
            refs.append(d.ref)
