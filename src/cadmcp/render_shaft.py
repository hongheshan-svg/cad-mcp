"""轴类零件图 SVG 渲染：主视图轴线水平、键槽朝前，移出断面图，链式长度尺寸、带公差直径。"""
from __future__ import annotations

import math

from .detailing import DrawingPlan
from .ir.resolve import ResolvedPart

THIN, THICK = 0.25, 0.5
CENTER = "6,1.5,1,1.5"
HIDDEN = "3,1"  # 细虚线（与 render_svg.HIDDEN 一致）


def _arrow(s, x, y, dx, dy, size=2.2):
    n = math.hypot(dx, dy) or 1
    ux, uy = dx / n, dy / n
    px, py = -uy, ux
    pts = [(x, y), (x - ux * size + px * size * 0.3, y - uy * size + py * size * 0.3),
           (x - ux * size - px * size * 0.3, y - uy * size - py * size * 0.3)]
    s.parts.append("<polygon points='" + " ".join(f"{a:.2f},{s.y(b):.2f}" for a, b in pts) + "' fill='#111'/>")


def _hdim(s, xa, xb, y_from_a, y_from_b, y, text, busy=None, text_x=None):
    """水平尺寸：两条尺寸界线 + 双箭头尺寸线 + 居中文字。文字比两界线间距宽时移到界线之外（默认右侧；
    右侧落在同一行别的尺寸 busy=[(x0, x1)] 上时改到左侧），尺寸线延长到文字下。"""
    from .render_svg import _text_w
    for x, yf in ((xa, y_from_a), (xb, y_from_b)):
        s.line(x, yf + (1 if y > yf else -1), x, y + (1.5 if y > yf else -1.5), THIN)
    tw = _text_w(text, 3.5)
    if tw > abs(xb - xa) - 1:
        x0, x1 = min(xa, xb), max(xa, xb)
        hit_r = any(a < x1 + 1.5 + tw + 1 and b > x1 + 0.5 for a, b in busy or [])
        hit_l = any(a < x0 - 0.5 and b > x0 - 1.5 - tw - 1 for a, b in busy or [])
        if hit_r and not hit_l:
            s.line(x0 - 1.5 - tw, y, x1, y, THIN)
            s.text(x0 - 1.5 - tw, y + 0.8, text, 3.5)
        else:
            s.line(x0, y, x1 + 1.5 + tw, y, THIN)
            s.text(x1 + 1.5, y + 0.8, text, 3.5)
    else:
        tx_ = (xa + xb) / 2 if text_x is None else text_x
        lo_, hi_ = min(xa, xb, tx_ - tw / 2), max(xa, xb, tx_ + tw / 2)  # 数字移到界线外时尺寸线延长到数字下
        s.line(lo_, y, hi_, y, THIN)
        s.text(tx_, y + 0.8, text, 3.5, "middle")
    _arrow(s, xa, y, -1, 0)
    _arrow(s, xb, y, 1, 0)


def _row_text_spans(plan, d, P, same_row) -> list[tuple[float, float]]:
    """同一行其它水平尺寸的文字占位（x 区间）：放得下的居中，放不下的按区间整体计（保守）。"""
    from .render_svg import _text_w
    out = []
    for d2 in plan.dims:
        if d2 is d or d2.view != d.view or d2.kind != "linear" or d2.at is None or not same_row(d2):
            continue
        a, b = sorted((P(d2.at[0], 0)[0], P(d2.at[1], 0)[0]))
        tw = _text_w(d2.text, 3.5)
        out.append(((a + b) / 2 - tw / 2, (a + b) / 2 + tw / 2) if tw <= b - a - 1 else (a, b))
        out += [(a - 0.3, a + 0.3), (b - 0.3, b + 0.3)]  # 尺寸界线
    return out


def _vdim(s, x, ya, yb, text, text_left=True, overflow="up", room=None):
    """竖直直径尺寸。文字长于尺寸线（短小轴段上的螺纹代号等）时，尺寸线向外延长，文字放在轮廓之外
    （overflow = up | down，由规划层 shaft_dia_overflow 决定）。"""
    from .render_svg import _text_w
    tw = _text_w(text, 3.5)
    ym = (ya + yb) / 2
    if tw > (yb - ya if room is None else room) - 2:
        if overflow == "down":
            s.line(x, ya, x, ya - tw - 3, THIN)
            ym = ya - 2 - tw / 2
        else:
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


def _hollow_section(s, part, P, segs, c, Ltot):
    """空心轴/套类主视图全剖（过轴线）：剖面线在外轮廓与孔之间；轮廓 = 外形 + 孔壁 + 孔底/内台阶线（贯穿孔径）；
    端面、轴肩线只画有材料的部分；外圆上的倒角棱线在剖视中不可见。"""
    from .detailing import bore_radius_at
    zs = sorted({0.0, Ltot, *(b[k] for b in part.shaft_bores for k in ("z0", "z1")), *(x for a, b, _ in segs for x in (a, b))})
    pieces = []  # (za, zb, rb)
    for za, zb in zip(zs, zs[1:]):
        if zb - za > 1e-9:
            pieces.append((za, zb, bore_radius_at(part, (za + zb) / 2)))

    def r_out(z):
        return radius_at_seg(segs, z)
    kws = [f for f in part.shaft_features if f["kind"] == "keyway"]  # 空心轴键槽在上方：剖切面过键槽 → 上轮廓降到槽底
    segs_top = segs
    for f in kws:
        out_ = []
        for a, b, r in segs_top:
            if b <= f["z0"] + 1e-9 or a >= f["z1"] - 1e-9:
                out_.append((a, b, r))
                continue
            if a < f["z0"]:
                out_.append((a, f["z0"], r))
            out_.append((max(a, f["z0"]), min(b, f["z1"]), f["d"] / 2 - f["depth"]))
            if b > f["z1"]:
                out_.append((f["z1"], b, r))
        segs_top = out_
    for sign in (1, -1):
        segs_s = segs_top if sign > 0 else segs
        # 外轮廓（含倒角）
        pts = [P(0, sign * max(segs[0][2] - c, pieces[0][2]))]
        if c:
            pts.append(P(c, sign * segs[0][2]))
        for a, b, r in segs_s:
            pts += [P(a if not (c and a == 0) else c, sign * r), P(b if not (c and b == Ltot) else Ltot - c, sign * r)]
        if c:
            pts.append(P(Ltot, sign * max(segs[-1][2] - c, pieces[-1][2])))
        if sign > 0:  # 剖切面之后：键槽侧壁与外圆的交线（可见）
            for f in kws:
                yw = math.sqrt(max((f["d"] / 2) ** 2 - (f["width"] / 2) ** 2, 0.0))
                s.line(*P(f["z0"], yw), *P(f["z1"], yw), THICK)
        # 剖面区域 = 外轮廓 + 沿孔壁返回（上半：轮毂键槽处孔壁升到槽底）
        def rin(za, zb, rb):
            if sign > 0:
                kb = [b for b in part.shaft_bores if b.get("kw") and b["z0"] - 1e-9 <= (za + zb) / 2 <= b["z1"] + 1e-9
                      and abs(b["d"] / 2 - rb) < 1e-9]
                if kb:
                    return kb[0]["kw"]["top"]
            return rb
        back = []
        for za, zb, rb in reversed(pieces):
            back += [P(zb, sign * rin(za, zb, rb)), P(za, sign * rin(za, zb, rb))]
        poly = pts + back
        cid = f"hollow{'U' if sign > 0 else 'D'}"
        path = " ".join(f"{x:.2f},{s.y(y):.2f}" for x, y in poly)
        s.parts.append(f"<clipPath id='{cid}'><polygon points='{path}'/></clipPath>")
        xs = [x for x, _ in poly]
        ys = [y for _, y in poly]
        x0, x1, y0, y1 = min(xs), max(xs), min(ys), max(ys)
        hatch, k = [], -(y1 - y0)
        while k < (x1 - x0):
            hatch.append(f"<line x1='{x0 + k:.2f}' y1='{s.y(y0):.2f}' x2='{x0 + k + (y1 - y0):.2f}' y2='{s.y(y1):.2f}' "
                         f"stroke='#111' stroke-width='0.18'/>")
            k += 2.5
        s.parts.append(f"<g clip-path='url(#{cid})'>{''.join(hatch)}</g>")
        s.poly(pts, THICK, close=False)
        # 孔壁（键槽处为槽底线）；键槽侧壁与孔壁的交线（剖切面之后，可见）
        for za, zb, rb in pieces:
            if rb > 0:
                ri = rin(za, zb, rb)
                s.line(*P(za, sign * ri), *P(zb, sign * ri), THICK)
                if ri != rb:
                    hw = next(b["kw"]["width"] / 2 for b in part.shaft_bores if b.get("kw") and abs(b["d"] / 2 - rb) < 1e-9)
                    yk0 = math.sqrt(max(rb * rb - hw * hw, 0.0))
                    s.line(*P(za, yk0), *P(zb, yk0), THICK)
        # 端面：孔壁到外轮廓（倒角起点）
        for z, rb, r in ((0.0, rin(0.0, 1e-6, pieces[0][2]), segs[0][2] - c),
                         (Ltot, rin(Ltot - 1e-6, Ltot, pieces[-1][2]), segs[-1][2] - c)):
            if r > rb + 1e-9:
                s.line(*P(z, sign * rb), *P(z, sign * r), THICK)
        # 外轴肩：小径到大径
        for (a, b, r), (a2, b2, r2) in zip(segs, segs[1:]):
            if abs(r - r2) > 1e-9:
                lo = max(min(r, r2), bore_radius_at(part, b - 1e-6), bore_radius_at(part, b + 1e-6))
                s.line(*P(b, sign * lo), *P(b, sign * max(r, r2)), THICK)
    # 孔底 / 内台阶：贯穿孔径（透过孔看到的台阶面积聚成线）
    for (za, zb, rb), (za2, zb2, rb2) in zip(pieces, pieces[1:]):
        if abs(rb - rb2) > 1e-9:
            m = max(rb, rb2)
            s.line(*P(zb, -m), *P(zb, m), THICK)
    # 法兰均布孔（GB/T 4458.1 规定画法：旋转到剖切平面上，上下各画一个）：孔处无剖面线、端面线断开，孔壁粗实线，孔轴线细点画线
    for fh in part.flange_holes:
        lo, hi = fh["pcd"] / 2 - fh["d"] / 2, fh["pcd"] / 2 + fh["d"] / 2
        for sign in (1, -1):
            (xa, ya), (xb, yb) = P(fh["z0"], sign * lo), P(fh["z1"], sign * hi)
            y_lo, y_hi = min(ya, yb), max(ya, yb)
            s.parts.append(f"<rect x='{xa - 0.4:.2f}' y='{s.y(y_hi) + 0.3:.2f}' width='{xb - xa + 0.8:.2f}' "
                           f"height='{y_hi - y_lo - 0.6:.2f}' fill='white' stroke='none'/>")
            for r_ in (lo, hi):
                s.line(*P(fh["z0"], sign * r_), *P(fh["z1"], sign * r_), THICK)
            (xc0, yc), (xc1, _) = P(fh["z0"], sign * fh["pcd"] / 2), P(fh["z1"], sign * fh["pcd"] / 2)
            s.line(xc0 - 2, yc, xc1 + 2, yc, THIN, CENTER)


def _vertical_cross_hole(s, part, main, P, h, sect, segs) -> None:
    """主视图上竖直的径向横孔（GB/T 4458.6 局部剖）：
    实心轴 —— 局部剖区域内画剖面线（孔内不画），两侧（盲孔还有下边）细波浪线为界；孔壁粗实线，盲孔 118° 钻尖；
    螺孔：小径（底孔）粗实线、大径细实线到螺纹深、终止线粗实线，剖面线画到小径（GB/T 4459.1）。
    套类（主视图已全剖）—— 只在上下壁上断开剖面线和轮廓线，孔壁粗实线。孔轴线细点画线。"""
    from .detailing import bore_radius_at, cross_hole_zone
    from .render_gear import _hatch
    sc = main.scale
    R = h["D"] / 2
    rt = h["hole"] / 2
    rd = h["thread"]["d"] / 2 if h["thread"] else rt
    z = h["z"]
    y_end = -R if h["through"] else R - h["drill"]
    y_thr = -R if h["through"] else R - (h["depth"] if h["thread"] else h["drill"])
    cone = 0.0 if h["through"] else rt * math.tan(math.radians(31))

    def white(za, zb, ya, yb):
        (xa, y_a), (xb, y_b) = P(za, ya), P(zb, yb)
        lo, hi = min(y_a, y_b), max(y_a, y_b)
        s.parts.append(f"<rect x='{xa:.2f}' y='{s.y(hi) - 0.4:.2f}' width='{xb - xa:.2f}' height='{hi - lo + 0.8:.2f}' "
                       f"fill='white' stroke='none'/>")
    if sect:
        rb = bore_radius_at(part, z)
        for sign in (1, -1):
            white(z - rt, z + rt, sign * rb if sign > 0 else -R, R if sign > 0 else -rb)
            for x_ in (z - rt, z + rt):
                s.line(*P(x_, sign * rb), *P(x_, sign * R), THICK)
    else:
        za, zb, y0, y1 = cross_hole_zone(part, h, sc)
        white(z - rt, z + rt, R - 0.01, R + 0.01)  # 孔口处上轮廓线断开
        if h["through"]:
            white(z - rt, z + rt, -R - 0.01, -R + 0.01)
        n = 0
        for a, b in ((za, z - rt), (z + rt, zb)):  # 孔两侧的剖面
            if b - a > 1e-6:
                poly = [P(a, y0), P(b, y0), P(b, y1), P(a, y1)]
                xs = [q[0] for q in poly]
                ys = [q[1] for q in poly]
                _hatch(s, f"xh{h['name']}{n}", poly, min(xs), min(ys), max(xs), max(ys))
                n += 1
        if not h["through"]:  # 孔底以下
            poly = [P(z - rt, y0), P(z + rt, y0), P(z + rt, y_end - cone), P(z, y_end - cone), P(z - rt, y_end)]
            xs = [q[0] for q in poly]
            ys = [q[1] for q in poly]
            _hatch(s, f"xh{h['name']}b", poly, min(xs), min(ys), max(xs), max(ys))
        for zz in (za, zb):  # 两侧波浪线
            k = 16
            pts = [(P(zz, 0)[0] + 0.5 * math.sin(i * math.pi / 4), P(0, y0 + (y1 - y0) * i / k)[1]) for i in range(k + 1)]
            s.poly(pts, THIN, close=False)
        if not h["through"]:
            k = 12
            pts = [(P(za + (zb - za) * i / k, 0)[0], P(0, y0)[1] + 0.5 * math.sin(i * math.pi / 3)) for i in range(k + 1)]
            s.poly(pts, THIN, close=False)
    if not sect:
        for x_ in (z - rt, z + rt):  # 孔壁（小径）
            s.line(*P(x_, R), *P(x_, y_end), THICK)
    if not sect and not h["through"]:  # 钻尖
        s.line(*P(z - rt, y_end), *P(z, y_end - cone), THICK)
        s.line(*P(z + rt, y_end), *P(z, y_end - cone), THICK)
    if h["thread"] and not sect:  # 大径细实线到螺纹深 + 终止线
        for x_ in (z - rd, z + rd):
            s.line(*P(x_, R), *P(x_, y_thr), THIN)
        if not h["through"]:
            s.line(*P(z - rd, y_thr), *P(z + rd, y_thr), THICK)
    y_bot = -R - 2.5 / sc if h["through"] else y_end - cone - 2 / sc
    s.line(*P(z, R + 2.5 / sc), *P(z, y_bot), THIN, CENTER)


def radius_at_seg(segs, z):
    for a, b, r in segs:
        if a - 1e-9 <= z <= b + 1e-9:
            return r
    return 0.0


def render_shaft(s, plan: DrawingPlan, part: ResolvedPart, refs: list[str]) -> None:
    main = plan.views[0]
    sc = main.scale
    Ltot = part.bbox.zmax - part.bbox.zmin
    cx, cy = main.origin_on_sheet
    from .detailing import shaft_sx

    def P(z, r):
        return shaft_sx(main, Ltot, z), cy + r * sc

    segs = _profile(part)
    c = part.shaft_chamfer
    sect = bool(part.shaft_bores or part.flange_holes)  # 主视图全剖：套类/空心轴，或带法兰均布孔的盘盖（含闷盖）
    if sect:
        _hollow_section(s, part, P, segs, c, Ltot)
        s.text_bg = True  # 此后的尺寸数字垫白底：压在剖面线上时剖面线断开
    # 外形：上下轮廓线 + 轴肩/槽边竖线（空心件由 _hollow_section 画剖视轮廓）
    for sign in (() if sect else (1, -1)):
        pts = [P(0, 0), P(0, sign * (segs[0][2] - c))]
        if c:
            pts.append(P(c, sign * segs[0][2]))
        for a, b, r in segs:
            pts += [P(a if not (c and a == 0) else c, sign * r), P(b if not (c and b == Ltot) else Ltot - c, sign * r)]
        if c:
            pts.append(P(Ltot, sign * (segs[-1][2] - c)))
        pts.append(P(Ltot, 0))
        s.poly(pts, THICK, close=False)
    for (a, b, r), (a2, b2, r2) in zip(segs, segs[1:]) if not sect else ():
        if abs(r - r2) > 1e-9:
            rm = max(r, r2)
            s.line(*P(b, -rm), *P(b, rm), THICK)
    if c and not sect:
        for z, r in ((c, segs[0][2]), (Ltot - c, segs[-1][2])):
            s.line(*P(z, -r), *P(z, r), THICK)
    # 轴端螺孔（不可见螺纹，GB/T 4459.1：全部细虚线）：小径（底孔）到底孔深 + 118° 钻尖，大径与终止线到螺纹深
    for h in part.end_holes:
        sg = 1 if h["side"] == "start" else -1
        z_end = 0.0 if sg > 0 else Ltot
        rt, rd = h["tap"] / 2, h["d"] / 2
        zt, zd = z_end + sg * h["depth"], z_end + sg * h["drill"]
        cone = rt * math.tan(math.radians(31))  # 118° 钻尖：轴向长 = r·tan(31°)
        for y in (rt, -rt):
            s.line(*P(z_end, y), *P(zd, y), THIN, dash=HIDDEN)
            s.line(*P(zd, y), *P(zd + sg * cone, 0), THIN, dash=HIDDEN)
        for y in (rd, -rd):
            s.line(*P(z_end, y), *P(zt, y), THIN, dash=HIDDEN)
        s.line(*P(zt, -rd), *P(zt, rd), THIN, dash=HIDDEN)
    for h in part.cross_holes:  # 竖直横孔：局部剖（实心轴）或全剖中孔壁断开（套类）
        if h["dir"] == "vertical":
            _vertical_cross_hole(s, part, main, P, h, sect, segs)
    # 径向横孔（孔轴线沿投射方向）：孔圆粗实线；螺孔加大径 3/4 圈细实线（GB/T 4459.1）；竖直中心线
    for h in part.cross_holes:
        if h["dir"] == "vertical":
            continue
        hx, hy = P(h["z"], 0)
        rh = h["hole"] / 2 * sc
        s.circle(hx, hy, rh, THICK)
        if h["thread"]:
            rd = h["thread"]["d"] / 2 * sc
            arc = [(hx + rd * math.cos(math.radians(a)), hy + rd * math.sin(math.radians(a))) for a in range(0, 271, 6)]
            s.poly(arc, THIN, close=False)
        ext = (h["thread"]["d"] / 2 if h["thread"] else h["hole"] / 2) * sc + 2
        s.line(hx, hy - ext, hx, hy + ext, THIN, CENTER)
    # 断开画法：盖住间隙内的轮廓，两侧画波浪线（细实线，GB/T 4457.4）
    for a, b in main.breaks:
        r = radius_at_seg(segs, (a + b) / 2)
        xa, xb = P(a, 0)[0], P(b, 0)[0]
        s.parts.append(f"<rect x='{xa + 0.3:.2f}' y='{s.y(cy + r * sc + 1):.2f}' width='{xb - xa - 0.6:.2f}' "
                       f"height='{2 * r * sc + 2:.2f}' fill='white' stroke='none'/>")
        for x in (xa, xb):
            n = 24
            pts = [(x + 1.2 * math.sin(k / n * 2 * math.pi * 1.5), cy - r * sc + 2 * r * sc * k / n) for k in range(n + 1)]
            s.poly(pts, THIN, close=False)
    s.line(*P(-3, 0), *P(Ltot + 3, 0), THIN, CENTER)

    # 外螺纹（GB/T 4459.1）：大径粗实线（即轮廓），小径细实线画到螺纹终止处（倒角内也画），终止线粗实线
    for f in (f for f in part.shaft_features if f["kind"] == "thread"):
        r1 = f["d1"] / 2
        s.line(*P(f["z0"], r1), *P(f["z1"], r1), THIN)
        s.line(*P(f["z0"], -r1), *P(f["z1"], -r1), THIN)
        zt = f["z0"] if f["side"] == "end" else f["z1"]  # 终止线在螺纹靠轴内的一端
        if 1e-6 < zt < Ltot - 1e-6:
            if sect:  # 剖视中终止线只画螺纹牙高部分（大径到小径）
                for sg in (1, -1):
                    s.line(*P(zt, sg * r1), *P(zt, sg * f["d"] / 2), THICK)
            else:
                s.line(*P(zt, -f["d"] / 2), *P(zt, f["d"] / 2), THICK)
    # 齿轮段（GB/T 4459.2 外形视图）：齿顶线即轮廓；分度线细点画线（伸出端面 2 mm）、齿根线细实线
    for f in (f for f in part.shaft_features if f["kind"] == "gear"):
        for sign in (1, -1):
            s.line(*P(f["z0"] - 2 / sc, sign * f["pd"] / 2), *P(f["z1"] + 2 / sc, sign * f["pd"] / 2), THIN, CENTER)
            s.line(*P(f["z0"], sign * f["df"] / 2), *P(f["z1"], sign * f["df"] / 2), THIN)
    # 键槽（朝前，主视图显示其实形）
    kws = [f for f in part.shaft_features if f["kind"] == "keyway"]
    for f in (kws if not sect else ()):  # 全剖：键槽在上方，由剖视轮廓表达
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

    from .detailing import _shaft_dia_label, shaft_dia_overflow, shaft_dia_z
    up_lines = []  # 向上伸出轮廓的直径尺寸线（及其竖写文字）：上方尺寸行的数字不放在这里
    for j_ in range(len(part.shaft_steps)):
        if shaft_dia_overflow(part, j_, sc, _shaft_dia_label(part, j_)) == "up":
            x_ = P(shaft_dia_z(part, j_, sc), 0)[0]
            up_lines.append((x_ - 4.5, x_ + 4.5))
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
                _hdim(s, xa, xb, cy - radius_at(za + 1e-6) * sc, cy - radius_at(zb - 1e-6) * sc, y, d.text,
                      busy=_row_text_spans(plan, d, P, lambda d2: d2.ref.startswith("step")),
                      text_x=(d.extra or {}).get("text_x"))
            else:  # 键槽、环槽定位：画在主视图上方
                tr_ = (d.extra or {}).get("track", 0)
                y = y_top + 14 - 7 * tr_
                row_ = _row_text_spans(plan, d, P, lambda d2: d2.ref != "overall_z" and not d2.ref.startswith("step")
                                       and (d2.extra or {}).get("track", 0) == tr_) + up_lines
                _hdim(s, xa, xb, cy + radius_at(za) * sc, cy + radius_at(zb) * sc, y, d.text, busy=row_,
                      text_x=(d.extra or {}).get("text_x"))
        elif d.kind == "diameter" and d.at is not None and d.ref.startswith("bore"):
            from .detailing import bore_dia_overflow, bore_dia_place
            k = int(d.ref[4:].split("_")[0])
            x = P(bore_dia_place(part, k, sc), 0)[0]
            dia = d.at[1]
            _vdim(s, x, cy - dia / 2 * sc, cy + dia / 2 * sc, d.text, overflow=bore_dia_overflow(part, k, sc, d.text) or "up")
        elif d.kind == "diameter" and d.at is not None:
            zm, dia = d.at
            i = int(d.ref[4:].split("_")[0])
            from .detailing import shaft_dia_overflow, shaft_dia_place
            zz, side = shaft_dia_place(part, i, sc)
            x = P(zz, 0)[0]
            from .detailing import shaft_dia_blocked, shaft_text_room
            _vdim(s, x, cy - dia / 2 * sc, cy + dia / 2 * sc, d.text, text_left=side == "left",
                  room=0.0 if shaft_dia_blocked(part, i, sc) else shaft_text_room(part, zz, dia) * sc,
                  overflow=shaft_dia_overflow(part, i, sc, d.text) or "up")
        elif d.kind in ("datum", "gtol", "roughness"):
            from . import render_gdt
            z, r = d.at
            px, py = P(z, r)
            row = d.extra.get("sheet_pos", (0, y_top + 22))[1]
            if d.kind == "gtol" and d.extra.get("elbow"):  # 轴肩端面：小端一侧水平引线 + 轴向箭头，竖段上到框格行
                e = d.extra["elbow"]
                s.line(e["x_leader"], e["y"], e["x_face"], e["y"], THIN)
                _arrow(s, e["x_face"], e["y"], e["dir"], 0)
                render_gdt.fcf(s, e["x_leader"], e["y"], (0, 1), d.extra["cells"], reach=row - e["y"], arrow=False)
            elif d.kind == "gtol":  # 轴上框格放到键槽尺寸行之上
                x_ = d.extra["sheet_pos"][0]
                render_gdt.fcf(s, x_, py, (0, 1), d.extra["cells"], reach=row - py)
            elif d.kind == "roughness" and d.extra.get("on_frame"):  # 标在框格上方（GB/T 131）
                render_gdt.roughness(s, *d.extra["sheet_at"], (0, 1), d.text)
            else:
                render_gdt.draw(s, d.kind, px, py, d.extra, d.text)
        elif d.kind == "callout" and d.ref.startswith("xh:") and (d.extra or {}).get("label"):
            lb = d.extra["label"]
            s.line(*lb["anchor"], *lb["elbow"], THIN)
            s.line(*lb["elbow"], *lb["end"], THIN)
            _arrow(s, *lb["anchor"], lb["anchor"][0] - lb["elbow"][0], lb["anchor"][1] - lb["elbow"][1])
            s.text(*lb["text_at"], d.text, 3.5)
        elif d.kind == "callout" and d.ref.startswith(("ch:", "eh:")) and (d.extra or {}).get("label"):
            lb = d.extra["label"]  # 中心孔 GB/T 4459.5
            s.line(*lb["anchor"], *lb["elbow"], THIN)
            s.line(*lb["elbow"], *lb["end"], THIN)
            _arrow(s, *lb["anchor"], lb["anchor"][0] - lb["elbow"][0], lb["anchor"][1] - lb["elbow"][1])
            sx, sy = lb["sym_at"]
            keep = d.extra.get("keep")
            if keep in ("required", "forbidden"):  # 60° 开口符号（"<"）；不允许保留再加一竖
                s.line(sx + 0.3, sy + 2.2, sx + 3, sy + 3.75, THIN)
                s.line(sx + 0.3, sy + 2.2, sx + 3, sy + 0.65, THIN)
                if keep == "forbidden":
                    s.line(sx + 1.8, sy + 0.6, sx + 1.8, sy + 3.8, THIN)
            s.text(*lb["text_at"], d.text, 3)
        elif d.kind == "callout" and d.at is not None and "elbow" in (d.extra or {}).get("label", {}):
            lb = d.extra["label"]  # 规划层（shaft_dim_sheet）给定：引线折点、文字位置、出框则向左
            s.line(*lb["anchor"], *lb["elbow"], THIN)
            s.line(*lb["elbow"], *lb["end"], THIN)
            s.text(*lb["text_at"], d.text, 3)
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

    # 左视图（套类内键槽）：外圆、端部倒角圆、从左端可见的孔（带键槽缺口），尺寸按规划层图纸坐标
    lv = next((v for v in plan.views if v.type == "left"), None)
    if lv is not None:
        from .render_gear import draw_sheet_linear
        ex, ey = lv.origin_on_sheet
        Dm = max(d for _, d in part.shaft_steps)
        seen = 0.0
        for _, dj in part.shaft_steps:  # 从左端看得见的轴段外圆：比左边各段都大的
            if dj > seen + 1e-9:
                s.circle(ex, ey, dj / 2 * sc, THICK)
                seen = dj
        d0 = part.shaft_steps[0][1]
        if c:
            s.circle(ex, ey, (d0 / 2 - c) * sc, THICK)
        from .detailing import flange_hole_sheet
        for fh in part.flange_holes:  # 均布孔：分布圆细点画线 + 孔（被左侧更大轴段挡住时细虚线）+ 孔的中心线
            s.circle(ex, ey, fh["pcd"] / 2 * sc, THIN, CENTER)
            hidden = any(dj / 2 > fh["pcd"] / 2 - fh["d"] / 2 for _, dj in part.shaft_steps[:fh["step"]])
            rh = fh["d"] / 2 * sc
            for q in fh["pts"]:
                hx, hy = flange_hole_sheet(lv, *q)
                s.circle(hx, hy, rh, THIN if hidden else THICK, HIDDEN if hidden else None)
                ux, uy = (hx - ex), (hy - ey)
                n_ = math.hypot(ux, uy) or 1
                ux, uy = ux / n_, uy / n_
                s.line(hx - ux * (rh + 2), hy - uy * (rh + 2), hx + ux * (rh + 2), hy + uy * (rh + 2), THIN, CENTER)
        for b in sorted((b for b in part.shaft_bores if b["z0"] < 1e-9), key=lambda b: -b["d"]):
            rb = b["d"] / 2 * sc
            if b.get("kw"):  # 键槽朝上（与主视图一致）
                hw = b["kw"]["width"] / 2 * sc
                a0 = math.asin(min(1.0, hw / rb))
                pts = [(ex + rb * math.sin(a), ey + rb * math.cos(a)) for a in [a0 + (2 * math.pi - 2 * a0) * k / 96 for k in range(97)]]
                yk = ey + b["kw"]["top"] * sc
                s.poly(pts + [(ex - hw, yk), (ex + hw, yk)], THICK)
            else:
                s.circle(ex, ey, rb, THICK)
        R_ = Dm / 2 * sc
        s.line(ex - R_ - 4, ey, ex + R_ + 4, ey, THIN, CENTER)
        s.line(ex, ey - R_ - 4, ex, ey + R_ + 4, THIN, CENTER)
        for d in plan.dims:
            if d.view == "left" and d.kind == "linear" and (d.extra or {}).get("sheet_pts"):
                draw_sheet_linear(s, d)
                refs.append(d.ref)
            elif d.view == "left" and d.kind == "gtol" and (d.extra or {}).get("elbow_h"):
                from .detailing import draw_elbow_fcf
                draw_elbow_fcf(s, d.extra)
                refs.append(d.ref)
            elif d.view == "left" and d.kind == "callout" and (d.extra or {}).get("label"):
                lb = d.extra["label"]
                s.line(*lb["anchor"], *lb["elbow"], THIN)
                s.line(*lb["elbow"], *lb["end"], THIN)
                _arrow(s, *lb["anchor"], lb["anchor"][0] - lb["elbow"][0], lb["anchor"][1] - lb["elbow"][1])
                s.text(*lb["text_at"], d.text, 3.5)
                refs.append(d.ref)

    for d in plan.dims:  # 局部淬火粗点画线（坐标由规划层给出）
        if d.kind == "chain":
            (xa, ya), (xb, yb) = d.extra["sheet_pts"]
            s.line(xa, ya, xb, yb, THICK, dash="8,1.5,1,1.5")
            refs.append(d.ref)
    for v in plan.views:
        if v.type == "detail":
            _detail_view(s, plan, part, v, segs, refs)

    # 断面图
    for v in plan.views[1:]:
        if v.type != "section":
            continue
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
        zc_ = (f["z0"] + f["z1"]) / 2
        from .detailing import bore_radius_at
        rb_ = bore_radius_at(part, zc_) * sc
        if rb_ > 0:  # 空心轴：断面中的内孔
            s.parts.append(f"<circle cx='{vx:.2f}' cy='{s.y(vy):.2f}' r='{rb_ - 0.2:.2f}' fill='white' stroke='none'/>")
            s.circle(vx, vy, rb_, THICK)
        for h in part.end_holes:  # 断面切到轴端螺孔：孔（小径/底孔）粗实线、大径 3/4 圈细实线（GB/T 4459.1）
            if h["z0"] - 1e-6 <= zc_ <= h["z1"] + 1e-6:
                rt = h["tap"] / 2 * sc
                s.circle(vx, vy, rt, THICK)
                s.parts.append(f"<circle cx='{vx:.2f}' cy='{s.y(vy):.2f}' r='{rt - 0.3:.2f}' fill='white' stroke='none'/>")
                inside_thread = (zc_ <= h["depth"]) if h["side"] == "start" else (zc_ >= h["z1"] - h["depth"])
                if inside_thread:
                    rd = h["d"] / 2 * sc
                    pts = [(vx + rd * math.cos(math.radians(a)), vy + rd * math.sin(math.radians(a))) for a in range(0, 271, 10)]
                    s.poly(pts, THIN, close=False)
        s.line(vx - r - 3, vy, vx + r + 3, vy, THIN, CENTER)
        s.line(vx, vy - r - 3, vx, vy + r + 3, THIN, CENTER)
        s.text(vx, vy + r + 12, v.label, 4, "middle")
        # 剖切位置符号（主视图上下两端短粗线 + 字母）
        zc = (f["z0"] + f["z1"]) / 2
        mx = P(zc, 0)[0]
        for yy, sign in ((y_top + 3, 1), (y_bot - 3, -1)):
            s.line(mx, yy, mx, yy + sign * 5, 0.7)
            s.text(mx + 1.5, yy + (sign * 2 if sign > 0 else -4.5), v.label[0], 3.5)
        for d in plan.dims:  # 键槽对称度：箭头与槽宽尺寸线对齐，框格在断面左侧
            if d.view == "section" and d.kind == "gtol" and (d.extra or {}).get("keyway") == f["name"] \
                    and "sheet_at" in d.extra:
                from . import render_gdt
                render_gdt.fcf(s, *d.extra["sheet_at"], (-1, 0), d.extra["cells"], reach=d.extra["reach"])
                refs.append(d.ref)
        for d in plan.dims:
            if d.view != "section" or not d.ref.startswith(f["name"] + "_"):
                continue
            if d.ref.endswith("_w"):
                _hdim(s, vx - hb, vx + hb, vy + r - t1, vy + r - t1, vy + r + 5, d.text)
            else:
                xs = (d.extra or {}).get("sheet_pos", (vx + r + 6, 0))[0]
                if xs > vx:
                    _vdim(s, xs, vy - r, vy + r - t1, d.text, text_left=False)
                    s.line(vx + hb, vy + r - t1, xs + 1.5, vy + r - t1, THIN)
                    s.line(vx, vy - r, xs + 1.5, vy - r, THIN)
                else:  # 左侧（规划层：右侧出图框）
                    _vdim(s, xs, vy - r, vy + r - t1, d.text, text_left=True)
                    s.line(vx - hb, vy + r - t1, xs - 1.5, vy + r - t1, THIN)
                    s.line(vx, vy - r, xs - 1.5, vy - r, THIN)
            refs.append(d.ref)


def _clip_circle(p0, p1, c, r):
    """线段 p0-p1 在圆 (c, r) 内的部分（参数区间），无则 None。"""
    (x0, y0), (x1, y1) = p0, p1
    dx, dy = x1 - x0, y1 - y0
    fx, fy = x0 - c[0], y0 - c[1]
    A = dx * dx + dy * dy
    if A < 1e-18:
        return None
    B = 2 * (fx * dx + fy * dy)
    C = fx * fx + fy * fy - r * r
    disc = B * B - 4 * A * C
    if disc <= 0:
        return None
    sq = math.sqrt(disc)
    t0, t1 = max(0.0, (-B - sq) / (2 * A)), min(1.0, (-B + sq) / (2 * A))
    if t1 - t0 < 1e-9:
        return None
    return (x0 + dx * t0, y0 + dy * t0), (x0 + dx * t1, y0 + dy * t1)


def _detail_view(s, plan, part, v, segs, refs) -> None:
    """局部放大图（GB/T 4458.1）：主视图上细实线圆 + 引线罗马数字；放大图 = 圆内的外轮廓（粗实线）+ 材料一侧
    的波浪线边界；上方标注 罗马数字/比例；放大图内尺寸。"""
    from .detailing import detail_sheet, scale_label
    from .render_svg import _text_w
    dt = v.detail
    if "circle" in dt:
        cx, cy, r = dt["circle"]
        s.circle(cx, cy, r, THIN)
        (ax, ay), (bx, by) = dt["leader"]
        s.line(ax, ay, bx, by, THIN)
        s.text(*dt["label_at"], v.label, 3.5)
    S = detail_sheet(v)
    zc, yc, rm = dt["zc"], dt["yc"], dt["rm"]
    pts = []  # 上半外轮廓折线（模型）
    for a, b, rr in segs:
        if pts and abs(pts[-1][1] - rr) > 1e-9:
            pts.append((a, rr))
        elif not pts:
            pts.append((a, rr))
        pts.append((b, rr))
    for p0, p1 in zip(pts, pts[1:]):
        seg = _clip_circle(p0, p1, (zc, yc), rm)
        if seg:
            s.line(*S(*seg[0]), *S(*seg[1]), THICK)
    xd, yd = v.origin_on_sheet
    rs = rm * v.scale
    run: list = []
    for k in range(0, 361, 2):  # 材料一侧（轮廓以下）的圆周：细波浪线
        t = math.radians(k)
        z, y = zc + rm * math.cos(t), yc + rm * math.sin(t)
        if y < radius_at_seg(segs, z) - 1e-6:
            w = rs + 0.5 * math.sin(k * math.pi / 15)
            run.append((xd + w * math.cos(t), yd + w * math.sin(t)))
        elif run:
            if len(run) > 1:
                s.poly(run, THIN, close=False)
            run = []
    if len(run) > 1:
        s.poly(run, THIN, close=False)
    top = yd + rs
    s.text(xd, top + 8.5, v.label, 3.5, "middle")
    s.line(xd - 5, top + 7.2, xd + 5, top + 7.2, THIN)
    s.text(xd, top + 3, scale_label(v.scale), 3.5, "middle")
    for d in plan.dims:
        ex = d.extra or {}
        if d.view != "detail" or ex.get("detail") != v.label:
            continue
        (xa, ya), (xb, yb) = ex["sheet_pts"]
        px, py = ex["sheet_pos"]
        if ex["orient"] == "h":  # 槽宽：注在槽底下方（材料内），数字在尺寸线上方
            for x in (xa, xb):
                s.line(x, ya - 1, x, py - 1.5, THIN)
            s.line(xa, py, xb, py, THIN)
            _arrow(s, xa, py, -1, 0)
            _arrow(s, xb, py, 1, 0)
            s.text(px, py + 0.8, d.text, 3.5, "middle")
        else:  # 槽深：自由侧，尺寸界线自槽底角水平引出，箭头在外，数字在尺寸线向上的延长线左侧
            sg = 1 if px > xa else -1
            s.line(xa + sg * 1, ya, px + sg * 1.5, ya, THIN)
            tw = _text_w(d.text, 3.5)
            s.line(px, ya - 4, px, py + tw / 2 + 1, THIN)
            _arrow(s, px, ya, 0, 1)
            _arrow(s, px, yb, 0, -1)
            s.text(px - 1, py, d.text, 3.5, "middle", -90)
        refs.append(d.ref)
