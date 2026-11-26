"""剖视图（与 CAD 无关）：选剖切位置（全剖 / 阶梯剖 / 半剖）、计算剖面几何（实体、孔腔、螺纹线）。

坐标约定：剖切面 ⟂ D 轴，视图内水平轴 H、竖直轴 Z（模型 z）。
- 主视图剖视（view="front"）：H=x，D=y，从 -Y 向 +Y 看；剖切线画在俯视图上。
- 左视图剖视（view="left"）：H=y，D=x，从 -X 向 +X 看；剖切线画在俯视图上。
几何以剖切面上的三维点输出，渲染/适配器用各自视图的投影即可（镜像由视图轴处理）。
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

AX = {"x": 0, "y": 1, "z": 2}
FRAME = {"front": ("x", "y"), "left": ("y", "x")}  # 视图 → (H 轴, D 轴)
TOL = 0.5


@dataclass
class SectionPlan:
    label: str
    kind: str  # full | step | half | local
    view: str  # 被剖视替代的视图：front | left
    parent: str  # 画剖切线的视图
    path: list[tuple[float, float, float]]  # [(h0, h1, d)]：H 区间内在 D=d 处剖切
    half_from: float | None = None  # 半剖：H ≥ half_from 的一半剖开
    features: list[str] = field(default_factory=list)  # 剖到的特征
    geometry: dict[str, Any] = field(default_factory=dict)
    cut_points: list[tuple[float, float, float]] = field(default_factory=list)  # 剖切线（模型坐标，顶面上）
    bbox: Any = None  # 零件包围盒（视图投影用）
    wave_amp: float = 1.0  # 局部剖波浪线幅值（模型 mm；规划时按比例换算成图纸上约 1.2 mm）


def _p3(view: str, h: float, d: float, z: float) -> tuple[float, float, float]:
    ha, da = FRAME[view]
    p = [0.0, 0.0, 0.0]
    p[AX[ha]], p[AX[da]], p[2] = h, d, z
    return tuple(p)


def _intervals(poly: list[tuple[float, float]], axis_scan: int, c: float) -> list[tuple[float, float]]:
    """多边形与直线（第 axis_scan 个坐标 = c）的交区间，返回另一坐标的区间。"""
    o = 1 - axis_scan
    xs = []
    n = len(poly)
    for i in range(n):
        a, b = poly[i], poly[(i + 1) % n]
        if (a[axis_scan] - c) * (b[axis_scan] - c) < 0 or (a[axis_scan] == c and b[axis_scan] != c):
            if b[axis_scan] == a[axis_scan]:
                continue
            t = (c - a[axis_scan]) / (b[axis_scan] - a[axis_scan])
            xs.append(a[o] + t * (b[o] - a[o]))
    xs.sort()
    return [(xs[i], xs[i + 1]) for i in range(0, len(xs) - 1, 2) if xs[i + 1] - xs[i] > 1e-6]


def _clip(iv: list[tuple[float, float]], lo: float, hi: float) -> list[tuple[float, float]]:
    return [(max(a, lo), min(b, hi)) for a, b in iv if min(b, hi) - max(a, lo) > 1e-6]


def hole_profile(h) -> list[tuple[float, float]]:
    """孔沿轴向的 (深度, 半径) 折线：沉孔台阶 / 沉头锥 / 钻孔 / 盲孔 118° 钻尖。"""
    pts: list[tuple[float, float]] = []
    rd = h.drill / 2
    if h.cbore:
        dc, hc = h.cbore
        pts += [(0.0, dc / 2), (hc, dc / 2), (hc, rd)]
    elif h.csink:
        pts += [(0.0, h.csink / 2), ((h.csink / 2 - rd), rd)]  # 90° 沉头
    else:
        pts.append((0.0, rd))
    pts.append((h.depth, rd))
    if not h.through:
        pts.append((h.depth + rd / math.tan(math.radians(59)), 0.0))
    return pts


def _r_at(prof: list[tuple[float, float]], depth: float) -> float:
    for (d0, r0), (d1, r1) in zip(prof, prof[1:]):
        if d0 - 1e-9 <= depth <= d1 + 1e-9:
            return r0 if d1 - d0 < 1e-9 else r0 + (r1 - r0) * (depth - d0) / (d1 - d0)
    return 0.0


def _interesting(p) -> list[tuple[str, list[tuple[float, float]], str]]:
    """内部结构（值得剖开表达的特征）：(特征名, 各实例的 (H 坐标, D 坐标)-按主视图, 类型)。"""
    out = []
    for h in p.holes:
        inner = (h.cbore or h.csink or h.type == "tapped" or not h.through or h.face not in ("top", "bottom")
                 or h.tolerance)  # 配合孔（H7 等）剖开表达孔壁与倒角
        if inner and not h.slot:
            out.append((h.name, [(i.xyz[0], i.xyz[1]) for i in h.instances], "hole"))
    for pk in p.pockets:
        if not pk["through"] and pk["face"] in ("top", "bottom"):
            u0, u1, v0, v1 = pk["profile"].bbox2d
            out.append((pk["name"], [((u0 + u1) / 2, (v0 + v1) / 2)], "pocket"))
    return out


LOCAL_SPAN = 0.35  # 内部结构集中在视图宽度的这个比例内 → 局部剖


def _local_range(p, view: str, reps: list[tuple[float, float]], lo_h: float, hi_h: float) -> tuple[float, float]:
    """局部剖区间：代表孔/型腔的 H 范围外扩半径 + 余量，不超出零件。reps = [(H 坐标, 半宽)]。"""
    m = 3.0 + 0.1 * (hi_h - lo_h) ** 0.5
    h0 = max(lo_h + 1, min(h - r for h, r in reps) - m)
    h1 = min(hi_h - 1, max(h + r for h, r in reps) + m)
    return h0, h1


def _half_width(p, name: str) -> float:
    for h in p.holes:
        if h.name == name:
            return h.max_dia / 2
    for pk in p.pockets:
        if pk["name"] == name:
            u0, u1, _, _ = pk["profile"].bbox2d
            return (u1 - u0) / 2
    return 3.0


def needs_section(p) -> bool:
    return p.base_type == "extrude" and p.profile is not None and bool(_interesting(p))


def auto_section(p, label: str = "A") -> SectionPlan | None:
    """自动选剖切：一个 D 位置能剖到所有内部结构 → 全剖；否则阶梯剖（最多 3 个位置）。"""
    if not needs_section(p):
        return None
    feats = _interesting(p)
    view, (ha, da) = "front", FRAME["front"]
    hi_, di_ = AX[ha], AX[da]
    b = p.bbox
    lo_h, hi_h = (b.xmin, b.xmax)
    cands = sorted({round(pt[di_], 3) for _, pts, _ in feats for pt in pts})
    mid_d = (b.ymin + b.ymax) / 2

    def covers(c):
        return {n for n, pts, _ in feats if any(abs(pt[di_] - c) < TOL for pt in pts)}

    allf = {n for n, _, _ in feats}
    best = max(cands, key=lambda c: (len(covers(c)), -abs(c - mid_d)))
    if covers(best) == allf:
        reps = []
        for n, pts, _ in feats:
            hs = [pt[hi_] for pt in pts if abs(pt[di_] - best) < TOL]
            reps.append((min(hs, key=lambda h: abs(h - (lo_h + hi_h) / 2)), _half_width(p, n)))
        h0, h1 = _local_range(p, view, reps, lo_h, hi_h)
        if h1 - h0 < LOCAL_SPAN * (hi_h - lo_h):  # 内部结构只在一小段：局部剖，视图其余部分保留外形
            return SectionPlan(label, "local", view, "", [(h0, h1, best)], features=sorted(allf))
        return SectionPlan(label, "full", view, "top", [(lo_h, hi_h, best)], features=sorted(allf))
    chosen, left = [best], allf - covers(best)
    while left and len(chosen) < 3:
        c = max(cands, key=lambda c: (len(covers(c) & left), -abs(c - mid_d)))
        if not covers(c) & left:
            break
        chosen.append(c)
        left -= covers(c)
    # 每个位置取其覆盖特征的 H 坐标，按 H 排序分段，转折点取相邻代表点中点
    # 每个特征只需剖到一个实例：先按各剖切位置候选实例的平均 H 排定次序，每个特征取离本位置均值最近的实例，
    # 使同一位置的特征在 H 上连成一段，转折最少
    owner = {}
    for n, _, _ in feats:
        owner[n] = next(c for c in chosen if n in covers(c))
    reps = []
    for c in chosen:
        mine = [(n, [pt for pt in pts if abs(pt[di_] - c) < TOL]) for n, pts, _ in feats if owner[n] == c]
        mean = sum(pt[hi_] for _, ps in mine for pt in ps) / max(1, sum(len(ps) for _, ps in mine))
        for n, ps in mine:
            pt = min(ps, key=lambda q: abs(q[hi_] - mean))
            reps.append((pt[hi_], c))
    order = sorted(chosen, key=lambda c: sum(h for h, cc in reps if cc == c) / max(1, sum(1 for _, cc in reps if cc == c)))
    reps.sort(key=lambda r: (order.index(r[1]), r[0]))
    # 同一位置的代表点按 H 排序后，若不同位置区间交叠，退回按 H 全排序
    reps_h = sorted(reps)
    reps = reps if all(a[0] <= b[0] for a, b in zip(reps, reps[1:])) else reps_h
    segs: list[list[float]] = []
    for h, c in reps:
        if segs and segs[-1][2] == c:
            segs[-1][1] = h
        else:
            segs.append([h, h, c])
    path = []
    for i, (a, bb, c) in enumerate(segs):
        h0 = lo_h if i == 0 else (segs[i - 1][1] + a) / 2
        h1 = hi_h if i == len(segs) - 1 else (bb + segs[i + 1][0]) / 2
        path.append((h0, h1, c))
    covered = set().union(*(covers(c) for c in chosen))
    return SectionPlan(label, "step" if len(path) > 1 else "full", view, "top", path, features=sorted(covered))


def explicit_section(p, sp, label: str) -> SectionPlan:
    """用户指定：kind + at（D 坐标；阶梯剖多个）或 through（特征名，取其实例位置）。"""
    view = sp.view
    ha, da = FRAME[view]
    b = p.bbox
    lo_h, hi_h = (b.xmin, b.xmax) if ha == "x" else (b.ymin, b.ymax)
    lo_d, hi_d = (b.ymin, b.ymax) if da == "y" else (b.xmin, b.xmax)
    hi_, di_ = AX[ha], AX[da]
    pts: list[tuple[float, float]] = []
    widths: list[float] = []
    if sp.through:
        names = set(sp.through)
        for h in p.holes:
            if h.name in names:
                pts += [(i.xyz[hi_], i.xyz[di_]) for i in h.instances[:1]]
                widths.append(h.max_dia / 2)
        for pk in p.pockets:
            if pk["name"] in names:
                u0, u1, v0, v1 = pk["profile"].bbox2d
                c = ((u0 + u1) / 2, (v0 + v1) / 2, 0)
                pts.append((c[hi_], c[di_]))
                widths.append((u1 - u0) / 2 if hi_ == 0 else (v1 - v0) / 2)
    ats = [float(a) for a in (sp.at or [])] or [round(pt[1], 3) for pt in pts] or [(lo_d + hi_d) / 2]
    if sp.kind == "local":
        if not pts:
            raise ValueError("局部剖需要 through 指定要剖开的孔/型腔")
        h0, h1 = _local_range(p, view, list(zip([q[0] for q in pts], widths)), lo_h, hi_h)
        return SectionPlan(label, "local", view, "", [(h0, h1, ats[0])], features=sorted(sp.through))
    if sp.kind in ("full", "half") or len(set(ats)) == 1:
        path = [(lo_h, hi_h, ats[0])]
    else:
        hs = sorted(pts) if pts else [(lo_h + (hi_h - lo_h) * (i + 0.5) / len(ats), a) for i, a in enumerate(ats)]
        path = []
        for i, (h, c) in enumerate(hs):
            h0 = lo_h if i == 0 else (hs[i - 1][0] + h) / 2
            h1 = hi_h if i == len(hs) - 1 else (h + hs[i + 1][0]) / 2
            if path and path[-1][2] == c:
                path[-1] = (path[-1][0], h1, c)
            else:
                path.append((h0, h1, c))
    kind = sp.kind if sp.kind != "step" or len(path) > 1 else "full"
    return SectionPlan(label, kind, view, "top", path, half_from=(lo_h + hi_h) / 2 if kind == "half" else None,
                       features=sorted(sp.through or []))


def geometry(p, sec: SectionPlan) -> dict[str, Any]:
    """剖面几何：solid（剖面线区域）、voids（孔/型腔）、thin（螺纹大径等细实线）、thick（螺纹终止线）、
    axes（孔轴线）、outline（半剖未剖一半的外形）。均为剖切面上的三维点列。"""
    view = sec.view
    ha, da = FRAME[view]
    hi_, di_ = AX[ha], AX[da]
    zlo, zhi = (p.bbox.zmin, p.bbox.zmin + p.thickness) if p.thickness else (p.bbox.zmin, p.bbox.zmax)
    # voids：剖面线遮罩用的封闭轮廓；edges：孔腔要画的轮廓线（开口处不画）；openings：实体轮廓上要去掉的开口段
    out: dict[str, list] = {"solid": [], "voids": [], "edges": [], "openings": [], "thin": [], "thick": [],
                            "axes": [], "outline": []}
    outline = p.profile.outline
    scan = 1 if da == "y" else 0  # 外形 outline 坐标为 (x, y)
    path = sec.path
    if sec.kind == "half":
        path = [(max(h0, sec.half_from), h1, d) for h0, h1, d in path if h1 > sec.half_from]
        hmin = min(x for x, _, _ in sec.path)  # 未剖一半的外形：开口在中心线一侧
        out["outline"].append([_p3(view, h, path[0][2], z) for h, z in
                               ((sec.half_from, zlo), (hmin, zlo), (hmin, zhi), (sec.half_from, zhi))])
    for h0, h1, d in path:
        for a, b in _clip(_intervals(outline, scan, d), h0, h1):
            out["solid"].append([_p3(view, a, d, zlo), _p3(view, b, d, zlo), _p3(view, b, d, zhi), _p3(view, a, d, zhi)])
        for bs in p.bosses:
            for a, b in _clip(_intervals(bs["profile"].outline, scan, d), h0, h1):
                z0, z1 = bs["z0"], bs["z0"] + bs["height"]
                out["solid"].append([_p3(view, a, d, z0), _p3(view, b, d, z0), _p3(view, b, d, z1), _p3(view, a, d, z1)])
        for pk in p.pockets:
            if pk["face"] not in ("top", "bottom"):
                continue
            z1 = zhi if pk["face"] == "top" else zlo
            z0 = z1 - pk["depth"] if pk["face"] == "top" else z1 + pk["depth"]
            for a, b in _clip(_intervals(pk["profile"].outline, scan, d), h0, h1):
                A0, B0, B1, A1 = _p3(view, a, d, z0), _p3(view, b, d, z0), _p3(view, b, d, z1), _p3(view, a, d, z1)
                out["voids"].append([A0, B0, B1, A1])
                out["edges"].append([A1, A0, B0, B1])
                out["openings"].append([A1, B1])
        for h in p.holes:
            if h.slot:
                continue
            prof = hole_profile(h)
            for inst in h.instances:
                x = inst.xyz
                if not (h0 - 1e-6 <= x[hi_] <= h1 + 1e-6):
                    continue
                axis = [i for i in range(3) if inst.normal[i] != 0][0]
                inward = -inst.normal[axis]
                if axis == di_:  # 孔轴垂直剖切面：剖到的是圆截面
                    depth = (d - x[di_]) * inward
                    r = _r_at(prof, depth) if 0 <= depth <= prof[-1][0] else 0
                    if r > 0:
                        circ = [_p3(view, x[hi_] + r * math.cos(t), d, x[2] + r * math.sin(t))
                                for t in (2 * math.pi * k / 32 for k in range(32))]
                        out["voids"].append(circ)
                        out["edges"].append(circ + [circ[0]])
                    continue
                dist = abs(x[di_] - d)
                if dist >= max(r for _, r in prof):
                    continue

                def pt(depth, off, _x=x, _axis=axis, _in=inward):
                    q = list(_x)
                    q[_axis] += _in * depth
                    other = hi_ if _axis == 2 else 2
                    q[other] += off
                    q[di_] = d
                    return tuple(q)

                w = [(dd, math.sqrt(max(0.0, r * r - dist * dist))) for dd, r in prof]
                left = [pt(dd, -ww) for dd, ww in w]
                right = [pt(dd, ww) for dd, ww in w]
                out["voids"].append(left + right[::-1])
                out["openings"].append([left[0], right[0]])
                if w[-1][1] > 1e-6:  # 通孔：两端开口，两侧孔壁分开画
                    out["edges"] += [left, right]
                    out["openings"].append([left[-1], right[-1]])
                else:
                    out["edges"].append(left + right[::-1][1:])
                if dist < h.drill / 2:
                    out["axes"].append([pt(-2.0, 0.0), pt(prof[-1][0] + 2.0, 0.0)])
                if h.type == "tapped" and h.thread_depth:
                    rm = math.sqrt(max(0.0, (h.nominal / 2) ** 2 - dist * dist))
                    td = h.thread_depth
                    out["thin"] += [[pt(0, -rm), pt(td, -rm)], [pt(0, rm), pt(td, rm)]]
                    if not h.through or td < h.depth - 1e-6:
                        out["thick"].append([pt(td, -rm), pt(td, rm)])
    if sec.kind == "local":
        _local_breaks(out, sec, view, zlo, zhi)
    if sec.kind == "half":  # 只保留剖开一半上的线
        keep = lambda pl: any(q[hi_] >= sec.half_from - 1e-6 for q in pl) and \
            not all(abs(q[hi_] - sec.half_from) < 1e-6 for q in pl)  # noqa: E731
        for k in ("edges", "openings", "thin", "thick", "axes"):
            out[k] = [pl for pl in out[k] if keep(pl) or k == "axes"]
        out["edges"] = [[q for q in pl if q[hi_] >= sec.half_from - 1e-6] for pl in out["edges"]]
        out["edges"] = [pl for pl in out["edges"] if len(pl) > 1]
    return out


def _wave(h: float, d: float, view: str, z0: float, z1: float, amp: float, phase: float) -> list:
    """波浪线（局部剖分界，GB/T 4457.4 细实线）：沿 z 方向的正弦折线。"""
    n = max(1, round((z1 - z0) / (7 * amp)))  # 每个周期约 7 倍幅值
    k = 12 * n
    return [_p3(view, h + amp * math.sin(phase + 2 * math.pi * n * i / k), d, z0 + (z1 - z0) * i / k) for i in range(k + 1)]


def _local_breaks(out: dict[str, list], sec: SectionPlan, view: str, zlo: float, zhi: float) -> None:
    """局部剖：实体两端的竖直边换成波浪线（细实线，不画成轮廓），region 为擦除原视图的区域。"""
    h0, h1, d = sec.path[0]
    hi_ = AX[FRAME[view][0]]
    a = sec.wave_amp
    w0, w1 = _wave(h0, d, view, zlo, zhi, a, 0.0), _wave(h1, d, view, zlo, zhi, a, math.pi)

    def h_at(w, z):  # 波浪线在高度 z 处的 H
        for q0, q1 in zip(w, w[1:]):
            if q0[2] - 1e-9 <= z <= q1[2] + 1e-9:
                t = 0 if q1[2] == q0[2] else (z - q0[2]) / (q1[2] - q0[2])
                return q0[hi_] + t * (q1[hi_] - q0[hi_])
        return w[-1][hi_]

    solids = []
    for poly in out["solid"]:
        hs = [q[hi_] for q in poly]
        zs = [q[2] for q in poly]
        a_, b_, z0, z1 = min(hs), max(hs), min(zs), max(zs)
        left = [q for q in w0 if z0 - 1e-9 <= q[2] <= z1 + 1e-9] if abs(a_ - h0) < 1e-6 else \
            [_p3(view, a_, d, z0), _p3(view, a_, d, z1)]
        right = [q for q in w1 if z0 - 1e-9 <= q[2] <= z1 + 1e-9] if abs(b_ - h1) < 1e-6 else \
            [_p3(view, b_, d, z0), _p3(view, b_, d, z1)]
        if abs(a_ - h0) < 1e-6:
            left = [_p3(view, h_at(w0, z0), d, z0)] + left + [_p3(view, h_at(w0, z1), d, z1)]
        if abs(b_ - h1) < 1e-6:
            right = [_p3(view, h_at(w1, z0), d, z0)] + right + [_p3(view, h_at(w1, z1), d, z1)]
        solids.append(right + left[::-1])  # 下 → 右侧上行 → 上 → 左侧下行
    out["solid"] = solids
    out["breaks"] = [w0, w1]
    out["region"] = [w0 + w1[::-1]]


def cut_line(p, sec: SectionPlan) -> list[tuple[float, float, float]]:
    """剖切线（在俯视图上）：顶面上的折线点，两端伸出外形 4 mm。局部剖剖切位置明显，不画剖切符号。"""
    if sec.kind == "local":
        return []
    ha, da = FRAME[sec.view]
    z = p.bbox.zmax
    pts = []
    for i, (h0, h1, d) in enumerate(sec.path):
        a = h0 - (4 if i == 0 else 0)
        b = h1 + (4 if i == len(sec.path) - 1 else 0)
        pts.append(_p3(sec.view, a, d, z))  # 首段起点 / 阶梯转折点
        pts.append(_p3(sec.view, b, d, z))
    return pts
