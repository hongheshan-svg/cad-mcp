"""装配解析：坐标系对齐求位姿、阵列展开、BOM、包围盒干涉检查（与 CAD 无关）。

真实适配器用这里求出的位姿做校验；原生装配仍用坐标系重合约束（SW 坐标系配合 /
Creo Csys 约束）建立，保证模型在 CAD 中仍然是"约束驱动"而非"固定位置"。
"""
from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from ..errors import REF_NOT_FOUND, SPEC_INVALID, CadError
from ..expr import evaluate, resolve_parameters
from .assembly import AssemblySpec
from .resolve import Box, ResolvedRef

Mat = list[list[float]]


def identity() -> Mat:
    return [[1.0 if i == j else 0.0 for j in range(4)] for i in range(4)]


def matmul(a: Mat, b: Mat) -> Mat:
    return [[sum(a[i][k] * b[k][j] for k in range(4)) for j in range(4)] for i in range(4)]


def translate(x: float, y: float, z: float) -> Mat:
    m = identity()
    m[0][3], m[1][3], m[2][3] = x, y, z
    return m


def rotate_xyz(rx: float, ry: float, rz: float) -> Mat:
    """依次绕 X、Y、Z（固定轴）旋转，角度为度。结果 = Rz·Ry·Rx。"""
    def r(axis: int, deg: float) -> Mat:
        c, s = math.cos(math.radians(deg)), math.sin(math.radians(deg))
        c = 0.0 if abs(c) < 1e-12 else c
        s = 0.0 if abs(s) < 1e-12 else s
        m = identity()
        i, j = [(1, 2), (0, 2), (0, 1)][axis]
        m[i][i], m[i][j], m[j][i], m[j][j] = c, -s, s, c
        if axis == 1:
            m[i][j], m[j][i] = s, -s
        return m
    return matmul(r(2, rz), matmul(r(1, ry), r(0, rx)))


def invert_rigid(m: Mat) -> Mat:
    rt = [[m[j][i] for j in range(3)] for i in range(3)]
    t = [m[i][3] for i in range(3)]
    out = identity()
    for i in range(3):
        for j in range(3):
            out[i][j] = rt[i][j]
        out[i][3] = -sum(rt[i][k] * t[k] for k in range(3))
    return out


def apply(m: Mat, p: tuple[float, float, float]) -> tuple[float, float, float]:
    return tuple(sum(m[i][k] * p[k] for k in range(3)) + m[i][3] for i in range(3))  # type: ignore[return-value]


def euler_xyz(m: Mat) -> tuple[float, float, float]:
    """rotate_xyz 的逆：旋转部分 R = Rz·Ry·Rx → (rx, ry, rz)（度）。万向锁（ry=±90°）时取 rx=0。"""
    sy = max(-1.0, min(1.0, -m[2][0]))
    ry = math.asin(sy)
    if abs(abs(sy) - 1) < 1e-9:
        rx, rz = 0.0, math.atan2(-m[0][1], m[1][1])
    else:
        rx, rz = math.atan2(m[2][1], m[2][2]), math.atan2(m[1][0], m[0][0])
    return tuple(round(math.degrees(a), 9) + 0.0 for a in (rx, ry, rz))  # type: ignore[return-value]


def same_frame(a: Mat, b: Mat, tol: float = 1e-6) -> bool:
    return all(abs(a[i][j] - b[i][j]) <= tol for i in range(3) for j in range(4))


def ref_frame(ref: ResolvedRef) -> Mat:
    return matmul(translate(*ref.origin), rotate_xyz(*ref.rotate))


def world_box(m: Mat, b: Box) -> Box:
    pts = [apply(m, (x, y, z)) for x in (b.xmin, b.xmax) for y in (b.ymin, b.ymax) for z in (b.zmin, b.zmax)]
    xs, ys, zs = zip(*pts)
    return Box(min(xs), max(xs), min(ys), max(ys), min(zs), max(zs))


@dataclass
class SourceInfo:
    """组件来源的几何与 BOM 信息（由 Gateway 根据 part:/lib:/asm:/file: 提供）。"""
    source: str
    kind: str  # part | assembly | purchased | standard
    part_no: str
    title: str
    material: str | None
    mass_kg: float
    bbox: Box
    references: dict[str, ResolvedRef]
    model: str | None = None
    properties: dict[str, str] = field(default_factory=dict)
    standard: str = ""  # 标准件：标准号（如 GB/T 70.1），明细表"代号"栏
    mounts: dict[str, dict[str, Any]] = field(default_factory=dict)  # 零件：CS_<mount> → 接口信息
    interface_frame: str | None = None  # 外购件：登记的安装接口坐标系
    install_holes: list[dict[str, Any]] = field(default_factory=list)  # 零件：顶/底面成组通孔 {name, dia, label, pts(零件坐标)}
    revolve: list[tuple[float, float, float, float]] = field(default_factory=list)  # 回转件沿零件 Z：(z0, z1, 外径, 内径)
    threads: list[tuple[float, float, float]] = field(default_factory=list)  # 外螺纹沿零件 Z：(z0, z1, 大径)
    gears: list[dict[str, float]] = field(default_factory=list)  # 齿轮段沿零件 Z：{z0, z1, da, d(分度圆), df}
    fits: list[dict[str, Any]] = field(default_factory=list)  # 零件：带配合公差的圆柱面 {role hole|shaft, d, tol, p0, p1(轴线段，零件坐标), name}


def part_revolve(p) -> tuple[list[tuple[float, float, float, float]], list[dict[str, float]]]:
    """回转件外形（沿零件 Z 的 (z0, z1, 外径, 内径)）与齿轮段：阶梯轴/齿轮轴按轴段（齿轮段外径 = 齿顶圆），
    盘状齿轮按齿顶圆 × 齿宽、内径 = 中心轴孔。其它零件返回空。"""
    if p.base_type == "shaft":
        rev = [(*p.step_z(k), d, 0.0) for k, (_, d) in enumerate(p.shaft_steps)]
        gears = [{"z0": f["z0"], "z1": f["z1"], "da": f["d"], "d": f["pd"], "df": f["df"]}
                 for f in (p.shaft_features or []) if f.get("kind") == "gear"]
        return rev, gears
    if p.gear:
        from ..gear_drawing import gear_parts
        bore = gear_parts(p)["bore"]
        g = p.gear
        z0, z1 = p.bbox.zmin, p.bbox.zmax
        return [(z0, z1, g["da"], bore.drill if bore else 0.0)], [{"z0": z0, "z1": z1, "da": g["da"], "d": g["d"], "df": g["df"]}]
    return [], []


def part_fits(p) -> list[dict[str, Any]]:
    """零件上可能参与配合的圆柱面（孔：光孔/套类内孔；轴：轴段），tol 为公差带（未注为 None），轴线段为零件坐标。"""
    out: list[dict[str, Any]] = []
    for h in p.holes:
        if h.slot:
            continue
        if h.type == "tapped":  # 螺纹孔：装配全剖画内螺纹（不参与配合）
            for i in h.instances:
                n = i.normal
                at = lambda t: tuple(i.xyz[k] - n[k] * t for k in range(3))  # noqa: E731
                out.append({"role": "tap", "d": h.nominal, "drill": h.drill, "p0": tuple(i.xyz),
                            "p1": at(h.thread_depth or h.depth), "p2": at(h.depth), "through": h.through,
                            "name": f"{h.name}[{i.index}]"})
            continue
        if h.type in ("clearance", "counterbore", "countersink"):  # 螺钉过孔：装配全剖中挖空
            for i in h.instances:
                n = i.normal
                out.append({"role": "bore", "d": h.drill, "p0": tuple(i.xyz),
                            "p1": tuple(i.xyz[k] - n[k] * h.depth for k in range(3)), "cbore": h.cbore,
                            "name": f"{h.name}[{i.index}]"})
            continue
        if h.type not in ("simple",):
            continue
        for i in h.instances:
            n = i.normal
            q = tuple(i.xyz[k] - n[k] * h.depth for k in range(3))
            out.append({"role": "hole", "d": h.drill, "tol": h.tolerance, "p0": tuple(i.xyz), "p1": q,
                        "name": f"{h.name}[{i.index}]"})
    tols = list(getattr(p, "shaft_tolerances", []) or [])
    for k, (L, d) in enumerate(getattr(p, "shaft_steps", []) or []):
        z0, z1 = p.step_z(k)
        out.append({"role": "shaft", "d": d, "tol": tols[k] if k < len(tols) else None,
                    "p0": (0.0, 0.0, z0), "p1": (0.0, 0.0, z1), "name": f"step:{k}"})
    for b in getattr(p, "shaft_bores", []) or []:
        out.append({"role": "hole", "d": b["d"], "tol": b.get("tol"), "p0": (0.0, 0.0, b["z0"]), "p1": (0.0, 0.0, b["z1"]),
                    "name": b["name"]})
    return out


def _coaxial_pairs(a: "ResolvedAssembly"):
    """不同零件上公称直径相同、轴线重合、轴向有重叠的孔与轴；每个孔只取第一根轴。"""
    feats = []
    for inst in a.instances:
        for f in inst.source.fits:
            feats.append((inst, f, apply(inst.transform, f["p0"]), apply(inst.transform, f["p1"])))
    for ih, fh, h0, h1 in feats:
        if fh["role"] != "hole":
            continue
        dh = [h1[k] - h0[k] for k in range(3)]
        L = sum(c * c for c in dh) ** 0.5
        if L < 1e-9:
            continue
        u = [c / L for c in dh]
        for js, fs, s0, s1 in feats:
            if fs["role"] != "shaft" or js is ih or abs(fh["d"] - fs["d"]) > 1e-6:
                continue
            ds = [s1[k] - s0[k] for k in range(3)]
            Ls = sum(c * c for c in ds) ** 0.5
            if Ls < 1e-9 or abs(abs(sum(u[k] * ds[k] / Ls for k in range(3))) - 1) > 1e-6:
                continue  # 不平行

            def off(q):  # 点到孔轴线的距离、沿轴线参数
                w = [q[k] - h0[k] for k in range(3)]
                t = sum(w[k] * u[k] for k in range(3))
                return sum((w[k] - t * u[k]) ** 2 for k in range(3)) ** 0.5, t
            (e0, t0), (e1, t1) = off(s0), off(s1)
            if max(e0, e1) > 0.01:
                continue  # 不同轴
            lo, hi = max(0.0, min(t0, t1)), min(L, max(t0, t1))
            if hi - lo < 1e-3:
                continue  # 轴向不重叠
            yield ih, fh, js, fs, tuple(h0[k] + u[k] * lo for k in range(3)), tuple(h0[k] + u[k] * hi for k in range(3))
            break


def assembly_fits(a: "ResolvedAssembly") -> list[dict[str, Any]]:
    """装配中的配合（GB/T 4458.5 配合代号 Φd H7/k6）：同轴重叠且孔、轴都注了公差带。
    返回 {d, text, hole, shaft, inst_hole, inst_shaft, p0, p1(重叠段轴线，装配坐标)}。"""
    out = []
    for ih, fh, js, fs, p0, p1 in _coaxial_pairs(a):
        if fh["tol"] and fs["tol"]:
            d = fh["d"]
            # 与滚动轴承配合：只注零件一侧（GB/T 4458.5），轴承内圈孔、外圈的公差由轴承标准规定
            text = f"Φ{d:g}{fs['tol']}" if fh["tol"] == "bearing" else f"Φ{d:g}{fh['tol']}" if fs["tol"] == "bearing" \
                else f"Φ{d:g}{fh['tol']}/{fs['tol']}"
            if fh["tol"] == fs["tol"] == "bearing":
                continue
            out.append({"d": d, "text": text, "hole": fh["name"], "shaft": fs["name"],
                        "inst_hole": ih.id, "inst_shaft": js.id, "p0": p0, "p1": p1})
    return out


def fit_gaps(a: "ResolvedAssembly") -> list[dict[str, Any]]:
    """A005：同轴、同公称直径、轴向重叠的孔与轴，但孔或轴未注公差带——装配图配合尺寸会漏标，
    零件图上也缺加工依据。"""
    out = []
    for ih, fh, js, fs, _, _ in _coaxial_pairs(a):
        if fh["tol"] and fs["tol"] or fh["tol"] == fs["tol"] == "bearing":
            continue
        miss = [f"{ih.id}.{fh['name']}（孔）" for _ in [0] if not fh["tol"]] + \
               [f"{js.id}.{fs['name']}（轴）" for _ in [0] if not fs["tol"]]
        out.append({"rule": "A005", "severity": "warning", "target": f"{ih.id}.{fh['name']}~{js.id}.{fs['name']}",
                    "message": f"Φ{fh['d']:g} 孔与轴同轴配合，但 {'、'.join(miss)} 未注公差带",
                    "hint": "按配合性质补公差带：滚动轴承位轴 k6/js6、座孔 H7/J7；定位销 H7/m6、轴承座孔 H7、齿轮孔 H7/k6，间隙配合 H7/f7、H8/f7"})
    return out


@dataclass
class Instance:
    id: str
    component: str
    source: SourceInfo
    transform: Mat
    world_bbox: Box


@dataclass
class ResolvedAssembly:
    name: str
    part_no: str
    title: str
    parameters: dict[str, float]
    instances: list[Instance]
    bbox: Box
    mass_kg: float
    properties: dict[str, str]
    extras: list[tuple[SourceInfo, int, str]] = field(default_factory=list)

    def bom(self) -> list[dict[str, Any]]:
        rows: dict[str, dict[str, Any]] = {}

        def row(s: SourceInfo) -> dict[str, Any]:
            key = s.part_no or s.source
            if key not in rows:
                rows[key] = {"part_no": s.part_no, "title": s.title, "spec": s.model or "", "material": s.material or "",
                             "kind": s.kind, "qty": 0, "unit_mass_kg": round(s.mass_kg, 4), "instances": [],
                             "source": s.source, "standard": s.standard, "properties": dict(s.properties)}
            return rows[key]

        for inst in self.instances:
            r = row(inst.source)
            r["qty"] += 1
            r["instances"].append(inst.id)
        for s, qty, note in self.extras:
            r = row(s)
            r["qty"] += qty
            if note:
                r["note"] = note
        order = {"assembly": 0, "part": 1, "purchased": 2, "standard": 3}
        out = sorted(rows.values(), key=lambda r: (order.get(r["kind"], 9), r["part_no"]))
        for i, r in enumerate(out, 1):
            r["item"] = i
        return out

    def summary(self) -> dict[str, Any]:
        return {"name": self.name, "part_no": self.part_no, "title": self.title,
                "instances": len(self.instances), "bbox_mm": [round(v, 2) for v in self.bbox.size],
                "mass_kg": round(self.mass_kg, 3), "bom_lines": len(self.bom())}


def resolve_assembly(spec: AssemblySpec, source_info: Callable[[str], SourceInfo]) -> ResolvedAssembly:
    params = resolve_parameters(spec.parameters)

    def ev(v):
        return evaluate(v, params)

    comps = {c.id: c for c in spec.components}
    sources = {c.id: source_info(c.source) for c in spec.components}
    base_tf: dict[str, Mat] = {}
    visiting: set[str] = set()

    def ref_of(comp_id: str, ref: str) -> ResolvedRef:
        refs = sources[comp_id].references
        if ref not in refs:
            raise CadError(REF_NOT_FOUND, f"{comp_id} ({sources[comp_id].source}) has no reference {ref!r}",
                           hint=f"available: {sorted(refs)}")
        r = refs[ref]
        if r.kind != "csys":
            raise CadError(SPEC_INVALID, f"{comp_id}.{ref} is a {r.kind}; v0 placement only aligns coordinate systems (CS_*)",
                           hint="在零件中添加 CS_ 坐标系基准用于装配")
        return r

    def tf(cid: str) -> Mat:
        if cid in base_tf:
            return base_tf[cid]
        if cid in visiting:
            raise CadError(SPEC_INVALID, f"circular placement involving {cid!r}")
        visiting.add(cid)
        c = comps[cid]
        if c.place is None:
            m = identity()
        else:
            tgt_comp, _, tgt_ref = c.place.to.partition(".")
            if tgt_comp == "ASM":
                if tgt_ref != "CS_Origin":
                    raise CadError(REF_NOT_FOUND, f"assembly reference {tgt_ref!r} unknown", hint="use ASM.CS_Origin")
                target = identity()
            else:
                target = matmul(tf(tgt_comp), ref_frame(ref_of(tgt_comp, tgt_ref)))
            off = [ev(v) for v in c.place.offset]
            rot = [ev(v) for v in c.place.rotate]
            align = ref_frame(ref_of(cid, c.place.align))
            m = matmul(target, matmul(translate(*off), matmul(rotate_xyz(*rot), invert_rigid(align))))
        visiting.discard(cid)
        base_tf[cid] = m
        return m

    instances: list[Instance] = []
    for c in spec.components:
        m0 = tf(c.id)
        n = int(round(ev(c.array.count))) if c.array else 1
        step = [ev(v) for v in c.array.step] if c.array else [0.0, 0.0, 0.0]
        for k in range(n):
            m = matmul(translate(*(s * k for s in step)), m0)
            iid = c.id if n == 1 else f"{c.id}#{k + 1}"
            instances.append(Instance(iid, c.id, sources[c.id], m, world_box(m, sources[c.id].bbox)))

    boxes = [i.world_bbox for i in instances]
    bb = Box(min(b.xmin for b in boxes), max(b.xmax for b in boxes), min(b.ymin for b in boxes),
             max(b.ymax for b in boxes), min(b.zmin for b in boxes), max(b.zmax for b in boxes))
    extras = []
    for e in spec.bom_extras:
        if not e.source.startswith("lib:"):
            raise CadError(SPEC_INVALID, f"bom_extras {e.source}: only lib: sources")
        extras.append((source_info(e.source), int(round(ev(e.qty))), e.note))
    return ResolvedAssembly(spec.name, spec.part_no, spec.title, params, instances, bb,
                            sum(i.source.mass_kg for i in instances) + sum(s.mass_kg * q for s, q, _ in extras),
                            dict(spec.properties), extras)


def aabb_interference(asm: ResolvedAssembly, tol: float = 0.01) -> list[dict[str, Any]]:
    """包围盒干涉（保守：可能误报 L 形件等，不会漏报）。贴合面（重叠厚度 ≤ tol）不算干涉。"""
    out = []
    ins = asm.instances
    for a in range(len(ins)):
        for b in range(a + 1, len(ins)):
            A, B = ins[a].world_bbox, ins[b].world_bbox
            dx = min(A.xmax, B.xmax) - max(A.xmin, B.xmin)
            dy = min(A.ymax, B.ymax) - max(A.ymin, B.ymin)
            dz = min(A.zmax, B.zmax) - max(A.zmin, B.zmin)
            if dx > tol and dy > tol and dz > tol:
                out.append({"a": ins[a].id, "b": ins[b].id, "overlap_mm": [round(dx, 3), round(dy, 3), round(dz, 3)],
                            "volume_mm3": round(dx * dy * dz, 1)})
    return out


def interface_check(spec: AssemblySpec, source_info: Callable[[str], SourceInfo]) -> list[dict[str, Any]]:
    """外购件安装接口校验：装在 mount 接口上的必须是同一个件、用登记的接口坐标系对齐；
    外购件装在没有 mount 的位置上提示孔位未经校验。"""
    out: list[dict[str, Any]] = []
    comps = {c.id: c for c in spec.components}
    for c in spec.components:
        if c.place is None:
            continue
        tgt_id, _, tgt_ref = c.place.to.partition(".")
        if tgt_id == "ASM" or tgt_id not in comps:
            continue
        src, tgt = source_info(c.source), source_info(comps[tgt_id].source)
        where = f"{c.id}→{tgt_id}.{tgt_ref}"
        m = tgt.mounts.get(tgt_ref)
        if m is not None and src.kind == "standard":
            continue  # 紧固件借用接口坐标系定位，不校验
        if m is not None:
            ref = c.source.partition(":")[2]
            code = ref.partition("?")[0]
            if not c.source.startswith("lib:") or code != m["item"]:
                out.append({"rule": "A003", "severity": "error", "target": where,
                            "message": f"{tgt_id}.{tgt_ref} 是按 {m['item']} 开的安装接口，但装的是 {c.source}",
                            "fix": f"改装 lib:{m['item_ref']}，或修改零件的 mount.item"})
            elif c.place.align != m["frame"]:
                out.append({"rule": "A003", "severity": "error", "target": where,
                            "message": f"应以 {m['frame']} 对齐安装接口，实际用 {c.place.align}",
                            "fix": f"place.align = {m['frame']!r}"})
            elif ref != m["item_ref"] and "?" in (ref + m["item_ref"]):
                out.append({"rule": "A003", "severity": "warning", "target": where,
                            "message": f"接口按 {m['item_ref']} 开孔，装配件参数为 {ref}（孔数/孔位可能不同）",
                            "fix": "两处参数保持一致"})
        elif src.interface_frame and c.place.align == src.interface_frame and tgt.kind == "part":
            out.append({"rule": "A004", "severity": "warning", "target": where,
                        "message": f"{c.source} 装在 {tgt_id}.{tgt_ref}，该处没有按接口开孔（mount），孔位未经校验",
                        "fix": f"在 {tgt_id} 中用 mount 特征（item={c.source.partition(':')[2]}）生成安装孔"})
    return out


def solid_boxes(inst: "Instance") -> list[tuple[Box, tuple[float, float, float, float] | None]]:
    """装配消隐用的实体块（装配坐标）：回转件每段一个包围盒（带段参数），其余为整体包围盒。"""
    if not inst.source.revolve:
        return [(inst.world_bbox, None)]
    out = []
    for seg in inst.source.revolve:
        z0, z1, do, _ = seg
        r = do / 2
        out.append((world_box(inst.transform, Box(-r, r, -r, r, z0, z1)), seg))
    return out
