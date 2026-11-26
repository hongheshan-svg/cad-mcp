"""Creo 编译器：ResolvedPart → CREOSON / 企业扩展命令计划。

J-Link/CREOSON 不能直接创建草图特征。从零建模采用"起始件 + UDF 库"：
  - 起始件 cadmcp_start.prt：已含 CS_Origin、基准面、单位 mmNs、标准参数（PART_NO/TITLE/MATERIAL…）
  - UDF 库：udf_base_rect(W,H,T,R)、udf_base_circle、udf_shaft_stepN、udf_csys_offset(X,Y,Z)、
            udf_hole_{type}_{size}(深度) 、udf_pocket_rect …（由 Creo 工程师一次性建好）
  - 企业扩展命令 cadmcp_udf_place 在 J-Link 中放置 UDF（pfcUDFCustomCreateInstructions）
若有 TOOLKIT 授权，可改为元素树（ProFeatureCreate）直接建特征，计划格式不变。
"""
from __future__ import annotations

import math

from ...errors import CAPABILITY_UNSUPPORTED, CadError
from ...ir.part import PartSpec
from ...ir.resolve import ResolvedPart, shaft_thread_points
from ..native import NativeOp


def compile_part(spec: PartSpec, p: ResolvedPart) -> list[NativeOp]:
    ops = [NativeOp("creoson.file_open", {"file": "cadmcp_start.prt", "new_name": f"{spec.name}.prt"},
                    "复制起始件")]
    for k, v in {"PART_NO": p.part_no, "TITLE": p.title, "MATERIAL": p.material,
                 "SURFACE_TREATMENT": p.surface_treatment or ""}.items():
        ops.append(NativeOp("creoson.parameter_set", {"name": k, "type": "STRING", "value": v}))
    for k, v in p.parameters.items():
        ops.append(NativeOp("creoson.parameter_set", {"name": k.upper(), "type": "DOUBLE", "value": v}))
    if p.base_type == "extrude":
        d = p.profile.data
        udf = {"rect": "udf_base_rect", "circle": "udf_base_circle"}.get(p.profile.shape, "udf_base_polygon")
        ops.append(NativeOp("ext.cadmcp_udf_place", {"udf": udf, "feature": "BASE", "refs": {"CSYS": "CS_ORIGIN"},
                                                    "dims": {**{k: v for k, v in d.items() if isinstance(v, (int, float))},
                                                             "T": p.thickness}}))
    elif p.base_type == "shaft":
        ops.append(NativeOp("ext.cadmcp_udf_place", {"udf": f"udf_shaft_step{len(p.shaft_steps)}", "feature": "BASE",
                                                    "dims": {f"L{i}": L for i, (L, _) in enumerate(p.shaft_steps)}
                                                    | {f"D{i}": d for i, (_, d) in enumerate(p.shaft_steps)}}))
    else:
        ops.append(NativeOp("ext.cadmcp_sheetmetal", {"profile": p.profile.data, "thickness": p.thickness,
                                                     "bends": p.bends}, "需 TOOLKIT 或钣金 UDF；能力矩阵标 partial"))
    for h in p.holes:
        for inst in h.instances:
            cs = f"CS_{h.name}_{inst.index}".upper()
            ops.append(NativeOp("ext.cadmcp_udf_place", {"udf": "udf_csys_offset", "feature": cs,
                                                        "refs": {"CSYS": "CS_ORIGIN"},
                                                        "dims": dict(zip("XYZ", inst.xyz)), "normal": inst.normal}))
            ops.append(NativeOp("ext.cadmcp_udf_place", {
                "udf": f"udf_hole_{h.type}_{h.size_label.replace('Φ', 'D')}", "feature": f"{h.name}_{inst.index}".upper(),
                "refs": {"CSYS": cs}, "dims": {"DEPTH": h.depth, "THREAD": h.thread_depth or 0}}))
    for r in p.references.values():
        if r.kind == "csys" and not r.auto:
            ops.append(NativeOp("ext.cadmcp_udf_place", {"udf": "udf_csys_offset", "feature": r.name.upper(),
                                                        "dims": dict(zip("XYZ", r.origin)), "rotate": r.rotate}))
    ops.append(NativeOp("creoson.file_regenerate", {}))
    ops.append(NativeOp("ext.cadmcp_feature_status", {}, "读取失败特征（CREOSON 原生无此命令）"))
    ops.append(NativeOp("creoson.file_massprops", {}))
    ops.append(NativeOp("creoson.file_save", {}))
    return ops


# ---------------------------------------------------------------------------------------------
# TOOLKIT 路径（cadmcp_tk，协议见 docs/CREO_TOOLKIT.md §3.2）：ResolvedPart → tk.<cmd> 计划
# 草绘平面约定：TOP u=+X v=+Y 法向+Z；FRONT u=+X v=+Z 法向-Y；RIGHT u=+Y v=+Z 法向+X；
# 平面 offset 沿该平面法向计（FRONT 的 offset = -y）。
# ---------------------------------------------------------------------------------------------
# 面 → (草绘平面, 偏移符号, 切除"沿材料内部"方向相对平面法向)；面内坐标 (u,v) 与 ir.resolve.face_point 一致
FACE_SKETCH = {"top": ("TOP", 1, "reverse"), "bottom": ("TOP", 1, "normal"),
               "front": ("FRONT", -1, "reverse"), "back": ("FRONT", -1, "normal"),
               "left": ("RIGHT", 1, "normal"), "right": ("RIGHT", 1, "reverse")}
_PLANE_OF_AXIS = {"z": ("TOP", 1), "y": ("FRONT", -1), "x": ("RIGHT", 1)}


def _line(a, b) -> dict:
    return {"type": "line", "p1": [round(a[0], 6), round(a[1], 6)], "p2": [round(b[0], 6), round(b[1], 6)]}


def _arc(c, r, a0, a1) -> dict:
    return {"type": "arc", "center": [round(c[0], 6), round(c[1], 6)], "r": round(r, 6),
            "a0": round(a0, 6), "a1": round(a1, 6)}


def _circle(c, r) -> dict:
    return {"type": "circle", "center": [round(c[0], 6), round(c[1], 6)], "r": round(r, 6)}


def capsule_entities(a, b, r) -> list[dict]:
    """a、b 为两端圆心，r 为端圆半径；a==b 退化成圆。"""
    if math.dist(a, b) < 1e-9:
        return [_circle(a, r)]
    th = math.atan2(b[1] - a[1], b[0] - a[0])
    n = (-math.sin(th) * r, math.cos(th) * r)
    d = math.degrees(th)
    return [_line((a[0] + n[0], a[1] + n[1]), (b[0] + n[0], b[1] + n[1])),
            _arc(b, r, d - 90, d + 90),
            _line((b[0] - n[0], b[1] - n[1]), (a[0] - n[0], a[1] - n[1])),
            _arc(a, r, d + 90, d + 270)]


def profile_entities(pr) -> list[dict]:
    """ResolvedProfile → 2D 实体列表（草绘面内 u,v，mm）。"""
    d = pr.data
    if pr.shape == "circle":
        return [_circle(d["center"], d["diameter"] / 2)]
    if pr.shape == "slot":
        a = math.radians(d["angle"])
        hx, hy = (d["length"] - d["width"]) / 2 * math.cos(a), (d["length"] - d["width"]) / 2 * math.sin(a)
        cx, cy = d["center"]
        return capsule_entities((cx - hx, cy - hy), (cx + hx, cy + hy), d["width"] / 2)
    if pr.shape == "rect":
        (cx, cy), w, h, r = d["center"], d["width"], d["height"], d["corner_radius"]
        x0, x1, y0, y1 = cx - w / 2, cx + w / 2, cy - h / 2, cy + h / 2
        if r <= 1e-9:
            pts = [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]
            return [_line(pts[i], pts[(i + 1) % 4]) for i in range(4)]
        ents = [_line((x0 + r, y0), (x1 - r, y0)), _arc((x1 - r, y0 + r), r, 270, 360),
                _line((x1, y0 + r), (x1, y1 - r)), _arc((x1 - r, y1 - r), r, 0, 90),
                _line((x1 - r, y1), (x0 + r, y1)), _arc((x0 + r, y1 - r), r, 90, 180),
                _line((x0, y1 - r), (x0, y0 + r)), _arc((x0 + r, y0 + r), r, 180, 270)]
        return [e for e in ents if e["type"] != "line" or math.dist(e["p1"], e["p2"]) > 1e-9]
    pts = d["points"]
    return [_line(pts[i], pts[(i + 1) % len(pts)]) for i in range(len(pts))]


def entity_mid(e: dict) -> tuple[float, float]:
    """实体上的中点（用来在 TOOLKIT 里点选边）。"""
    if e["type"] == "line":
        return ((e["p1"][0] + e["p2"][0]) / 2, (e["p1"][1] + e["p2"][1]) / 2)
    if e["type"] == "arc":
        m = math.radians((e["a0"] + e["a1"]) / 2)
        return (e["center"][0] + e["r"] * math.cos(m), e["center"][1] + e["r"] * math.sin(m))
    return (e["center"][0] + e["r"], e["center"][1])


def edge_points(rule: str, p: ResolvedPart) -> list[list[float]]:
    """倒角/圆角的边中点（模型坐标）。规则同 SW 执行器：
    outer_vertical = 顶面外环各尖角处的竖直棱（取棱中点）；top_outer / bottom_outer = 顶/底面外环各条边（取边中点）。"""
    t = p.thickness or 0.0
    pr = p.profile
    if pr is None:
        return []
    if rule == "outer_vertical":
        if pr.shape == "polygon":
            corners = list(pr.data["points"])
        elif pr.shape == "rect" and pr.data["corner_radius"] <= 1e-9:
            corners = pr.outline
        else:
            return []  # 圆/槽/圆角矩形的竖直面相切连接，没有尖角棱
        return [[round(u, 6), round(v, 6), round(t / 2, 6)] for u, v in corners]
    z = t if rule == "top_outer" else 0.0
    return [[round(m[0], 6), round(m[1], 6), z] for m in (entity_mid(e) for e in profile_entities(pr))]


def _rot(rx: float, ry: float, rz: float):
    """R = Rz·Ry·Rx（度），返回 3×3 矩阵。"""
    a, b, c = (math.radians(v) for v in (rx, ry, rz))
    cx, sx, cy, sy, cz, sz = math.cos(a), math.sin(a), math.cos(b), math.sin(b), math.cos(c), math.sin(c)
    return [[cz * cy, cz * sy * sx - sz * cx, cz * sy * cx + sz * sx],
            [sz * cy, sz * sy * sx + cz * cx, sz * sy * cx - cz * sx],
            [-sy, cy * sx, cy * cx]]


def _face_plane(p: ResolvedPart, face: str, at: float | None) -> tuple[dict, str]:
    from ...ir.resolve import face_plane
    ref, sign, into = FACE_SKETCH[face]
    return {"ref": ref, "offset": round(sign * face_plane(face, p.bbox, at), 6)}, into


def compile_part_tk(spec: PartSpec, p: ResolvedPart, start_part: str = "") -> list[NativeOp]:
    """ResolvedPart → TOOLKIT 命令计划（op 名 `tk.<cmd>`，args 即协议 args）。
    钣金/焊接件首期不支持。`$out_dir` 在执行时替换为输出目录。"""
    if p.base_type not in ("extrude", "shaft"):
        raise CadError(CAPABILITY_UNSUPPORTED,
                       f"Creo TOOLKIT 首期只支持拉伸件和轴类，暂不支持 {p.base_type}（钣金/焊接件）",
                       hint="见 docs/CREO_TOOLKIT.md；钣金与焊接件请在 SolidWorks 目标上建模")
    m = spec.name
    ops: list[NativeOp] = [NativeOp("tk.model.new_part", {"name": m, "start_part": start_part},
                                    "start_part 为空则建空零件并设 mmNs")]

    def feat(cmd: str, feature: str, why: str = "", **args) -> None:
        ops.append(NativeOp(f"tk.{cmd}", {"model": m, "feature": feature, **args}, why))

    def param(k: str, v, typ: str) -> None:
        ops.append(NativeOp("tk.model.param_set", {"name": m, "param": k, "value": v, "type": typ}))

    for k, v in {"PART_NO": p.part_no, "TITLE": p.title, "MATERIAL": p.material,
                 "SURFACE_TREATMENT": p.surface_treatment or "", "CATEGORY": p.category}.items():
        param(k, v, "string")
    for k, v in p.properties.items():
        param(k.upper(), v, "string")
    for k, v in p.parameters.items():
        param(k.upper(), v, "double")

    if p.base_type == "extrude":
        feat("feat.extrude", "Base", plane={"ref": "TOP", "offset": 0}, entities=profile_entities(p.profile),
             depth=p.thickness, direction="normal", op="add", through_all=False)
    else:  # 轴类：轴线 = 模型 Z；FRONT 面上 (u=X, v=Z) 画半截面，绕 u=0 的中心线旋转
        c = p.shaft_chamfer
        steps = p.shaft_steps
        total = sum(L for L, _ in steps)
        r0 = steps[0][1] / 2
        pts = [(0.0, 0.0)] + ([(r0 - c, 0.0), (r0, c)] if c > 0 else [(r0, 0.0)])
        z = 0.0
        for i, (L, d) in enumerate(steps):
            z += L
            r = d / 2
            if i + 1 < len(steps):
                pts += [(r, z), (steps[i + 1][1] / 2, z)]
            elif c > 0:
                pts += [(r, z - c), (r - c, z)]
            else:
                pts.append((r, z))
        pts.append((0.0, total))
        ents = [_line(pts[i], pts[(i + 1) % len(pts)]) for i in range(len(pts))]
        ents = [e for e in ents if math.dist(e["p1"], e["p2"]) > 1e-9]
        axis = {"p1": [0, 0], "p2": [0, total]}
        feat("feat.revolve", "Base", "半截面含两端倒角", plane={"ref": "FRONT", "offset": 0}, entities=ents,
             axis=axis, angle=360, op="add")
        for h in p.end_holes:  # 轴端螺孔：底孔拉伸切除 + 装饰螺纹（TODO(实机核对) 拉伸切除面上能否加装饰螺纹）
            feat("feat.extrude", h["name"], "轴端螺孔底孔", plane={"ref": "TOP", "offset": h["z0"]},
                 entities=[_circle((0, 0), h["tap"] / 2)], depth=h["drill"], direction="normal", op="cut", through_all=False)
            feat("feat.cosmetic_thread", h["name"] + "_THR", "内螺纹装饰（失败不影响几何）", hole_feature=h["name"],
                 major_diameter=h["d"], length=h["depth"])
        if p.shoulder_fillets:  # 轴肩圆角：轴肩根部圆边上的点
            feat("feat.round", "SHOULDER_FILLET", "轴肩圆角", edges=[[sf["r"], 0, sf["z"]] for sf in p.shoulder_fillets],
                 radius=p.shoulder_fillets[0]["R"])
        for fh in p.flange_holes:  # 法兰均布孔：分布圆上各画一圆，切穿法兰段
            feat("feat.extrude", fh["name"], fh["callout"], plane={"ref": "TOP", "offset": fh["z0"]},
                 entities=[_circle(q, fh["d"] / 2) for q in fh["pts"]], depth=fh["z1"] - fh["z0"], direction="normal",
                 op="cut", through_all=False)
        for b in p.shaft_bores:  # 内孔：IR XY 面（TOP）偏移 z0 处画圆，切深 = 孔深（平底）
            feat("feat.extrude", b["name"], "内孔", plane={"ref": "TOP", "offset": b["z0"]},
                 entities=[_circle((0, 0), b["d"] / 2)], depth=b["z1"] - b["z0"], direction="normal", op="cut",
                 through_all=False)
            if b.get("kw"):  # 轮毂键槽在 −Y（与 SW 一致）
                k = b["kw"]
                hw = k["width"] / 2
                pts = [(-hw, 0.0), (hw, 0.0), (hw, -k["top"]), (-hw, -k["top"])]
                feat("feat.extrude", k["name"], "轮毂键槽", plane={"ref": "TOP", "offset": b["z0"]},
                     entities=[_line(pts[i], pts[(i + 1) % 4]) for i in range(4)], depth=b["z1"] - b["z0"],
                     direction="normal", op="cut", through_all=False)
        for f in p.shaft_features:
            if f["kind"] == "thread":
                cyl_pt, start_pt = shaft_thread_points(p, f)
                feat("feat.cosmetic_thread", f["name"], "轴外螺纹：装饰螺纹（失败不影响几何）", edge_point=cyl_pt,
                     start_edge_point=start_pt, major_diameter=f["d"], length=f["length"])
                continue
            if f["kind"] == "gear":  # 齿轮段：IR XY 面（TOP）偏移 z0 处画齿廓折线 + 外圆，切深 = 齿宽
                pts = f["points"]
                ents = [_line(pts[i], pts[(i + 1) % len(pts)]) for i in range(len(pts))] + [_circle((0, 0), f["d"] / 2 + 1)]
                feat("feat.extrude", f["name"], "齿轮段：切出齿槽", plane={"ref": "TOP", "offset": f["z0"]}, entities=ents,
                     depth=f["z1"] - f["z0"], direction="normal", op="cut", through_all=False)
                continue
            if f["kind"] == "keyway":
                ang = round(f["angle"]) % 360
                if ang not in (0, 90, 180, 270):
                    raise CadError(CAPABILITY_UNSUPPORTED, f"keyway {f['name']}: angle must be a multiple of 90 in v0")
                hw = f["width"] / 2
                ents = capsule_entities((0, f["z0"] + hw), (0, f["z1"] - hw), hw)
                o = f["d"] / 2 - f["depth"]  # 草绘面离轴线的距离，向外切到轴表面以外
                # 0°:+X（RIGHT 法向）；180°:-X；90°:+Y（FRONT 法向为 -Y，故反向）；270°:-Y
                plane, dire = {0: ({"ref": "RIGHT", "offset": o}, "normal"),
                               180: ({"ref": "RIGHT", "offset": -o}, "reverse"),
                               90: ({"ref": "FRONT", "offset": -o}, "reverse"),
                               270: ({"ref": "FRONT", "offset": o}, "normal")}[ang]
                feat("feat.extrude", f["name"], "键槽：草绘面在槽底，向外切穿轴表面", plane=plane, entities=ents,
                     depth=f["depth"] + 1, direction=dire, op="cut", through_all=False)
            else:
                ra, rb = f["d2"] / 2, f["d"] / 2 + 1
                ents = [_line((ra, f["z0"]), (rb, f["z0"])), _line((rb, f["z0"]), (rb, f["z1"])),
                        _line((rb, f["z1"]), (ra, f["z1"])), _line((ra, f["z1"]), (ra, f["z0"]))]
                feat("feat.revolve", f["name"], plane={"ref": "FRONT", "offset": 0}, entities=ents,
                     axis=axis, angle=360, op="cut")

    for b in p.bosses:
        feat("feat.extrude", b["name"], plane={"ref": "TOP", "offset": b["z0"]}, entities=profile_entities(b["profile"]),
             depth=b["height"], direction="normal", op="add", through_all=False)
    for pk in p.pockets:
        plane, into = _face_plane(p, pk["face"], pk.get("at"))
        feat("feat.extrude", pk["name"], plane=plane, entities=profile_entities(pk["profile"]), depth=pk["depth"],
             direction=into, op="cut", through_all=bool(pk["through"]))
    for h in p.holes:
        if h.slot:  # 腰型孔：草绘面上画胶囊并切除
            plane, into = _face_plane(p, h.face, h.at)
            ents = []
            for inst in h.instances:
                a, b, r = h.capsule(inst)
                ents += capsule_entities(a, b, r)
            feat("feat.extrude", h.name, plane=plane, entities=ents, depth=h.depth, direction=into, op="cut",
                 through_all=bool(h.through))
            continue
        feat("feat.hole", h.name, face=h.face, points=[[round(c, 6) for c in i.xyz] for i in h.instances],
             diameter=h.drill, depth=h.depth, through=bool(h.through),
             cbore=[h.cbore[0], h.cbore[1]] if h.cbore else None, csink=h.csink, drill_point=not h.through)
        if h.type == "tapped":
            feat("feat.cosmetic_thread", h.name + "_THR", "装饰螺纹（失败不影响几何）", hole_feature=h.name,
                 major_diameter=h.nominal, length=h.thread_depth or h.depth)
    for f in p.finishes:
        pts = edge_points(f["edges"], p)
        if not pts:
            continue  # 没有可选的边（如圆形外形的竖直棱）
        if f["type"] == "fillet":
            feat("feat.round", f["name"], edges=pts, radius=f["size"])
        else:
            feat("feat.chamfer", f["name"], edges=pts, distance=f["size"])
    ext = max(p.bbox.size)
    for r in p.references.values():
        if r.name == "CS_Origin":
            continue  # 默认坐标系已存在
        if r.kind == "csys":
            R = _rot(*r.rotate)
            feat("datum.csys", r.name, origin=list(r.origin), x_dir=[round(R[i][0], 9) for i in range(3)],
                 y_dir=[round(R[i][1], 9) for i in range(3)])
        elif r.kind == "plane":
            d = r.direction or (0, 0, 1)
            ax = next((a for i, a in enumerate("xyz") if abs(abs(d[i]) - 1) < 1e-9), None)
            if ax is None:
                raise CadError(CAPABILITY_UNSUPPORTED, f"datum plane {r.name}: only axis-aligned planes supported")
            ref, sign = _PLANE_OF_AXIS[ax]
            feat("datum.plane", r.name, ref=ref, offset=round(sign * r.origin["xyz".index(ax)], 6))
        elif r.kind == "axis":
            d = r.direction or (0, 0, 1)
            feat("datum.axis", r.name, p1=list(r.origin), p2=[round(r.origin[i] + d[i] * ext, 6) for i in range(3)])
        else:
            feat("datum.point", r.name, at=list(r.origin))
    ops.append(NativeOp("tk.model.regenerate", {"name": m}))
    ops.append(NativeOp("tk.model.feature_status", {"name": m}))
    ops.append(NativeOp("tk.model.save", {"name": m, "dir": "$out_dir"}))
    return ops


# ---------------------------------------------------------------------------------------------
# TOOLKIT 装配（docs/CREO_TOOLKIT.md §3.4）：ResolvedAssembly → tk.asm.* 计划
# ---------------------------------------------------------------------------------------------
def compile_assembly_tk(spec, asm, component_files: dict[str, str], library_root: str = "",
                        start_asm: str = "") -> list[NativeOp]:
    """元件先按求得的位姿（IR 4×4）放入；有 place 且无偏移/旋转时附带坐标系重合约束（服务端约束失败退回固定），
    有偏移/旋转时位姿已含在 transform 里，直接固定放置。库件无原生模型文件时取 library_root/<编码>.prt。"""
    from pathlib import PurePath

    m = spec.name
    ops: list[NativeOp] = [NativeOp("tk.asm.new", {"name": m, "start_asm": start_asm},
                                    "start_asm 为空则建空装配（无 ASM_DEF_CSYS，坐标系约束会退回固定）")]
    for k, v in {"PART_NO": spec.part_no, "TITLE": spec.title, **{k.upper(): v for k, v in spec.properties.items()}}.items():
        ops.append(NativeOp("tk.model.param_set", {"name": m, "param": k, "value": str(v), "type": "string"}))
    comps = {c.id: c for c in spec.components}
    ids = {i.id for i in asm.instances}
    for inst in asm.instances:
        c = comps[inst.component]
        kind, _, ref = c.source.partition(":")
        if inst.component in component_files:
            file = str(component_files[inst.component])
        elif kind == "lib" and library_root:
            file = str(PurePath(library_root) / f"{ref.partition('?')[0]}.prt")
        else:
            raise CadError(CAPABILITY_UNSUPPORTED, f"组件 {inst.id}（{c.source}）没有可装入的 Creo 模型文件",
                           hint="在节点配置中设置 library_root，或先在本会话中建出该零件")
        args = {"asm": m, "file": file, "name": inst.id,
                "transform": [[round(float(x), 9) for x in row] for row in inst.transform], "csys": None}
        why = "按求得的位姿固定放置"
        if c.place is not None and not any(c.place.offset) and not any(c.place.rotate):
            tgt, _, tref = c.place.to.partition(".")
            args["csys"] = {"ref": c.place.align, "target": None if tgt == "ASM" else (tgt if tgt in ids else f"{tgt}#1"),
                            "target_ref": tref}
            why = "坐标系重合约束（失败退回固定）"
        ops.append(NativeOp("tk.asm.add_component", args, why))
    ops.append(NativeOp("tk.model.regenerate", {"name": m}))
    ops.append(NativeOp("tk.asm.interference", {"asm": m}, "全局实体干涉"))
    ops.append(NativeOp("tk.model.save", {"name": m, "dir": "$out_dir"}))
    return ops
