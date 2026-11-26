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
    mounts: dict[str, dict[str, Any]] = field(default_factory=dict)  # 零件：CS_<mount> → 接口信息
    interface_frame: str | None = None  # 外购件：登记的安装接口坐标系


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
                             "source": s.source}
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
