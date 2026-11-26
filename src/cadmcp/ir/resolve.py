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
from .part import (CircularPattern, EdgeFinish, ExtrudeBase, GridPattern, Hole, PartSpec, Pocket,
                   SheetMetalBase, ShaftBase, Boss)


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
def face_point(face: str, uv: tuple[float, float], box: Box) -> tuple[float, float, float]:
    u, v = uv
    return {
        "top": (u, v, box.zmax), "bottom": (u, v, box.zmin),
        "front": (u, box.ymin, v), "back": (u, box.ymax, v),
        "left": (box.xmin, u, v), "right": (box.xmax, u, v),
    }[face]


def face_outline(face: str, box: Box, profile: ResolvedProfile | None) -> list[tuple[float, float]]:
    if face in ("top", "bottom") and profile is not None:
        return profile.outline
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


def resolve_hole(h: Hole, ev, box: Box) -> ResolvedHole:
    through = h.depth == "through"
    extent = box.extent(h.face)
    depth = extent if through else ev(h.depth)
    size = h.size
    cbore = csink = None
    thread_depth = None
    if h.type == "simple":
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
    insts = [HoleInstance(h.name, i, h.face, uv, face_point(h.face, uv, box), FACE_NORMAL[h.face]) for i, uv in enumerate(uvs)]
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
                        cbore, csink, h.tolerance, insts, callout, circ)


def _hole_volume(h: ResolvedHole) -> float:
    n = len(h.instances)
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
            flat_add = (L - t - r) + ba - (t + r) * 0  # 平直段+展开弧长（外形尺寸法近似）
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
            bends.append({"edge": f.edge, "angle": ang, "radius": r, "length": L, "bend_allowance": round(ba, 3)})
        flat = (fu1 - fu0, fv1 - fv0)
    else:  # pragma: no cover
        raise CadError(SPEC_INVALID, "unknown base type")

    pockets, bosses, finishes, holes = [], [], [], []
    for f in spec.features:
        if isinstance(f, Boss):
            pr = resolve_profile(f.profile, ev)
            hgt = ev(f.height)
            if f.face != "top":
                raise CadError(SPEC_INVALID, f"boss {f.name}: only face 'top' is supported in v0")
            volume += pr.area * hgt
            box.zmax += 0  # 先记录，统一在最后扩展
            bosses.append({"name": f.name, "face": f.face, "profile": pr, "height": hgt, "z0": box.zmax})
    for b in bosses:
        box.zmax = max(box.zmax, b["z0"] + b["height"])

    for f in spec.features:
        if isinstance(f, Hole):
            rh = resolve_hole(f, ev, box)
            volume -= _hole_volume(rh)
            holes.append(rh)
        elif isinstance(f, Pocket):
            pr = resolve_profile(f.profile, ev)
            depth = box.extent(f.face) if f.depth == "through" else ev(f.depth)
            volume -= pr.area * depth
            pockets.append({"name": f.name, "face": f.face, "profile": pr, "depth": depth, "through": f.depth == "through"})
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
        refs["MP_Top"] = ResolvedRef("MP_Top", "plane", (0.0, 0.0, thickness), direction=(0.0, 0.0, 1.0), auto=True)
        refs["MP_Bottom"] = ResolvedRef("MP_Bottom", "plane", (0.0, 0.0, 0.0), direction=(0.0, 0.0, -1.0), auto=True)
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

    return ResolvedPart(
        name=spec.name, part_no=spec.part_no, title=spec.title, material=spec.material, density=dens,
        category=spec.category, surface_treatment=spec.surface_treatment, base_type=base.type,
        parameters=params, bbox=box, volume_mm3=volume, mass_kg=volume * 1e-9 * dens, thickness=thickness,
        profile=profile, holes=holes, pockets=pockets, bosses=bosses, finishes=finishes, references=refs,
        flat_pattern=flat, bends=bends, shaft_steps=steps, properties=dict(spec.properties),
    )
