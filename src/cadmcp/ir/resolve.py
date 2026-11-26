"""把 PartSpec 解析成具体数值几何（与 CAD 无关）。

输出给：规则检查(rules)、出图规划(detailing)、Mock 适配器，以及真实适配器
（真实适配器用解析值建特征，同时把 spec.parameters 写成全局变量/关系式保持参数化）。
体积/质量是解析估算，真实 CAD 的测量值以 CAD 为准，二者偏差可用于自检。
"""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from typing import Any

from .. import materials, standards
from ..errors import SPEC_INVALID, CadError
from ..expr import evaluate, resolve_parameters
from .common import FACE_NORMAL, REF_PREFIXES, Circle, Polygon, Rect, Slot
from .part import (GEOTOL_SYMBOL, CircularPattern, EdgeFinish, Mount, ExtrudeBase, GridPattern, Groove, Hole, Keyway, PartSpec, Pocket,
                   SheetMetalBase, ShaftBase, Boss, WeldmentBase)
from .weldment import plate_hole_frame, resolve_weldment


@dataclass
class Box:
    xmin: float
    xmax: float
    ymin: float
    ymax: float
    zmin: float
    zmax: float

    @property
    def size(self) -> tuple[float, float, float]:
        return (self.xmax - self.xmin, self.ymax - self.ymin, self.zmax - self.zmin)

    def extent(self, face: str) -> float:
        """沿某面法向的厚度（通孔深度）。"""
        ax = [abs(c) for c in FACE_NORMAL[face]].index(1)
        return self.size[ax]


@dataclass
class ResolvedProfile:
    shape: str
    area: float
    bbox2d: tuple[float, float, float, float]  # umin, umax, vmin, vmax
    outline: list[tuple[float, float]]  # 用于边距计算的多边形（圆/槽为近似）
    perimeter: float
    data: dict[str, Any]


@dataclass
class HoleInstance:
    group: str
    index: int
    face: str
    uv: tuple[float, float]
    xyz: tuple[float, float, float]
    normal: tuple[int, int, int]


@dataclass
class ResolvedHole:
    name: str
    face: str
    type: str
    size_label: str
    nominal: float  # 公称直径（螺纹/螺钉），简单孔 = 孔径
    drill: float  # 实际钻孔直径
    max_dia: float  # 沉孔/沉头最大直径
    depth: float
    through: bool
    thread_depth: float | None
    cbore: tuple[float, float] | None
    csink: float | None
    tolerance: str | None
    instances: list[HoleInstance]
    callout: str
    circular: dict[str, float] | None = None  # {center_u, center_v, pcd, count, start_angle}
    slot: dict[str, float] | None = None  # {width, length(中心距), angle}
    at: float | None = None
    flat_uv: list[tuple[float, float]] | None = None  # 钣金：各孔在展开图上的位置
    plate_box: "Box | None" = None  # 焊接件：孔所在焊接板的包围盒（孔边距按板算）

    def capsule(self, inst: "HoleInstance") -> tuple[tuple[float, float], tuple[float, float], float]:
        """孔在面内的形状：线段两端点 + 半径（圆孔两端点重合）。用于边距/孔距计算。"""
        if self.slot:
            a = math.radians(self.slot["angle"])
            dx, dy = self.slot["length"] / 2 * math.cos(a), self.slot["length"] / 2 * math.sin(a)
            u, v = inst.uv
            return (u - dx, v - dy), (u + dx, v + dy), self.slot["width"] / 2
        return inst.uv, inst.uv, self.max_dia / 2


@dataclass
class ResolvedRef:
    name: str
    kind: str
    origin: tuple[float, float, float]
    rotate: tuple[float, float, float] = (0.0, 0.0, 0.0)
    direction: tuple[float, float, float] | None = None
    auto: bool = False


@dataclass
class ResolvedPart:
    name: str
    part_no: str
    title: str
    material: str
    density: float
    category: str
    surface_treatment: str | None
    base_type: str
    parameters: dict[str, float]
    bbox: Box
    volume_mm3: float
    mass_kg: float
    thickness: float | None
    profile: ResolvedProfile | None
    holes: list[ResolvedHole]
    pockets: list[dict[str, Any]]
    bosses: list[dict[str, Any]]
    finishes: list[dict[str, Any]]
    references: dict[str, ResolvedRef]
    flat_pattern: tuple[float, float] | None = None
    bends: list[dict[str, Any]] = field(default_factory=list)
    shaft_steps: list[tuple[float, float]] = field(default_factory=list)
    properties: dict[str, str] = field(default_factory=dict)
    shaft_tolerances: list[str | None] = field(default_factory=list)
    shaft_chamfer: float = 0.0
    shaft_features: list[dict[str, Any]] = field(default_factory=list)  # keyway / groove
    datums: list[dict[str, Any]] = field(default_factory=list)
    geotols: list[dict[str, Any]] = field(default_factory=list)
    roughness: list[dict[str, Any]] = field(default_factory=list)
    mounts: list[dict[str, Any]] = field(default_factory=list)
    flat_box: tuple[float, float, float, float] | None = None  # 展开图范围 (u0, u1, v0, v1)
    weld_bodies: list[dict[str, Any]] = field(default_factory=list)  # 焊接件：杆件/板件
    welds: list[dict[str, Any]] = field(default_factory=list)
    cut_list: list[dict[str, Any]] = field(default_factory=list)
    weld_findings: list[dict[str, Any]] = field(default_factory=list)
    weld_options: dict[str, Any] = field(default_factory=dict)
    casting: dict[str, Any] = field(default_factory=dict)  # 铸件：工艺参数、技术要求、是否毛坯

    def step_z(self, i: int) -> tuple[float, float]:
        z0 = sum(L for L, _ in self.shaft_steps[:i])
        return z0, z0 + self.shaft_steps[i][0]

    def summary(self) -> dict[str, Any]:
        sx, sy, sz = self.bbox.size
        return {
            "name": self.name, "part_no": self.part_no, "title": self.title,
            "material": self.material, "base": self.base_type,
            "bbox_mm": [round(sx, 3), round(sy, 3), round(sz, 3)],
            "mass_kg": round(self.mass_kg, 4),
            "holes": [{"name": h.name, "count": len(h.instances), "callout": h.callout} for h in self.holes],
            "references": sorted(self.references),
            "parameters": self.parameters,
            **({"flat_pattern_mm": [round(v, 2) for v in self.flat_pattern]} if self.flat_pattern else {}),
            **({"shaft_features": [{"name": f["name"], "callout": f["callout"]} for f in self.shaft_features]}
               if self.shaft_features else {}),
            **({"cut_list": [{k: v for k, v in r.items() if k != "unit_area_mm2"} for r in self.cut_list],
                "welds": {"count": len(self.welds), "total_length_mm": round(sum(w["length"] for w in self.welds)),
                          "sizes": sorted({w["size"] for w in self.welds})}} if self.weld_bodies else {}),
        }

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _fmt(v: float) -> str:
    return f"{v:g}" if abs(v - round(v)) > 1e-9 else str(int(round(v)))


# ---------------- profiles ----------------
def resolve_profile(p, ev) -> ResolvedProfile:
    if isinstance(p, Rect):
        w, h, r = ev(p.width), ev(p.height), ev(p.corner_radius)
        cx, cy = ev(p.center[0]), ev(p.center[1])
        if w <= 0 or h <= 0:
            raise CadError(SPEC_INVALID, f"rect size must be > 0 (got {w}×{h})")
        if r * 2 > min(w, h) + 1e-9:
            raise CadError(SPEC_INVALID, f"corner_radius {r} too large for {w}×{h}")
        outline = [(cx - w / 2, cy - h / 2), (cx + w / 2, cy - h / 2), (cx + w / 2, cy + h / 2), (cx - w / 2, cy + h / 2)]
        return ResolvedProfile("rect", w * h - (4 - math.pi) * r * r, (cx - w / 2, cx + w / 2, cy - h / 2, cy + h / 2),
                               outline, 2 * (w + h) - 8 * r + 2 * math.pi * r,
                               {"width": w, "height": h, "center": (cx, cy), "corner_radius": r})
    if isinstance(p, Circle):
        d = ev(p.diameter)
        cx, cy = ev(p.center[0]), ev(p.center[1])
        if d <= 0:
            raise CadError(SPEC_INVALID, "circle diameter must be > 0")
        n = 48
        outline = [(cx + d / 2 * math.cos(2 * math.pi * i / n), cy + d / 2 * math.sin(2 * math.pi * i / n)) for i in range(n)]
        return ResolvedProfile("circle", math.pi * d * d / 4, (cx - d / 2, cx + d / 2, cy - d / 2, cy + d / 2),
                               outline, math.pi * d, {"diameter": d, "center": (cx, cy)})
    if isinstance(p, Slot):
        L, W, a = ev(p.length), ev(p.width), math.radians(ev(p.angle))
        cx, cy = ev(p.center[0]), ev(p.center[1])
        if L < W or W <= 0:
            raise CadError(SPEC_INVALID, f"slot length {L} must be ≥ width {W} > 0")
        hx, hy = (L - W) / 2 * math.cos(a), (L - W) / 2 * math.sin(a)
        ends = [(cx - hx, cy - hy), (cx + hx, cy + hy)]
        bb = (min(e[0] for e in ends) - W / 2, max(e[0] for e in ends) + W / 2,
              min(e[1] for e in ends) - W / 2, max(e[1] for e in ends) + W / 2)
        nx, ny = -math.sin(a) * W / 2, math.cos(a) * W / 2
        outline = [(ends[0][0] - nx, ends[0][1] - ny), (ends[1][0] - nx, ends[1][1] - ny),
                   (ends[1][0] + nx, ends[1][1] + ny), (ends[0][0] + nx, ends[0][1] + ny)]
        return ResolvedProfile("slot", (L - W) * W + math.pi * W * W / 4, bb, outline, 2 * (L - W) + math.pi * W,
                               {"length": L, "width": W, "center": (cx, cy), "angle": math.degrees(a)})
    if isinstance(p, Polygon):
        pts = [(ev(x), ev(y)) for x, y in p.points]
        area2 = sum(pts[i][0] * pts[(i + 1) % len(pts)][1] - pts[(i + 1) % len(pts)][0] * pts[i][1] for i in range(len(pts)))
        if abs(area2) < 1e-9:
            raise CadError(SPEC_INVALID, "polygon has zero area")
        per = sum(math.dist(pts[i], pts[(i + 1) % len(pts)]) for i in range(len(pts)))
        xs, ys = [q[0] for q in pts], [q[1] for q in pts]
        return ResolvedProfile("polygon", abs(area2) / 2, (min(xs), max(xs), min(ys), max(ys)), pts, per, {"points": pts})
    raise CadError(SPEC_INVALID, f"unknown profile {p!r}")


def point_in_polygon(pt: tuple[float, float], poly: list[tuple[float, float]]) -> bool:
    x, y = pt
    inside = False
    for i in range(len(poly)):
        (x1, y1), (x2, y2) = poly[i], poly[(i + 1) % len(poly)]
        if (y1 > y) != (y2 > y) and x < (x2 - x1) * (y - y1) / (y2 - y1) + x1:
            inside = not inside
    return inside


def dist_to_outline(pt: tuple[float, float], poly: list[tuple[float, float]]) -> float:
    """到轮廓的有符号距离：内部为正。"""
    best = math.inf
    for i in range(len(poly)):
        (x1, y1), (x2, y2) = poly[i], poly[(i + 1) % len(poly)]
        dx, dy = x2 - x1, y2 - y1
        L2 = dx * dx + dy * dy
        t = 0.0 if L2 == 0 else max(0.0, min(1.0, ((pt[0] - x1) * dx + (pt[1] - y1) * dy) / L2))
        best = min(best, math.dist(pt, (x1 + t * dx, y1 + t * dy)))
    return best if point_in_polygon(pt, poly) else -best


# ---------------- face mapping ----------------
def face_plane(face: str, box: Box, at: float | None = None) -> float:
    """面所在平面在法向轴上的坐标；at 指定非外表面（台阶面、L 形内侧面）。"""
    if at is not None:
        return at
    return {"top": box.zmax, "bottom": box.zmin, "front": box.ymin, "back": box.ymax,
            "left": box.xmin, "right": box.xmax}[face]


def face_depth(face: str, box: Box, at: float | None = None) -> float:
    """从该面沿法向反方向穿透整个零件的深度（通孔深度）。"""
    c = face_plane(face, box, at)
    return {"top": c - box.zmin, "bottom": box.zmax - c, "front": box.ymax - c, "back": c - box.ymin,
            "left": box.xmax - c, "right": c - box.xmin}[face]


def face_point(face: str, uv: tuple[float, float], box: Box, at: float | None = None) -> tuple[float, float, float]:
    u, v = uv
    c = face_plane(face, box, at)
    return {
        "top": (u, v, c), "bottom": (u, v, c),
        "front": (u, c, v), "back": (u, c, v),
        "left": (c, u, v), "right": (c, u, v),
    }[face]


SIDE_AXIS = {"front": (1, -1), "back": (1, 1), "left": (0, -1), "right": (0, 1)}  # (截面中法向轴 0=x 1=y, 方向)


def profile_chord(outline: list[tuple[float, float]], face: str, u: float, plane: float) -> float | None:
    """挤出件侧面上的孔：沿法向从面向内，穿过实体的长度（截面多边形与直线求交）。"""
    ax, sgn = SIDE_AXIS[face]
    hits = []
    for i in range(len(outline)):
        a, b = outline[i], outline[(i + 1) % len(outline)]
        ua, ub = a[1 - ax], b[1 - ax]
        if (ua > u) != (ub > u):
            t = (u - ua) / (ub - ua)
            hits.append(a[ax] + t * (b[ax] - a[ax]))
    hits.sort()
    for k in range(0, len(hits) - 1, 2):
        lo, hi = hits[k], hits[k + 1]
        if sgn > 0 and abs(hi - plane) < 1e-6:
            return hi - lo
        if sgn < 0 and abs(lo - plane) < 1e-6:
            return hi - lo
    return None


def side_face_range(outline: list[tuple[float, float]], face: str, plane: float) -> tuple[float, float] | None:
    """侧面在截面上的真实范围（与该平面共线的轮廓边的并集范围）。"""
    ax, _ = SIDE_AXIS[face]
    us = []
    for i in range(len(outline)):
        a, b = outline[i], outline[(i + 1) % len(outline)]
        if abs(a[ax] - plane) < 1e-6 and abs(b[ax] - plane) < 1e-6:
            us += [a[1 - ax], b[1 - ax]]
    return (min(us), max(us)) if us else None


def face_outline(face: str, box: Box, profile: ResolvedProfile | None,
                 at: float | None = None, extrude: bool = False) -> list[tuple[float, float]]:
    if face in ("top", "bottom") and profile is not None:
        return profile.outline
    if extrude and profile is not None and profile.shape == "polygon" and face in SIDE_AXIS:
        rng = side_face_range(profile.outline, face, face_plane(face, box, at))
        if rng:
            return [(rng[0], box.zmin), (rng[1], box.zmin), (rng[1], box.zmax), (rng[0], box.zmax)]
    if face in ("front", "back"):
        a, b, c, d = box.xmin, box.xmax, box.zmin, box.zmax
    elif face in ("left", "right"):
        a, b, c, d = box.ymin, box.ymax, box.zmin, box.zmax
    else:
        a, b, c, d = box.xmin, box.xmax, box.ymin, box.ymax
    return [(a, c), (b, c), (b, d), (a, d)]


# ---------------- holes ----------------
def _positions(h: Hole, ev) -> list[tuple[float, float]]:
    pts = [(ev(x), ev(y)) for x, y in h.positions]
    pat = h.pattern
    if isinstance(pat, GridPattern):
        nx, ny = int(round(ev(pat.nx))), int(round(ev(pat.ny)))
        if nx < 1 or ny < 1:
            raise CadError(SPEC_INVALID, f"hole {h.name}: grid counts must be ≥ 1")
        ox, oy, dx, dy = ev(pat.origin[0]), ev(pat.origin[1]), ev(pat.dx), ev(pat.dy)
        pts += [(ox + i * dx, oy + j * dy) for j in range(ny) for i in range(nx)]
    elif isinstance(pat, CircularPattern):
        n = int(round(ev(pat.count)))
        cx, cy, r, a0 = ev(pat.center[0]), ev(pat.center[1]), ev(pat.radius), ev(pat.start_angle)
        pts += [(cx + r * math.cos(math.radians(a0 + 360 * i / n)), cy + r * math.sin(math.radians(a0 + 360 * i / n)))
                for i in range(n)]
    return pts


def expand_mount(m: Mount, ev) -> tuple[list[Hole], dict[str, Any]]:
    """把安装接口展开成孔特征（坐标在面内 uv，已旋转平移）。"""
    from ..library import Library
    lib = Library.default()
    code = m.item.partition("?")[0]
    raw = lib.items.get(code)
    if raw is None:
        raise CadError(SPEC_INVALID, f"mount {m.name}: library item {code!r} not found", hint="用 cad_search_library 查询")
    if "mount" not in raw:
        raise CadError(SPEC_INVALID, f"mount {m.name}: {code} has no registered mounting interface",
                       hint="在 library.yaml 中为该件登记 mount（frame/screw/holes/pilot）")
    lp = lib.get(m.item)
    mi = raw["mount"]
    lev = lambda v: evaluate(v, lp.parameters)  # noqa: E731
    cx, cy, rot = ev(m.center[0]), ev(m.center[1]), ev(m.rotate)
    ca, sa = math.cos(math.radians(rot)), math.sin(math.radians(rot))
    if m.face == "bottom":  # 底面从下方看，面内 x 镜像
        sa = -sa

    def tr(x, y):
        return [round(cx + ca * x - sa * y, 6), round(cy + sa * x + ca * y, 6)]

    holes_spec = mi["holes"]
    common = {"face": m.face, "size": mi["screw"], "type": m.screw_hole}
    if m.screw_hole == "tapped":
        common.update(depth=m.depth, thread_depth=m.thread_depth)
    if holes_spec.get("kind") == "circular":
        r, n, a0 = lev(holes_spec["radius"]), lev(holes_spec["count"]), lev(holes_spec.get("start_angle", 0))
        screw = Hole(name=f"{m.name}_Screw", pattern=CircularPattern(center=tuple(tr(0, 0)), radius=r, count=n,
                                                                     start_angle=a0 + (rot if m.face == "top" else -rot)),
                     **common)
    elif holes_spec.get("kind") == "linear":
        n = int(lev(holes_spec["count"]))
        s0, pitch = lev(holes_spec.get("start", 0)), lev(holes_spec["pitch"])
        screw = Hole(name=f"{m.name}_Screw", positions=[tr(s0 + i * pitch, 0) for i in range(n)], **common)
    else:
        screw = Hole(name=f"{m.name}_Screw", positions=[tr(lev(x), lev(y)) for x, y in holes_spec["positions"]], **common)
    out = [screw]
    if m.pilot and mi.get("pilot"):
        out.append(Hole(name=f"{m.name}_Pilot", face=m.face, type="simple", size=lev(mi["pilot"]),
                        tolerance=mi.get("pilot_tolerance"), positions=[tr(0, 0)]))
    info = {"name": m.name, "item": code, "item_ref": m.item, "frame": mi["frame"], "face": m.face,
            "center": (cx, cy), "rotate": rot, "screw": mi["screw"]}
    return out, info


def resolve_hole(h: Hole, ev, box: Box, section: list[tuple[float, float]] | None = None) -> ResolvedHole:
    through = h.depth == "through"
    at = ev(h.at) if h.at is not None else None
    extent = face_depth(h.face, box, at)
    if through and section is not None and h.face in SIDE_AXIS:
        uvs0 = _positions(h, ev)
        chords = [profile_chord(section, h.face, uv[0], face_plane(h.face, box, at)) for uv in uvs0]
        if chords and all(c is not None for c in chords):
            extent = max(chords)
    depth = extent if through else ev(h.depth)
    size = h.size
    cbore = csink = None
    thread_depth = None
    slot = None
    if h.type == "slot":
        w = standards.clearance(size) if isinstance(size, str) and standards.is_metric(size) else ev(size)
        L = ev(h.slot_length)
        if L <= 0 or w <= 0:
            raise CadError(SPEC_INVALID, f"slot {h.name}: width and slot_length must be > 0")
        slot = {"width": w, "length": L, "angle": ev(h.slot_angle)}
        nominal = drill = maxd = w
        label = f"腰型孔 宽{_fmt(w)} 中心距{_fmt(L)}"
    elif h.type == "simple":
        d = ev(size) if not isinstance(size, str) or not standards.is_metric(size) else standards.clearance(size)
        nominal = drill = maxd = d
        label = f"Φ{_fmt(d)}{h.tolerance or ''}"
    else:
        nominal = float(str(size).upper().lstrip("M"))
        if h.type == "tapped":
            drill = standards.tap_drill(size)
            thread_depth = ev(h.thread_depth) if h.thread_depth is not None else (depth if through else None)
            if thread_depth is None:
                thread_depth = max(0.0, depth - 0.5 * nominal)
            label = str(size).upper()
        else:
            drill = standards.clearance(size)
            label = f"Φ{_fmt(drill)}"
        maxd = drill
        if h.type == "counterbore":
            cbore = standards.counterbore(size)
            maxd = cbore[0]
        elif h.type == "countersink":
            csink = standards.countersink(size)
            maxd = csink
    if depth <= 0:
        raise CadError(SPEC_INVALID, f"hole {h.name}: depth must be > 0")
    uvs = _positions(h, ev)
    n = len(uvs)
    insts = [HoleInstance(h.name, i, h.face, uv, face_point(h.face, uv, box, at), FACE_NORMAL[h.face]) for i, uv in enumerate(uvs)]
    prefix = f"{n}×" if n > 1 else ""
    depth_txt = "通" if through else f"↧{_fmt(depth)}"
    if h.type == "tapped":
        callout = f"{prefix}{label}-6H ↧{_fmt(thread_depth)}" if not through else f"{prefix}{label}-6H 通"
        if not through:
            callout += f" 底孔Φ{_fmt(drill)}↧{_fmt(depth)}"
    elif h.type == "counterbore":
        callout = f"{prefix}Φ{_fmt(drill)} {depth_txt} ⌴Φ{_fmt(cbore[0])}↧{_fmt(cbore[1])}"
    elif h.type == "countersink":
        callout = f"{prefix}Φ{_fmt(drill)} {depth_txt} ⌵Φ{_fmt(csink)}×90°"
    else:
        callout = f"{prefix}{label} {depth_txt}"
    circ = None
    if isinstance(h.pattern, CircularPattern) and not h.positions:
        circ = {"center_u": ev(h.pattern.center[0]), "center_v": ev(h.pattern.center[1]),
                "pcd": 2 * ev(h.pattern.radius), "count": len(uvs), "start_angle": ev(h.pattern.start_angle)}
        callout += " 均布"
    return ResolvedHole(h.name, h.face, h.type, label, nominal, drill, maxd, depth, through, thread_depth,
                        cbore, csink, h.tolerance, insts, callout, circ, slot, at)


def _sheet_hole(rh: ResolvedHole, bends: list[dict[str, Any]], t: float, box: Box) -> None:
    """钣金孔：翻边上的通孔只穿板厚；计算展开图位置（外形尺寸法：距虚拟尖角 z 处 → 展开坐标）。"""
    by_edge = {b["edge"]: b for b in bends}
    if rh.face in ("top", "bottom"):
        rh.flat_uv = [(i.xyz[0], i.xyz[1]) for i in rh.instances]
        return
    b = by_edge.get(rh.face)
    if b is None:
        raise CadError(SPEC_INVALID, f"hole {rh.name}: sheet metal has no flange on {rh.face}",
                       hint=f"翻边所在边：{sorted(by_edge)}")
    if rh.through:
        rh.depth = t
    ossb = (b["radius"] + t) * math.tan(math.radians(b["angle"]) / 2)
    ba = b["bend_allowance"]
    flat = []
    for i in rh.instances:
        x, y, z = i.xyz
        h = z if b["direction"] == "up" else t - z  # 距底板外表面的高度
        if h < t + b["radius"]:
            raise CadError(SPEC_INVALID, f"hole {rh.name}: position z={z:g} is inside the bend zone",
                           hint=f"翻边孔中心高度需 ≥ {t + b['radius'] + rh.max_dia / 2:g}")
        d = 2 * ossb - ba - h  # 相对底板外形边线的展开偏移
        flat.append({"front": (x, box.ymin + d), "back": (x, box.ymax - d),
                     "left": (box.xmin + d, y), "right": (box.xmax - d, y)}[rh.face])
    rh.flat_uv = flat


def _casting_info(spec: PartSpec) -> dict[str, Any]:
    from .. import casting
    blank = spec.casting.blank_of is not None
    return {**casting.params(spec), "blank": blank, "blank_of": spec.casting.blank_of,
            "notes": casting.notes(spec, blank)}


def _hole_volume(h: ResolvedHole) -> float:
    n = len(h.instances)
    if h.slot:
        w, L = h.slot["width"], h.slot["length"]
        return n * (L * w + math.pi * w * w / 4) * h.depth
    v = math.pi * h.drill ** 2 / 4 * h.depth
    if h.cbore:
        D, d = h.cbore
        v += math.pi * (D ** 2 - h.drill ** 2) / 4 * d
    if h.csink:
        hh = (h.csink - h.drill) / 2
        v += math.pi / 12 * hh * (h.csink ** 2 + h.csink * h.drill + h.drill ** 2) - math.pi * h.drill ** 2 / 4 * hh
    return n * v


# ---------------- part ----------------
def resolve_part(spec: PartSpec) -> ResolvedPart:
    params = resolve_parameters(spec.parameters)

    def ev(v):
        return evaluate(v, params)

    dens = materials.density(spec.material)
    base = spec.base
    profile = None
    thickness = None
    flat = None
    bends: list[dict[str, Any]] = []
    steps: list[tuple[float, float]] = []
    shaft_chamfer = 0.0
    flat_box = None

    if isinstance(base, ExtrudeBase):
        profile = resolve_profile(base.profile, ev)
        thickness = ev(base.thickness)
        if thickness <= 0:
            raise CadError(SPEC_INVALID, "thickness must be > 0")
        u0, u1, v0, v1 = profile.bbox2d
        box = Box(u0, u1, v0, v1, 0.0, thickness)
        volume = profile.area * thickness
    elif isinstance(base, ShaftBase):
        steps = [(ev(s.length), ev(s.diameter)) for s in base.steps]
        if any(L <= 0 or d <= 0 for L, d in steps):
            raise CadError(SPEC_INVALID, "shaft step length/diameter must be > 0")
        R = max(d for _, d in steps) / 2
        box = Box(-R, R, -R, R, 0.0, sum(L for L, _ in steps))
        volume = sum(math.pi * d * d / 4 * L for L, d in steps)
        shaft_chamfer = ev(base.chamfer)
        for d in (steps[0][1], steps[-1][1]):
            volume -= math.pi * d * shaft_chamfer ** 2 / 2  # 端部 45° 倒角近似
    elif isinstance(base, SheetMetalBase):
        profile = resolve_profile(base.profile, ev)
        t = thickness = ev(base.thickness)
        r = ev(base.bend_radius) if base.bend_radius is not None else t
        u0, u1, v0, v1 = profile.bbox2d
        box = Box(u0, u1, v0, v1, 0.0, t)
        volume = profile.area * t
        fu0, fu1, fv0, fv1 = u0, u1, v0, v1
        for f in base.flanges:
            L, ang = ev(f.length), ev(f.angle)
            if L <= t + r:
                raise CadError(SPEC_INVALID, f"flange on {f.edge}: length {L} must exceed thickness+bend radius {t + r}")
            edge_len = (u1 - u0) if f.edge in ("front", "back") else (v1 - v0)
            ba = math.pi / 180 * ang * (r + base.k_factor * t)
            ossb = (r + t) * math.tan(math.radians(ang) / 2)  # 外退让量（外形尺寸到虚拟尖角）
            # 外形尺寸法：展开 = 底边外形 + 翻边外形 − 2·OSSB + BA；底边外形已计入 profile，这里加翻边部分
            flat_add = (L - ossb) + ba - ossb
            volume += edge_len * (L - t) * t
            if f.direction == "up":
                box.zmax = max(box.zmax, L)
            else:
                box.zmin = min(box.zmin, -(L - t))
            if f.edge == "front":
                fv0 -= flat_add
            elif f.edge == "back":
                fv1 += flat_add
            elif f.edge == "left":
                fu0 -= flat_add
            else:
                fu1 += flat_add
            # 展开图上折弯中心线位置（展开坐标 = 底板坐标延伸）
            line = {"front": ("v", v0 + ossb - ba / 2), "back": ("v", v1 - ossb + ba / 2),
                    "left": ("u", u0 + ossb - ba / 2), "right": ("u", u1 - ossb + ba / 2)}[f.edge]
            bends.append({"edge": f.edge, "angle": ang, "radius": r, "length": L, "bend_allowance": round(ba, 3),
                          "direction": f.direction, "flat_axis": line[0], "flat_pos": round(line[1], 3),
                          "k_factor": base.k_factor})
        flat = (fu1 - fu0, fv1 - fv0)
        flat_box = (fu0, fu1, fv0, fv1)
    elif isinstance(base, WeldmentBase):
        if spec.features:
            raise CadError(SPEC_INVALID, "weldment: put holes on plates (plates[].holes), not in features",
                           hint="焊后加工孔写在对应焊接板的 holes 里")
        wr = resolve_weldment(base, ev)
        bx = [b.box for b in wr.bodies]
        box = Box(min(b[0] for b in bx), max(b[1] for b in bx), min(b[2] for b in bx), max(b[3] for b in bx),
                  min(b[4] for b in bx), max(b[5] for b in bx))
        volume = sum(b.volume for b in wr.bodies)
    else:  # pragma: no cover
        raise CadError(SPEC_INVALID, "unknown base type")

    pockets, bosses, finishes, holes = [], [], [], []
    shaft_feats: list[dict[str, Any]] = []
    for f in spec.features:
        if isinstance(f, (Keyway, Groove)):
            if not isinstance(base, ShaftBase):
                raise CadError(SPEC_INVALID, f"{f.feature} {f.name}: only valid on a shaft base")
            if not 0 <= f.step < len(steps):
                raise CadError(SPEC_INVALID, f"{f.name}: step {f.step} out of range (0..{len(steps) - 1})")
            sz0 = sum(L for L, _ in steps[:f.step])
            L, d = steps[f.step]
            if isinstance(f, Keyway):
                b, _, t1 = standards.keyway(d)
                b = ev(f.width) if f.width is not None else b
                t1 = ev(f.depth) if f.depth is not None else t1
                kl = ev(f.length)
                off = ev(f.offset) if f.offset is not None else (L - kl) / 2
                volume -= ((kl - b) * b + math.pi * b * b / 4) * t1
                shaft_feats.append({"kind": "keyway", "name": f.name, "step": f.step, "d": d, "width": b,
                                    "depth": t1, "length": kl, "z0": sz0 + off, "z1": sz0 + off + kl,
                                    "offset": off, "angle": ev(f.angle),
                                    "callout": f"键槽 {b:g}N9×{kl:g} 深{t1:g}",
                                    "section_dim": f"{d - t1:g}"})
            else:
                if f.type == "circlip":
                    d2, m = standards.circlip_groove(d)
                    label = f"挡圈槽 Φ{d2:g}×{m:g}（GB/T 894-{d:g}）"
                else:
                    m = ev(f.width) if f.width is not None else 2.0
                    d2 = d - 2 * (ev(f.depth) if f.depth is not None else 0.5)
                    label = f"退刀槽 {m:g}×{(d - d2) / 2:g}"
                pos = {"start": 0.0, "end": L - m}.get(f.position) if isinstance(f.position, str) else ev(f.position)
                if f.type == "circlip" and f.position == "end":
                    pos = L - m - 1.0 if L - m - 1.0 > 0 else L - m  # 挡圈槽离轴段端面留 1mm 以上（挡圈承载边）
                volume -= math.pi / 4 * (d * d - d2 * d2) * m
                shaft_feats.append({"kind": "groove", "type": f.type, "name": f.name, "step": f.step, "d": d,
                                    "d2": d2, "width": m, "z0": sz0 + pos, "z1": sz0 + pos + m, "offset": pos,
                                    "callout": label})
    for f in spec.features:
        if isinstance(f, Boss):
            pr = resolve_profile(f.profile, ev)
            hgt = ev(f.height)
            if f.face != "top":
                raise CadError(SPEC_INVALID, f"boss {f.name}: only face 'top' is supported in v0")
            volume += pr.area * hgt
            z0 = ev(f.at) if f.at is not None else box.zmax
            bosses.append({"name": f.name, "face": f.face, "profile": pr, "height": hgt, "z0": z0})
    for b in bosses:
        box.zmax = max(box.zmax, b["z0"] + b["height"])

    mounts: list[dict[str, Any]] = []
    feats: list[Any] = []
    for f in spec.features:
        if isinstance(f, Mount):
            hs, info = expand_mount(f, ev)
            feats += hs
            mounts.append(info)
        else:
            feats.append(f)
    if isinstance(base, WeldmentBase):
        bodies = {b.name: b for b in wr.bodies}
        for pl in base.plates:
            body = bodies[pl.name]
            face, (ou, ov) = plate_hole_frame(pl, body)
            pbox = Box(*body.box)
            for ph in pl.holes:
                try:
                    hs = Hole(name=f"{pl.name}_{ph.name}", face=face, type=ph.type, size=ph.size,
                              tolerance=ph.tolerance, at=face_plane(face, pbox),
                              positions=[(ou + ev(u), ov + ev(v)) for u, v in ph.positions])
                except ValueError as e:
                    raise CadError(SPEC_INVALID, f"plate {pl.name} hole {ph.name}: {e}") from None
                rh = resolve_hole(hs, ev, pbox)
                rh.plate_box = pbox
                volume -= _hole_volume(rh)
                holes.append(rh)
    for f in feats:
        if isinstance(f, Hole):
            section = profile.outline if (isinstance(base, (ExtrudeBase, SheetMetalBase)) and profile) else None
            rh = resolve_hole(f, ev, box, section if isinstance(base, ExtrudeBase) else None)
            if isinstance(base, SheetMetalBase):
                _sheet_hole(rh, bends, thickness, box)
            volume -= _hole_volume(rh)
            holes.append(rh)
        elif isinstance(f, Pocket):
            pr = resolve_profile(f.profile, ev)
            at = ev(f.at) if f.at is not None else None
            depth = face_depth(f.face, box, at) if f.depth == "through" else ev(f.depth)
            volume -= pr.area * depth
            pockets.append({"name": f.name, "face": f.face, "profile": pr, "depth": depth, "through": f.depth == "through",
                            "at": at})
        elif isinstance(f, EdgeFinish):
            s = ev(f.size)
            if thickness is None:
                raise CadError(SPEC_INVALID, f"{f.feature} {f.name}: edge finishes need an extrude/sheet base in v0")
            if f.edges == "outer_vertical":
                if profile and profile.shape == "rect":
                    corner = (1 - math.pi / 4) * s * s if f.feature == "fillet" else s * s / 2
                    volume -= 4 * corner * thickness
            else:
                per = profile.perimeter if profile else 0
                volume -= per * ((1 - math.pi / 4) * s * s if f.feature == "fillet" else s * s / 2)
            finishes.append({"name": f.name, "type": f.feature, "edges": f.edges, "size": s})

    if volume <= 0:
        raise CadError(SPEC_INVALID, f"resulting volume is not positive ({volume:.1f} mm³)",
                       hint="check pocket/hole depths against thickness")

    refs: dict[str, ResolvedRef] = {"CS_Origin": ResolvedRef("CS_Origin", "csys", (0.0, 0.0, 0.0), auto=True)}
    if isinstance(base, ShaftBase):
        refs["AX_Main"] = ResolvedRef("AX_Main", "axis", (0.0, 0.0, 0.0), direction=(0.0, 0.0, 1.0), auto=True)
        refs["MP_End0"] = ResolvedRef("MP_End0", "plane", (0.0, 0.0, box.zmin), direction=(0.0, 0.0, -1.0), auto=True)
        refs["MP_End1"] = ResolvedRef("MP_End1", "plane", (0.0, 0.0, box.zmax), direction=(0.0, 0.0, 1.0), auto=True)
    else:
        zt, zb = (thickness, 0.0) if thickness is not None else (box.zmax, box.zmin)
        refs["MP_Top"] = ResolvedRef("MP_Top", "plane", (0.0, 0.0, zt), direction=(0.0, 0.0, 1.0), auto=True)
        refs["MP_Bottom"] = ResolvedRef("MP_Bottom", "plane", (0.0, 0.0, zb), direction=(0.0, 0.0, -1.0), auto=True)
    for m in mounts:
        o = face_point(m["face"], m["center"], box)
        rot = (0.0, 0.0, m["rotate"]) if m["face"] == "top" else (180.0, 0.0, -m["rotate"])
        refs[f"CS_{m['name']}"] = ResolvedRef(f"CS_{m['name']}", "csys", o, rot, auto=True)
    for r in spec.references:
        kind = REF_PREFIXES[r.name[:3]]
        if kind == "plane":
            n = FACE_NORMAL[r.face]
            pt = face_point(r.face, (0.0, 0.0), box)
            refs[r.name] = ResolvedRef(r.name, kind, pt, direction=tuple(float(c) for c in n))
        elif kind == "axis" and r.hole:
            rh = next(h for h in holes if h.name == r.hole)
            if r.index >= len(rh.instances):
                raise CadError(SPEC_INVALID, f"{r.name}: hole {r.hole} has only {len(rh.instances)} instances")
            inst = rh.instances[r.index]
            refs[r.name] = ResolvedRef(r.name, kind, inst.xyz, direction=tuple(float(-c) for c in inst.normal))
        else:
            origin = tuple(ev(c) for c in r.origin)  # type: ignore[union-attr]
            direction = tuple(ev(c) for c in r.direction) if r.direction else None
            refs[r.name] = ResolvedRef(r.name, kind, origin, tuple(ev(c) for c in r.rotate), direction)

    def _target(tgt: str, what: str) -> tuple[str, str]:
        kind, _, ref = tgt.partition(":")
        ok = (kind == "face" and ref in FACE_NORMAL and not isinstance(base, ShaftBase)) or \
             (kind == "hole" and any(h.name == ref for h in holes)) or \
             (kind == "step" and isinstance(base, ShaftBase) and ref.isdigit() and int(ref) < len(steps))
        if not ok:
            raise CadError(SPEC_INVALID, f"{what}: invalid target {tgt!r}",
                           hint="face:<top|bottom|front|back|left|right>（非轴类）、hole:<孔名>、step:<段号>（轴类）")
        return kind, ref

    datums = [{"name": d.name, "target": d.target, "kind": _target(d.target, f"datum {d.name}")[0]}
              for d in spec.datums]
    geotols = []
    for g in spec.tolerances:
        kind, _ = _target(g.target, f"tolerance {g.type}")
        val = ev(g.value)
        dia = g.type in ("position", "coaxiality") and kind in ("hole", "step")
        geotols.append({"type": g.type, "value": val, "target": g.target, "kind": kind, "datums": list(g.datums),
                        "symbol": GEOTOL_SYMBOL[g.type], "zone": f"{'Φ' if dia else ''}{val:g}",
                        "text": " | ".join([GEOTOL_SYMBOL[g.type], f"{'Φ' if dia else ''}{val:g}", *g.datums])})
    rough = [{"target": r.target, "kind": _target(r.target, "roughness")[0], "ra": ev(r.ra)} for r in spec.roughness]

    return ResolvedPart(
        datums=datums, geotols=geotols, roughness=rough, mounts=mounts, flat_box=flat_box,
        name=spec.name, part_no=spec.part_no, title=spec.title, material=spec.material, density=dens,
        category=spec.category, surface_treatment=spec.surface_treatment, base_type=base.type,
        parameters=params, bbox=box, volume_mm3=volume, mass_kg=volume * 1e-9 * dens, thickness=thickness,
        profile=profile, holes=holes, pockets=pockets, bosses=bosses, finishes=finishes, references=refs,
        flat_pattern=flat, bends=bends, shaft_steps=steps, properties=dict(spec.properties),
        shaft_tolerances=[s.tolerance for s in base.steps] if isinstance(base, ShaftBase) else [],
        shaft_chamfer=shaft_chamfer, shaft_features=shaft_feats,
        **({"casting": _casting_info(spec)} if spec.casting else {}),
        **({"weld_bodies": [asdict(b) for b in wr.bodies], "welds": wr.welds, "cut_list": wr.cut_list,
            "weld_findings": wr.findings,
            "weld_options": {"end_caps": base.end_caps, "open_ends": wr.open_ends,
                             "stress_relief": base.stress_relief, "process": base.weld_process}}
           if isinstance(base, WeldmentBase) else {}),
    )
