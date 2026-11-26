"""焊接件解析（与 CAD 无关）：型钢截面、杆件/板件包围盒、下料清单、焊缝自动识别。

约定：杆件 start/end 为两端面中心，只支持沿坐标轴；截面"高"默认沿 Z（立柱沿 Y），rotate=90 换向。
截面以中心线为中心对称放置（角钢/槽钢按外包络）。
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Any

from ..errors import SPEC_INVALID, CadError

AXES = "xyz"
TOL = 0.01

# GB/T 706 热轧槽钢、工字钢（h, b, 腹板 d, 翼缘 t, 截面积 cm²）
CHANNEL = {"C8": (80, 43, 5.0, 8.0, 10.25), "C10": (100, 48, 5.3, 8.5, 12.75), "C12.6": (126, 53, 5.5, 9.0, 15.69),
           "C14a": (140, 58, 6.0, 9.5, 18.51), "C16a": (160, 63, 6.5, 10.0, 21.95)}
IBEAM = {"I10": (100, 68, 4.5, 7.6, 14.33), "I12.6": (126, 74, 5.0, 8.4, 18.10), "I14": (140, 80, 5.5, 9.1, 21.50),
         "I16": (160, 88, 6.0, 9.9, 26.11)}
# GB/T 6728 常用冷弯方矩管（边长, 壁厚）
SHS_STD = {(20, 2), (25, 2), (30, 2), (30, 3), (40, 2), (40, 3), (40, 4), (50, 3), (50, 4), (60, 3), (60, 4),
           (80, 4), (80, 5), (100, 4), (100, 5)}
RHS_STD = {(40, 20, 2), (50, 30, 3), (60, 40, 3), (80, 40, 3), (80, 40, 4), (100, 50, 4), (100, 60, 4), (120, 60, 4)}
FILLET_SERIES = [3, 4, 5, 6, 8, 10, 12]
NAMES = {"SHS": "方管", "RHS": "矩形管", "L": "角钢", "CHS": "圆管", "FB": "扁钢", "C": "槽钢", "I": "工字钢"}


@dataclass
class Section:
    code: str
    kind: str  # SHS RHS L CHS FB C I
    h: float  # 高（截面"高"方向外形）
    b: float  # 宽
    t: float  # 计算焊脚用的壁厚/板厚
    area: float  # mm²
    standard: bool = True
    hollow: bool = False

    @property
    def title(self) -> str:
        if self.kind in ("C", "I"):
            return f"{NAMES[self.kind]} {self.code[1:]}#"
        if self.kind == "CHS":
            return f"{NAMES['CHS']} Φ{self.h:g}×{self.t:g}"
        if self.kind == "FB":
            return f"{NAMES['FB']} {self.h:g}×{self.t:g}"
        if self.kind == "L":
            return f"{NAMES['L']} ∠{self.h:g}×{self.b:g}×{self.t:g}" if self.h != self.b else f"{NAMES['L']} ∠{self.h:g}×{self.t:g}"
        return f"{NAMES[self.kind]} {self.h:g}×{self.b:g}×{self.t:g}"

    @property
    def perimeter(self) -> float:
        return math.pi * self.h if self.kind == "CHS" else 2 * (self.h + self.b)


def section(code: str) -> Section:
    c = code.strip().upper().replace("×", "X").replace("*", "X")
    nums = [float(x) for x in re.findall(r"\d+(?:\.\d+)?", c)]
    m = re.match(r"^(SHS|RHS|CHS|FB|L)", c)
    if c in {k.upper() for k in CHANNEL}:
        key = next(k for k in CHANNEL if k.upper() == c)
        h, b, d, tf, a = CHANNEL[key]
        return Section(key, "C", h, b, tf, a * 100)
    if c in {k.upper() for k in IBEAM}:
        key = next(k for k in IBEAM if k.upper() == c)
        h, b, d, tf, a = IBEAM[key]
        return Section(key, "I", h, b, tf, a * 100)
    if not m:
        raise CadError(SPEC_INVALID, f"unknown section {code!r}",
                       hint="SHS40x3 / RHS60x40x3 / L50x5 / CHS48x3 / FB50x6 / C10 / I14")
    k = m.group(1)
    try:
        if k == "SHS":
            b = nums[0]
            t = nums[-1]
            s = Section(code, k, b, b, t, b * b - (b - 2 * t) ** 2, (b, t) in SHS_STD, True)
        elif k == "RHS":
            h, b, t = nums[0], nums[1], nums[2]
            s = Section(code, k, h, b, t, h * b - (h - 2 * t) * (b - 2 * t), (h, b, t) in RHS_STD, True)
        elif k == "L":
            b1, b2, t = (nums[0], nums[0], nums[1]) if len(nums) == 2 else (nums[0], nums[1], nums[2])
            s = Section(code, k, b1, b2, t, t * (b1 + b2 - t))
        elif k == "CHS":
            D, t = nums[0], nums[1]
            s = Section(code, k, D, D, t, math.pi / 4 * (D * D - (D - 2 * t) ** 2), True, True)
        else:
            b, t = nums[0], nums[1]
            s = Section(code, k, b, t, t, b * t)
    except IndexError:
        raise CadError(SPEC_INVALID, f"section {code!r}: missing dimensions") from None
    if s.t <= 0 or 2 * s.t >= min(s.h, s.b) and s.hollow:
        raise CadError(SPEC_INVALID, f"section {code!r}: wall thickness {s.t:g} invalid")
    return s


@dataclass
class Body:
    name: str
    kind: str  # member | plate
    box: tuple[float, float, float, float, float, float]  # xmin xmax ymin ymax zmin zmax
    t: float  # 焊脚计算用厚度
    volume: float
    section: Section | None = None
    axis: str | None = None  # 杆件轴向 x/y/z
    length: float = 0.0
    start: tuple[float, float, float] | None = None
    end: tuple[float, float, float] | None = None
    height_axis: str | None = None  # 截面"高"方向
    normal: str | None = None  # 板厚方向
    machined: bool = False

    def lo(self, a: str) -> float:
        return self.box[2 * AXES.index(a)]

    def hi(self, a: str) -> float:
        return self.box[2 * AXES.index(a) + 1]


def member_body(m, ev) -> Body:
    s = section(m.section)
    p0 = tuple(ev(v) for v in m.start)
    p1 = tuple(ev(v) for v in m.end)
    diff = [abs(a - b) for a, b in zip(p0, p1)]
    moving = [i for i, d in enumerate(diff) if d > 1e-6]
    if len(moving) != 1:
        raise CadError(SPEC_INVALID, f"member {m.name}: must run along exactly one of X/Y/Z (v1)",
                       hint="斜撑暂不支持；拆成轴向杆件或用板件")
    ax = AXES[moving[0]]
    L = diff[moving[0]]
    h_ax = ("y" if ax == "z" else "z")
    other = next(a for a in AXES if a not in (ax, h_ax))
    if m.rotate == 90:
        h_ax, other = other, h_ax
    half = {ax: None, h_ax: s.h / 2, other: s.b / 2}
    box = []
    for i, a in enumerate(AXES):
        if a == ax:
            box += [min(p0[i], p1[i]), max(p0[i], p1[i])]
        else:
            box += [p0[i] - half[a], p0[i] + half[a]]
    return Body(m.name, "member", tuple(box), s.t, s.area * L, s, ax, L, p0, p1, h_ax)


def plate_body(p, ev) -> Body:
    a, b = ev(p.size[0]), ev(p.size[1])
    t = ev(p.thickness)
    c = tuple(ev(v) for v in p.center)
    if min(a, b, t) <= 0:
        raise CadError(SPEC_INVALID, f"plate {p.name}: size/thickness must be > 0")
    half = {"z": {"x": a / 2, "y": b / 2, "z": t / 2}, "y": {"x": a / 2, "z": b / 2, "y": t / 2},
            "x": {"y": a / 2, "z": b / 2, "x": t / 2}}[p.normal]
    box = []
    for i, ax in enumerate(AXES):
        box += [c[i] - half[ax], c[i] + half[ax]]
    return Body(p.name, "plate", tuple(box), t, a * b * t, normal=p.normal, machined=p.machined)


def fillet_size(t_min: float) -> float:
    """角焊缝焊脚：≈0.8·t_min 取系列值，最小 K3。"""
    ok = [k for k in FILLET_SERIES if k <= 0.8 * t_min + 1e-9]
    return float(max(ok) if ok else 3)


def _joint(a: Body, b: Body) -> dict[str, Any] | None:
    ov = {ax: min(a.hi(ax), b.hi(ax)) - max(a.lo(ax), b.lo(ax)) for ax in AXES}
    if any(v < -TOL for v in ov.values()):
        return None
    touching = [ax for ax, v in ov.items() if abs(v) <= TOL]
    if not touching:
        return {"penetration": True, "overlap": {k: round(v, 3) for k, v in ov.items()}}
    if len(touching) > 1:
        return None  # 只有棱边/角点相碰
    n = touching[0]
    dims = [ov[ax] for ax in AXES if ax != n]
    plane = max(a.lo(n), b.lo(n))
    center = tuple(plane if ax == n else (max(a.lo(ax), b.lo(ax)) + min(a.hi(ax), b.hi(ax))) / 2 for ax in AXES)
    # 端接：某杆件端面贴在接触面上，且接触区覆盖整个截面 → 周围焊
    end_of = None
    for m, other in ((a, b), (b, a)):
        if m.kind == "member" and m.axis == n:
            sec = [m.hi(ax) - m.lo(ax) for ax in AXES if ax != n]
            if all(d >= s - TOL for d, s in zip(dims, sec)):
                end_of = m
                break
    if end_of is not None:
        kind, length = "all_around", end_of.section.perimeter
        if end_of.section.kind in ("L", "C", "I", "FB"):
            kind = "all_around"
    else:
        kind, length = "two_sides", 2 * max(dims)
    return {"penetration": False, "normal": n, "contact": tuple(round(d, 3) for d in dims), "center": center,
            "type": kind, "length": round(length, 1), "end_member": end_of.name if end_of else None}


@dataclass
class WeldResult:
    bodies: list[Body]
    welds: list[dict[str, Any]]
    cut_list: list[dict[str, Any]]
    findings: list[dict[str, Any]] = field(default_factory=list)
    open_ends: list[str] = field(default_factory=list)


def resolve_weldment(base, ev) -> WeldResult:
    bodies = [member_body(m, ev) for m in base.members] + [plate_body(p, ev) for p in base.plates]
    user_k = ev(base.weld_size) if base.weld_size is not None else None
    welds: list[dict[str, Any]] = []
    findings: list[dict[str, Any]] = []
    adj: dict[str, set[str]] = {b.name: set() for b in bodies}
    for i in range(len(bodies)):
        for j in range(i + 1, len(bodies)):
            a, b = bodies[i], bodies[j]
            jt = _joint(a, b)
            if jt is None:
                continue
            if jt["penetration"]:
                findings.append({"rule": "W001", "severity": "error", "target": f"{a.name}~{b.name}",
                                 "message": f"{a.name} 与 {b.name} 实体重叠 {jt['overlap']} mm",
                                 "hint": "调整杆件端点（下料长度）使其端面贴合，而不是穿入"})
                continue
            t_min = min(a.t, b.t)
            k = user_k if user_k is not None else fillet_size(t_min)
            welds.append({"id": f"W{len(welds) + 1}", "a": a.name, "b": b.name, "size": k, "t_min": t_min, **jt})
            adj[a.name].add(b.name)
            adj[b.name].add(a.name)
    # 连通性：所有件须焊成一体
    if bodies:
        seen, stack = set(), [bodies[0].name]
        while stack:
            n = stack.pop()
            if n not in seen:
                seen.add(n)
                stack += list(adj[n] - seen)
        for b in bodies:
            if b.name not in seen:
                findings.append({"rule": "W004", "severity": "error", "target": b.name,
                                 "message": f"{b.name} 未与焊接件主体接触（不会被焊上）",
                                 "hint": "检查坐标：焊接件各件必须面贴面接触"})
    # 敞口管端
    open_ends = []
    for m in bodies:
        if m.kind != "member" or not m.section.hollow:
            continue
        for end, val in (("start", m.start), ("end", m.end)):
            pos = val[AXES.index(m.axis)]
            closed = any(w["end_member"] == m.name and abs(w["center"][AXES.index(m.axis)] - pos) <= TOL
                         for w in welds if m.name in (w["a"], w["b"]))
            if not closed:
                open_ends.append(f"{m.name}.{end}")
    if open_ends and not base.end_caps:
        findings.append({"rule": "W003", "severity": "warning", "target": ",".join(open_ends[:6]),
                         "message": f"{len(open_ends)} 处管端敞口（进灰、积水、割手）",
                         "hint": "在管端加封板（plates），或设 end_caps=true 用塑料堵头"})
    for w in welds:
        if w["size"] > 1.2 * w["t_min"] + 1e-9:
            findings.append({"rule": "W002", "severity": "error", "target": w["id"],
                             "message": f"焊脚 K{w['size']:g} 超过较薄件厚度 {w['t_min']:g} 的 1.2 倍，会烧穿",
                             "hint": "减小 weld_size 或加厚板/壁"})
        elif w["size"] < 3:
            findings.append({"rule": "W002", "severity": "warning", "target": w["id"],
                             "message": f"焊脚 K{w['size']:g} < 3 mm，手工焊难以保证"})
    for m in bodies:
        if m.kind == "member" and not m.section.standard:
            findings.append({"rule": "W005", "severity": "warning", "target": m.name,
                             "message": f"{m.section.code} 不是常用规格（GB/T 6728），采购周期可能较长"})
    # 下料清单：按截面 + 长度合并
    cut: dict[tuple[str, float], dict[str, Any]] = {}
    for b in bodies:
        if b.kind == "member":
            key = (b.section.title, round(b.length, 1))
            row = cut.setdefault(key, {"kind": "member", "spec": b.section.title, "section": b.section.code,
                                       "length_mm": round(b.length, 1), "qty": 0, "bodies": [],
                                       "unit_area_mm2": b.section.area})
        else:
            dims = sorted([b.hi(ax) - b.lo(ax) for ax in AXES if ax != b.normal], reverse=True)
            key = (f"t{b.t:g}", tuple(round(d, 1) for d in dims))
            row = cut.setdefault(key, {"kind": "plate", "spec": f"钢板 t{b.t:g}",
                                       "size_mm": [round(d, 1) for d in dims], "qty": 0, "bodies": [],
                                       "machined": b.machined})
        row["qty"] += 1
        row["bodies"].append(b.name)
    cut_list = sorted(cut.values(), key=lambda r: (r["kind"] != "member", r["spec"], -r.get("length_mm", 0)))
    for i, r in enumerate(cut_list, 1):
        r["item"] = i
    return WeldResult(bodies, welds, cut_list, findings, open_ends)


def plate_hole_frame(p, body: Body) -> tuple[str, tuple[float, float]]:
    """板上孔所在面与面内原点（世界坐标）：水平板→top(x,y)，normal=y→front(x,z)，normal=x→right(y,z)。"""
    cx = (body.box[0] + body.box[1]) / 2
    cy = (body.box[2] + body.box[3]) / 2
    cz = (body.box[4] + body.box[5]) / 2
    return {"z": ("top", (cx, cy)), "y": ("front", (cx, cz)), "x": ("right", (cy, cz))}[p.normal]
