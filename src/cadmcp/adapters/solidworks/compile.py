"""SolidWorks 编译器：ResolvedPart → NativeOp 列表。

约定：IR 坐标系 = SW 模型坐标系；IR 的 XY 草图面 = SW "Front Plane"（前视基准面，法向 +Z）。
所有长度在计划中为 mm，执行器负责换算为 SW API 的米/弧度。
参数化：spec.parameters → 全局变量；尺寸通过方程式链接到全局变量，模型在 SW 里仍可手工改参。
"""
from __future__ import annotations

from ...errors import CAPABILITY_UNSUPPORTED, CadError

from ...ir.assembly_resolve import euler_xyz, identity, matmul, ref_frame, same_frame
from ...ir.part import PartSpec
from ...ir.resolve import ResolvedPart, shaft_thread_points
from ..native import NativeOp, to_sw_equation

HOLE_TYPE = {"tapped": "swWzdTap", "counterbore": "swWzdCounterBore",
             "countersink": "swWzdCounterSink", "clearance": "swWzdHole", "simple": "swWzdHole"}
FACE_RAY = {  # 从外部沿法向反方向打射线选面
    "top": ((0, 0, 1), (0, 0, -1)), "bottom": ((0, 0, -1), (0, 0, 1)),
    "front": ((0, -1, 0), (0, 1, 0)), "back": ((0, 1, 0), (0, -1, 0)),
    "left": ((-1, 0, 0), (1, 0, 0)), "right": ((1, 0, 0), (-1, 0, 0)),
}


def _profile_ops(pr, sketch: str) -> list[NativeOp]:
    d = pr.data
    if pr.shape == "rect":
        ops = [NativeOp("sketch_center_rectangle", {"sketch": sketch, "center": d["center"],
                                                     "width": d["width"], "height": d["height"]})]
        if d["corner_radius"] > 0:
            ops.append(NativeOp("sketch_fillet_all_corners", {"sketch": sketch, "radius": d["corner_radius"]}))
        return ops
    if pr.shape == "circle":
        return [NativeOp("sketch_circle", {"sketch": sketch, "center": d["center"], "diameter": d["diameter"]})]
    if pr.shape == "slot":
        return [NativeOp("sketch_slot", {"sketch": sketch, **d})]
    return [NativeOp("sketch_polyline_closed", {"sketch": sketch, "points": d["points"]})]


def _face_sketch(face: str, box, sketch: str, at: float | None = None, uv=(None, None)) -> NativeOp:
    """在面上开草图。射线从面外 0.5mm、目标位置处沿法向反方向打入：
    外表面和非外表面（台阶面、L 形内侧面，用 at 指定）都能选中正确的面。"""
    from ...ir.resolve import face_plane, face_point
    n = FACE_RAY[face][0]
    if uv[0] is None:
        lo = {"top": (box.xmin, box.ymin), "bottom": (box.xmin, box.ymin), "front": (box.xmin, box.zmin),
              "back": (box.xmin, box.zmin), "left": (box.ymin, box.zmin), "right": (box.ymin, box.zmin)}[face]
        hi = {"top": (box.xmax, box.ymax), "bottom": (box.xmax, box.ymax), "front": (box.xmax, box.zmax),
              "back": (box.xmax, box.zmax), "left": (box.ymax, box.zmax), "right": (box.ymax, box.zmax)}[face]
        uv = ((lo[0] + hi[0]) / 2, (lo[1] + hi[1]) / 2)
    pt = face_point(face, uv, box, at)
    start = tuple(pt[i] + n[i] * 0.5 for i in range(3))
    return NativeOp("sketch_on_face", {"sketch": sketch, "face": face, "ray_from": start, "ray_dir": FACE_RAY[face][1],
                                       "bbox": [box.xmin, box.xmax, box.ymin, box.ymax, box.zmin, box.zmax],
                                       "plane_coord": face_plane(face, box, at)},
                    "在目标位置附近沿法向打射线选面，不依赖面 ID；面内坐标经 ModelToSketchTransform 换算")


AXIS_PLANE = {"z": "Front Plane", "x": "Right Plane", "y": "Top Plane"}  # 草图平面法向 = 拉伸方向


def _section_loops(body: dict) -> tuple[list[list[list[float]]], list[tuple[list[float], float]]]:
    """杆件/板件截面（3D 点，位于过原点、法向为杆件轴的平面上）：返回 (闭合折线列表, 圆列表)。"""
    a = body["axis"] if body["kind"] == "member" else body["normal"]
    ia = "xyz".index(a)
    box = body["box"]
    if body["kind"] == "member":
        h_ax = body["height_axis"]
        o_ax = next(x for x in "xyz" if x not in (a, h_ax))
    else:
        h_ax, o_ax = [x for x in "xyz" if x != a]
    ih, io = "xyz".index(h_ax), "xyz".index(o_ax)
    h0, h1 = box[2 * ih], box[2 * ih + 1]
    o0, o1 = box[2 * io], box[2 * io + 1]

    def P(hv, ov):
        pt = [0.0, 0.0, 0.0]
        pt[ia], pt[ih], pt[io] = 0.0, hv, ov
        return pt

    def rect(a0, a1, b0, b1):
        return [P(a0, b0), P(a1, b0), P(a1, b1), P(a0, b1)]

    sec = body.get("section")
    if sec is None or sec["kind"] == "FB":
        return [rect(h0, h1, o0, o1)], []
    k, t = sec["kind"], sec["t"]
    if k in ("SHS", "RHS"):
        return [rect(h0, h1, o0, o1), rect(h0 + t, h1 - t, o0 + t, o1 - t)], []
    if k == "CHS":
        c = P((h0 + h1) / 2, (o0 + o1) / 2)
        return [], [(c, sec["h"]), (c, sec["h"] - 2 * t)]
    if k == "L":  # 角钢：内角在 (h0, o0)，两肢沿 +h、+o
        return [[P(h0, o0), P(h1, o0), P(h1, o0 + t), P(h0 + t, o0 + t), P(h0 + t, o1), P(h0, o1)]], []
    from ...ir.weldment import CHANNEL, IBEAM
    if k == "C":  # 槽钢：腹板在 o0 侧，开口朝 +o
        d, tf = CHANNEL[sec["code"]][2], CHANNEL[sec["code"]][3]
        return [[P(h0, o0), P(h0, o1), P(h0 + tf, o1), P(h0 + tf, o0 + d), P(h1 - tf, o0 + d), P(h1 - tf, o1),
                 P(h1, o1), P(h1, o0)]], []
    d, tf = IBEAM[sec["code"]][2], IBEAM[sec["code"]][3]  # 工字钢
    om = (o0 + o1) / 2
    return [[P(h0, o0), P(h0, o1), P(h0 + tf, o1), P(h0 + tf, om + d / 2), P(h1 - tf, om + d / 2), P(h1 - tf, o1),
             P(h1, o1), P(h1, o0), P(h1 - tf, o0), P(h1 - tf, om - d / 2), P(h0 + tf, om - d / 2), P(h0 + tf, o0)]], []


def weldment_ops(p: ResolvedPart) -> list[NativeOp]:
    """焊接件：多实体零件。每个杆件/板件在垂直于其轴的基准面上画截面，带起始偏移拉伸、不合并；
    之后插入焊件特征（SW 自动生成切割清单）。焊缝以焊缝符号/技术要求表达，不建焊缝实体。"""
    ops = [NativeOp("insert_weldment", {}, "FeatureManager.InsertWeldmentFeature：多实体 + 切割清单")]
    for body in p.weld_bodies:
        a = body["axis"] if body["kind"] == "member" else body["normal"]
        ia = "xyz".index(a)
        lo, hi = body["box"][2 * ia], body["box"][2 * ia + 1]
        sk = f"SK_{body['name']}"
        loops, circles = _section_loops(body)
        ops.append(NativeOp("sketch_on_plane", {"sketch": sk, "plane": AXIS_PLANE[a]}))
        for loop in loops:
            ops.append(NativeOp("sketch_polyline_3d", {"sketch": sk, "points": loop}))
        for c, dia in circles:
            ops.append(NativeOp("sketch_circle_3d", {"sketch": sk, "center": c, "diameter": dia}))
        ops.append(NativeOp("extrude_body", {"feature": body["name"], "sketch": sk, "start_offset": lo,
                                             "depth": hi - lo},
                            "FeatureExtrusion3 Merge=False，起始偏移到杆件端面"))
    # 切割清单属性：杆件是拉伸实体（非结构构件），SW 不会自动算 LENGTH，按方案写入各切割清单项
    rows = []
    for r in p.cut_list:
        props = {"Description": r["spec"], "MATERIAL": p.material, "规格": r["spec"]}
        if r["kind"] == "member":
            props.update(LENGTH=f"{r['length_mm']:g}", 下料长度=f"{r['length_mm']:g}", 端部="两端平切 90°")
        else:
            props.update(LENGTH="×".join(f"{v:g}" for v in r["size_mm"]), 板厚=r["spec"].split("t")[-1])
        rows.append({"bodies": list(r["bodies"]), "props": props})
    ops.append(NativeOp("cut_list_properties", {"rows": rows},
                        "UpdateCutList → 各 CutListFolder 的 CustomPropertyManager 写 Description/LENGTH/MATERIAL"))
    welds = p.welds
    if welds:
        ops.append(NativeOp("set_custom_properties", {"properties": {
            "WeldCount": str(len(welds)), "WeldLength": f"{sum(w['length'] for w in welds):.0f}",
            "WeldSizes": ",".join(f"K{k:g}" for k in sorted({w['size'] for w in welds}))}}))
    return ops


def compile_part(spec: PartSpec, p: ResolvedPart, template: str = "", holes_via: str = "cut") -> list[NativeOp]:
    def _end(h) -> str:
        # 钣金翻边上的"通孔"只穿板厚：through_all 会连对面翻边一起打穿；焊接板同理（不能打到下面的杆件）
        if p.base_type == "sheet_metal" and h.face not in ("top", "bottom") or p.base_type == "weldment":
            return "blind"
        return "through_all" if h.through else "blind"

    ops: list[NativeOp] = [
        NativeOp("new_document", {"kind": "part", "template": template}),
        NativeOp("set_units", {"system": "MMGS"}),
    ]
    for name, expr in spec.parameters.items():
        ops.append(NativeOp("add_global_variable", {"name": name, "equation": f'"{name}" = {to_sw_equation(expr)}'}))
    ops.append(NativeOp("set_material", {"database": "company_materials", "name": p.material}))
    props = {"PartNo": p.part_no, "Description": p.title, "Material": p.material,
             "SurfaceTreatment": p.surface_treatment or "", "Category": p.category, **p.properties}
    ops.append(NativeOp("set_custom_properties", {"properties": props}))

    base = spec.base
    if p.base_type == "extrude":
        ops.append(NativeOp("sketch_on_plane", {"sketch": "SK_Base", "plane": "Front Plane"}))
        ops += _profile_ops(p.profile, "SK_Base")
        ops.append(_dimension_profile_op(p.profile, base))
        ops.append(NativeOp("extrude_boss", {"feature": "Base", "sketch": "SK_Base", "depth": p.thickness,
                                             "depth_equation": to_sw_equation(base.thickness)}))
    elif p.base_type == "shaft":
        ops.append(NativeOp("sketch_on_plane", {"sketch": "SK_Shaft", "plane": "Right Plane"}))
        ops.append(NativeOp("sketch_shaft_half_profile", {"sketch": "SK_Shaft", "steps": p.shaft_steps,
                                                           "axis": "Z", "chamfer": p.shaft_chamfer}))
        ops.append(NativeOp("revolve", {"feature": "Base", "sketch": "SK_Shaft", "angle": 360}))
        for h in p.end_holes:  # 轴端螺孔：攻丝底孔切除 + 孔口圆边上的内螺纹装饰线
            sk = f"SK_{h['name']}"
            ops.append(NativeOp("sketch_on_plane", {"sketch": sk, "plane": "Front Plane"}))
            ops.append(NativeOp("sketch_circle", {"sketch": sk, "center": [0, 0], "diameter": h["tap"]}))
            ops.append(NativeOp("keyway_cut", {"feature": h["name"], "sketch": sk, "start_offset": h["z0"],
                                               "depth": h["drill"], "flip": False}, "底孔（平底；实际钻尖 118°）"))
            zm = 0.0 if h["side"] == "start" else h["z1"]
            ops.append(NativeOp("cosmetic_thread", {"feature": h["name"] + "_THR", "size": h["size"], "depth": h["depth"],
                                                    "edge_pts": [[h["tap"] / 2, 0, zm]], "diameter": h["d"],
                                                    "external": False, "tol": h["callout"].split("-")[1].split("↓")[0]},
                                "孔口圆边 → 内螺纹装饰线（失败不影响几何）"))
        for b in p.shaft_bores:  # 内孔：Front Plane(XY) 画圆，从 z0 起切到 z1
            sk = f"SK_{b['name']}"
            ops.append(NativeOp("sketch_on_plane", {"sketch": sk, "plane": "Front Plane"}))
            ops.append(NativeOp("sketch_circle", {"sketch": sk, "center": [0, 0], "diameter": b["d"]}))
            ops.append(NativeOp("keyway_cut", {"feature": b["name"], "sketch": sk, "start_offset": b["z0"],
                                               "depth": b["z1"] - b["z0"], "flip": False},
                                "FeatureCut4：起始偏移 z0、深度 = 孔深（平底）"))
            if b.get("kw"):  # 轮毂键槽在 −Y（SW 轴类主视图 *Right 转 180° 后 −Y 朝上）
                k = b["kw"]
                hw = k["width"] / 2
                ops.append(NativeOp("sketch_on_plane", {"sketch": sk + "_KW", "plane": "Front Plane"}))
                ops.append(NativeOp("sketch_polyline_closed", {"sketch": sk + "_KW", "points": [
                    [-hw, 0], [hw, 0], [hw, -k["top"]], [-hw, -k["top"]]]}))
                ops.append(NativeOp("keyway_cut", {"feature": k["name"], "sketch": sk + "_KW", "start_offset": b["z0"],
                                                   "depth": b["z1"] - b["z0"], "flip": False}, "轮毂键槽：贯通孔长"))
        for f in p.shaft_features:
            sk = f"SK_{f['name']}"
            if f["kind"] == "thread":  # 外螺纹：选起始端圆边 → 装饰螺纹（Metric Die），长度 = 螺纹长
                cyl_pt, _ = shaft_thread_points(p, f)
                ops.append(NativeOp("cosmetic_thread", {"feature": f["name"], "size": f["callout"].split("-")[0],
                                                        "depth": f["length"], "edge_pts": [cyl_pt], "diameter": f["d"],
                                                        "external": True, "tol": f["callout"].split("-")[-1]},
                                    "轴外螺纹装饰线（失败不影响几何）"))
                continue
            if f["kind"] == "gear":  # 齿轮段：Front Plane(XY) 画齿廓折线 + 外圆，从 z0 起切到 z1（齿槽 = 两轮廓之间）
                ops.append(NativeOp("sketch_on_plane", {"sketch": sk, "plane": "Front Plane"}))
                ops.append(NativeOp("sketch_polyline_closed", {"sketch": sk, "points": f["points"]}))
                ops.append(NativeOp("sketch_circle", {"sketch": sk, "center": [0, 0], "diameter": f["d"] + 2}))
                ops.append(NativeOp("keyway_cut", {"feature": f["name"], "sketch": sk, "start_offset": f["z0"],
                                                   "depth": f["z1"] - f["z0"], "flip": False},
                                    "FeatureCut4：起始偏移 z0、深度 = 齿宽，切出全部齿槽"))
                continue
            if f["kind"] == "keyway":
                # 键槽朝向：0/180 → 在 Right Plane(YZ) 画槽形，沿 ±X 切；90/270 → Top Plane(XZ)，沿 ±Y 切
                ang = round(f["angle"]) % 360
                if ang not in (0, 90, 180, 270):
                    raise CadError(CAPABILITY_UNSUPPORTED, f"keyway {f['name']}: angle must be a multiple of 90 in v0")
                plane = "Right Plane" if ang in (0, 180) else "Top Plane"
                hw = f["width"] / 2
                ops.append(NativeOp("sketch_on_plane", {"sketch": sk, "plane": plane}))
                ops.append(NativeOp("sketch_slot_3d", {"sketch": sk, "end1": [0, 0, f["z0"] + hw],
                                                       "end2": [0, 0, f["z1"] - hw], "width": f["width"]}))
                ops.append(NativeOp("keyway_cut", {"feature": f["name"], "sketch": sk,
                                                   "start_offset": f["d"] / 2 - f["depth"], "depth": f["depth"] + 1,
                                                   "flip": ang in (180, 270)},
                                    "FeatureCut4：起始偏移 d/2-t1，向外切到轴表面以外"))
            else:
                r0, r1 = f["d2"] / 2, f["d"] / 2 + 1
                ops.append(NativeOp("sketch_on_plane", {"sketch": sk, "plane": "Right Plane"}))
                ops.append(NativeOp("sketch_polyline_3d", {"sketch": sk, "points": [
                    [0, r0, f["z0"]], [0, r1, f["z0"]], [0, r1, f["z1"]], [0, r0, f["z1"]]],
                    "centerline": [[0, 0, -5], [0, 0, p.bbox.zmax + 5]]}))
                ops.append(NativeOp("revolve", {"feature": f["name"], "sketch": sk, "angle": 360, "cut": True}))
    elif p.base_type == "weldment":
        ops += weldment_ops(p)
    else:  # sheet_metal
        ops.append(NativeOp("sketch_on_plane", {"sketch": "SK_Base", "plane": "Front Plane"}))
        ops += _profile_ops(p.profile, "SK_Base")
        ops.append(_dimension_profile_op(p.profile, base))
        ops.append(NativeOp("sheet_metal_base_flange", {"feature": "Base-Flange", "sketch": "SK_Base",
                                                        "thickness": p.thickness,
                                                        "bend_radius": p.bends[0]["radius"] if p.bends else p.thickness,
                                                        "k_factor": base.k_factor}))
        for i, b in enumerate(p.bends):
            ops.append(NativeOp("sheet_metal_edge_flange", {
                "feature": f"Edge-Flange{i + 1}", "edge": b["edge"], "edge_point": list(b["edge_point"]),
                "length": b["length"], "angle": b["angle"], "radius": b["radius"], "direction": b["direction"],
                "expect_z": list(b["expect_z"]) if b["expect_z"] else None, "length_type": "outer_virtual_sharp"},
                "按底板边线中点选边 → InsertSheetMetalEdgeFlange2（材料在内、外部虚拟交点长度）→ 回读包围盒核对方向/高度"))
        if p.bends:
            ops.append(NativeOp("check_body_extent", {"axis": "z", "expect": [p.bbox.zmin, p.bbox.zmax]},
                                "翻边后实体 Z 向范围应与规划包围盒一致（方向/长度类型核对）"))

    for b in p.bosses:
        sk = f"SK_{b['name']}"
        ops.append(_face_sketch(b["face"], p.bbox, sk))
        ops += _profile_ops(b["profile"], sk)
        ops.append(NativeOp("extrude_boss", {"feature": b["name"], "sketch": sk, "depth": b["height"]}))
    for pk in p.pockets:
        sk = f"SK_{pk['name']}"
        c = pk["profile"].bbox2d
        ops.append(_face_sketch(pk["face"], p.bbox, sk, pk.get("at"), ((c[0] + c[1]) / 2, (c[2] + c[3]) / 2)))
        ops += _profile_ops(pk["profile"], sk)
        ops.append(NativeOp("extrude_cut", {"feature": pk["name"], "sketch": sk,
                                            "end": "through_all" if pk["through"] else "blind", "depth": pk["depth"]}))
    for h in p.holes:
        sk = f"SK_{h.name}"
        uv0 = h.instances[0].uv
        if h.slot:
            ops.append(_face_sketch(h.face, p.bbox, sk, h.at, uv0))
            for inst in h.instances:
                a, b, _ = h.capsule(inst)
                ops.append(NativeOp("sketch_slot_uv", {"sketch": sk, "end1": a, "end2": b, "width": h.slot["width"]}))
            ops.append(NativeOp("extrude_cut", {"feature": h.name, "sketch": sk,
                                                "end": _end(h), "depth": h.depth}))
            continue
        if holes_via == "cut":
            # 稳妥路径：拉伸切除生成孔（几何一定正确）；孔标注由出图方案以注释写入
            ops.append(_face_sketch(h.face, p.bbox, sk, h.at, uv0))
            ops.append(NativeOp("sketch_circles", {"sketch": sk, "points": [i.xyz for i in h.instances], "diameter": h.drill}))
            ops.append(NativeOp("extrude_cut", {"feature": h.name, "sketch": sk,
                                                "end": _end(h), "depth": h.depth}))
            if h.cbore or h.csink:
                d2, dep2 = (h.cbore if h.cbore else (h.csink, (h.csink - h.drill) / 2))
                ops.append(_face_sketch(h.face, p.bbox, sk + "_CB", h.at, uv0))
                ops.append(NativeOp("sketch_circles", {"sketch": sk + "_CB", "points": [i.xyz for i in h.instances],
                                                       "diameter": d2}))
                ops.append(NativeOp("extrude_cut", {"feature": h.name + "_CB", "sketch": sk + "_CB", "end": "blind",
                                                    "depth": dep2, "draft_deg": 45 if h.csink else 0}))
            if h.type == "tapped":
                # 孔口圆边上的一个点（面内偏移半径；left/right 面在 Y 方向，其余在 X 方向），供执行器选边
                r = h.drill / 2
                dx, dy = (0.0, r) if h.face in ("left", "right") else (r, 0.0)
                edge_pts = [[i.xyz[0] + dx, i.xyz[1] + dy, i.xyz[2]] for i in h.instances]
                ops.append(NativeOp("cosmetic_thread", {"feature": h.name, "size": h.size_label,
                                                        "depth": h.thread_depth, "edge_pts": edge_pts,
                                                        "diameter": h.nominal}, "装饰螺纹线（失败不影响几何）"))
            continue
        ops.append(_face_sketch(h.face, p.bbox, sk, h.at, uv0))
        ops.append(NativeOp("sketch_points", {"sketch": sk, "points": [i.xyz for i in h.instances]}))
        ops.append(NativeOp("hole_wizard", {
            "feature": h.name, "position_sketch": sk, "generic_type": HOLE_TYPE[h.type],
            "standard": "GB", "size": h.size_label if h.type != "simple" else None,
            "diameter": h.drill, "end": _end(h), "depth": h.depth,
            "thread_depth": h.thread_depth, "cbore": h.cbore, "csink": h.csink, "tolerance": h.tolerance,
        }, "预选草图点后调用 HoleWizard5，一次生成整组孔，孔标注可直接导入工程图"))
    for f in p.finishes:
        ops.append(NativeOp("select_edges", {"rule": f["edges"]}, "执行器按几何规则枚举边"))
        ops.append(NativeOp("fillet" if f["type"] == "fillet" else "chamfer",
                            {"feature": f["name"], "size": f["size"]}))
    for r in p.references.values():
        if r.auto and r.name in ("MP_Top", "MP_Bottom", "MP_End0", "MP_End1", "AX_Main"):
            pass  # 自动基准也要建，供装配引用
        if r.kind == "csys":
            ops.append(NativeOp("coordinate_system", {"feature": r.name, "origin": r.origin, "rotate_deg": r.rotate}))
        elif r.kind == "plane":
            ops.append(NativeOp("ref_plane_coincident", {"feature": r.name, "point": r.origin, "normal": r.direction}))
        elif r.kind == "axis":
            ops.append(NativeOp("ref_axis", {"feature": r.name, "point": r.origin, "direction": r.direction}))
        else:
            ops.append(NativeOp("ref_point", {"feature": r.name, "point": r.origin}))
    ops.append(NativeOp("rebuild", {"force": True}))
    ops.append(NativeOp("collect_feature_errors", {}))
    ops.append(NativeOp("measure_mass", {"expect_kg": round(p.mass_kg, 4)}, "与 IR 估算对比，偏差>3% 报警"))
    ops.append(NativeOp("save_as", {"name": spec.name, "ext": ".SLDPRT"}))
    return ops


def _dimension_profile_op(profile, base) -> NativeOp:
    from ...sketch_plan import profile_dimension_plan
    plan = profile_dimension_plan(profile, _base_links(base))
    return NativeOp("dimension_profile", {"sketch": "SK_Base", **plan},
                    "加几何关系 + 驱动尺寸使草图完全定义；含全局变量的尺寸用方程式链接（规划见 sketch_plan.py）")


def _base_links(base) -> dict[str, str]:
    links = {"thickness": to_sw_equation(base.thickness)}
    prof = base.profile
    for k in ("width", "height", "diameter", "length", "corner_radius"):
        if hasattr(prof, k):
            links[k] = to_sw_equation(getattr(prof, k))
    if hasattr(prof, "points"):
        links["points"] = [[to_sw_equation(c) for c in pt] for pt in prof.points]
    return links


# ---------------- 装配 ----------------
def compile_assembly(spec, asm, component_files: dict[str, str], library_root: str = "") -> list[NativeOp]:
    """组件用坐标系重合配合定位（swMateCOORDINATE）；配合失败时执行器退回"按位姿放置并固定"并记录警告。"""
    ops: list[NativeOp] = [NativeOp("new_document", {"kind": "assembly"})]
    for name, expr in spec.parameters.items():
        ops.append(NativeOp("add_global_variable", {"name": name, "equation": f'"{name}" = {to_sw_equation(expr)}'}))
    ops.append(NativeOp("set_custom_properties", {"properties": {"PartNo": spec.part_no, "Description": spec.title,
                                                                 **spec.properties}}))
    comps = {c.id: c for c in spec.components}
    by_id = {i.id: i for i in asm.instances}
    for inst in asm.instances:
        c = comps[inst.component]
        kind, _, ref = c.source.partition(":")
        if kind == "lib" and inst.component not in component_files:
            code, _, q = ref.partition("?")
            src = {"library": code, "query": q, "library_root": library_root, "out_name": inst.source.part_no}
        else:
            src = {"file": str(component_files[inst.component])}
        ops.append(NativeOp("add_component", {"instance": inst.id, **src,
                                              "transform": inst.transform}, "先按求得的位姿放入"))
        if c.place is None and inst.id == spec.components[0].id:
            ops.append(NativeOp("fix_component", {"instance": inst.id}))
        elif c.place is not None:
            tgt, _, tref = c.place.to.partition(".")
            tinst = None if tgt == "ASM" else (tgt if tgt in by_id else f"{tgt}#1")
            # 组件 align 坐标系的世界位姿 W 与目标坐标系的世界位姿 T；不重合（有 offset/rotate 或阵列副本）时
            # 在装配中按 W 建参考坐标系 MC_<实例>，再与组件坐标系做坐标系重合配合。
            W = matmul(inst.transform, ref_frame(inst.source.references[c.place.align]))
            T = identity() if tinst is None else matmul(by_id[tinst].transform,
                                                         ref_frame(by_id[tinst].source.references[tref]))
            frame = None
            if not same_frame(W, T):
                frame = {"name": "MC_" + inst.id.replace("#", "_"), "origin": [round(W[i][3], 6) + 0.0 for i in range(3)],
                         "rotate_deg": list(euler_xyz(W))}
            ops.append(NativeOp("mate_coordinate", {
                "instance": inst.id, "ref": c.place.align, "target_instance": tinst, "target_ref": tref,
                "frame": frame, "fallback": "fix_at_transform"},
                "坐标系重合配合" + ("（经装配参考坐标系 " + frame["name"] + "：偏移/旋转/阵列）" if frame else "")))
    ops.append(NativeOp("rebuild", {"force": True}))
    ops.append(NativeOp("collect_feature_errors", {}))
    ops.append(NativeOp("interference_check", {}, "InterferenceDetectionManager"))
    ops.append(NativeOp("save_as", {"name": spec.name, "ext": ".SLDASM"}))
    return ops


# ---------------- 工程图 ----------------
# IR 视图 → SW 模型命名视图（IR 坐标系 = SW 坐标系）。需实机核对方向，可在节点配置中覆盖。
SW_VIEW = {"top": ("*Front", 0), "bottom": ("*Back", 0), "front": ("*Bottom", 0), "back": ("*Top", 180),
           "right": ("*Right", 90), "left": ("*Left", -90), "iso": ("*Isometric", 0)}


# 轴类主视图：从 +X 看（键槽朝前），旋转 180° 使轴线 Z 向右。需实机核对。
SW_SHAFT_VIEW = ("*Right", 180)


# 原生明细表列：(列标题, 零件自定义属性；"" = SW 内置列：序号/数量)
BOM_COLUMNS = [["序号", ""], ["代号", "PartNo"], ["名称", "Description"], ["数量", ""], ["材料", "Material"],
               ["备注", ""]]


def _match_keys(row) -> list[str]:
    """把明细表行与 SW 组件对应起来的键：图号、模型文件名（part:/asm: 名称、库编码或参数化库件文件名）。"""
    src = row.get("source") or ""
    kind, _, ref = src.partition(":")
    keys = [str(row["part_no"]).lower()]
    if kind in ("part", "asm"):
        keys.append(ref.lower())
    elif kind == "lib":
        keys.append(ref.partition("?")[0].lower())
    return list(dict.fromkeys(keys))


def section_ops(plan) -> list[NativeOp]:
    """剖视图：在父视图（俯视图）上按剖切路径画折线 → CreateSectionViewAt5，放到被替代视图的位置。
    折线点为图纸坐标（mm），由与预览相同的视图投影算出。"""
    from types import SimpleNamespace

    from ...detailing import _project
    ops = []
    for s in plan.sections:
        if s.kind == "local":  # 局部剖：视图内画封闭轮廓（波浪线 + 上下边外扩）→ CreateBreakOutSection(深度)
            from ...section import AX, FRAME
            view = next(v for v in plan.views if v.type == s.view)
            region = s.geometry["region"][0]
            ha, da = FRAME[s.view]
            zlo, zhi = min(q[2] for q in region), max(q[2] for q in region)
            grow = 2.0 / view.scale  # 上下伸出外形 2 mm，确保轮廓包住剖开部分
            pts3 = [(*q[:2], q[2] - grow if q[2] <= zlo + 1e-6 else q[2] + grow if q[2] >= zhi - 1e-6 else q[2])
                    for q in region]
            pts = [_project(view, SimpleNamespace(bbox=s.bbox), q) for q in pts3]
            lo_d = s.bbox.ymin if da == "y" else s.bbox.xmin
            ops.append(NativeOp("broken_out_section", {
                "view": s.view, "label": s.label, "points": [[round(x, 3), round(y, 3)] for x, y in pts],
                "depth": round(s.path[0][2] - lo_d, 3)},
                "视图中画封闭折线（波浪线近似）并选中 → IDrawingDoc.CreateBreakOutSection(深度 m，自视图前表面算起)"))
            continue
        parent = next(v for v in plan.views if v.type == s.parent)
        target = next(v for v in plan.views if v.type == s.view)
        pts = [_project(parent, SimpleNamespace(bbox=s.bbox), q) for q in s.cut_points]
        ops.append(NativeOp("section_cut", {
            "view": s.view, "parent": s.parent, "label": s.label, "kind": s.kind,
            "points": [[round(x, 3), round(y, 3)] for x, y in pts], "center": target.origin_on_sheet},
            "父视图中画剖切折线并选中 → IDrawingDoc.CreateSectionViewAt5（阶梯剖用折线；半剖用 L 形线）"))
    return ops


def _gear_drawing_ops(plan, model_file: str) -> list[NativeOp]:
    """齿轮（GB/T 4459.2）：左视图 = 端面（模型 XY，SW *Front）；主视图 = 左视图上过中心竖直剖切线生成的全剖视图。
    尺寸选两条边线（extra pick_pts）标注；分度线/分度圆不是模型几何，原生图上不生成（warn）。参数表用通用表格。"""
    main, end = plan.views[0], plan.views[1]
    ex, ey = end.origin_on_sheet
    half = end.size_mm[1] / 2
    ops = [NativeOp("model_view", {"view": "left", "sw_view": "*Front", "model": model_file, "center": end.origin_on_sheet,
                                   "scale": end.scale, "rotate_deg": 0, "display": "hidden_visible"}, "端面视图"),
           NativeOp("section_cut", {"view": "front", "parent": "left", "label": "A", "kind": "full",
                                    "points": [[round(ex, 3), round(ey + half + 5, 3)], [round(ex, 3), round(ey - half - 5, 3)]],
                                    "center": main.origin_on_sheet},
                    "端面视图过中心（键槽）画竖直剖切线 → CreateSectionViewAt5，放到主视图位置"),
           NativeOp("center_marks", {"view": "left", "types": 5, "connection": 0})]
    skipped = []
    for d in plan.dims:
        ex_ = d.extra or {}
        if d.kind in ("diameter", "linear"):
            if not ex_.get("pick_pts"):
                skipped.append(d.text)
                continue
            ops.append(NativeOp("shaft_dimension", {"view": d.view, "kind": d.kind, "text": d.text, "at": None,
                                                    "ref": d.ref, "pts": ex_["pick_pts"], "pos": ex_.get("sheet_pos"),
                                                    "orient": ex_.get("orient")}, "选两条边线 AddDimension2；配合公差走 SW 原生"))
        elif d.kind == "callout" and ex_.get("label"):
            lb = ex_["label"]
            ops.append(NativeOp("leader_note", {"view": d.view, "text": d.text, "at_model": None, "ref": d.ref,
                                                "text_pos": lb["text_at"], "anchor": lb["anchor"]}, "孔径引线标注"))
        elif d.kind == "roughness":
            ops.append(NativeOp("surface_finish", {"view": d.view, "ref": d.ref, "at": ex_.get("sheet_at"), "pos": None,
                                                   "fallback_text": f"√{d.text}", "at_model": None, "ra": d.value,
                                                   "leader": False}))
        elif d.kind == "datum":
            ops.append(NativeOp("datum_tag", {"view": d.view, "ref": d.ref, "at": ex_["sheet_at"], "pos": ex_["sheet_pos"],
                                              "fallback_text": f"基准 {d.text}", "at_model": None, "label": d.text}))
        elif d.kind == "gtol":
            sym, zone, *datums = ex_["cells"]
            ops.append(NativeOp("gtol", {"view": d.view, "ref": d.ref, "at": ex_["sheet_at"], "pos": ex_["sheet_pos"],
                                         "fallback_text": d.text.replace(" | ", " "), "at_model": None,
                                         "type": "circular_runout", "diameter": False, "value": zone, "datums": datums,
                                         "leader": True}, "齿坯跳动"))
    if skipped:
        ops.append(NativeOp("warn", {"message": "齿轮分度线/分度圆不是模型几何，原生图未标：" + "、".join(skipped)}))
    from ...gear_drawing import param_table_rows
    t = param_table_rows(plan)
    ops.append(NativeOp("table", {"origin": list(t["origin"]), "columns": [c for c, _ in t["columns"]],
                                  "widths": [w for _, w in t["columns"]], "rows": [list(r) for r in t["rows"]],
                                  "title": "齿轮参数表"}, "图框右上角齿轮参数表（InsertTableAnnotation2）"))
    from ...detailing import general_roughness_at, notes_origin
    if plan.title_block.get("surface_finish"):
        gx, gy = general_roughness_at(plan)
        ops.append(NativeOp("note", {"text": f"其余 √{plan.title_block['surface_finish']} (√)", "anchor": "above_notes",
                                     "at": [gx, gy + 4.5]}))
    nx, ny = notes_origin(plan)
    ops.append(NativeOp("note", {"text": "\n".join(plan.notes), "anchor": "above_title_block",
                                 "at": [nx, ny + 5 * max(len(plan.notes) - 1, 0) + 3.5]}))
    ops.append(NativeOp("readback_annotations", {}, "回读尺寸/注释数量供出图检查"))
    ops.append(NativeOp("save_as", {"name": plan.name, "ext": ".SLDDRW"}))
    ops.append(NativeOp("export", {"format": "pdf"}))
    return ops


def compile_drawing(plan, model_file: str, sheet_templates: dict[str, str] | None = None,
                    model_features: list | None = None) -> list[NativeOp]:
    sheet_templates = sheet_templates or {}
    model_features = model_features or []
    W, H = plan.sheet_size
    ops: list[NativeOp] = [
        NativeOp("new_drawing", {"sheet": plan.sheet, "template": sheet_templates.get(plan.sheet, ""),
                                 "width": W, "height": H, "scale": plan.scale}),
        NativeOp("set_custom_properties", {"properties": {
            "PartNo": plan.title_block.get("part_no", ""), "Description": plan.title_block.get("name", ""),
            "Material": plan.title_block.get("material", ""), "Scale": plan.title_block.get("scale", ""),
            "Weight": plan.title_block.get("mass", ""), "DrawnBy": plan.title_block.get("designer", ""),
            "CheckedBy": plan.title_block.get("checker", ""), "Date": plan.title_block.get("date", ""),
            "Revision": plan.title_block.get("revision", "")}},
            "模板标题栏用 $PRP 链接这些属性"),
    ]
    if plan.layout == "gear":
        return ops + _gear_drawing_ops(plan, model_file)
    for v in plan.views:
        if v.type == "flat_pattern":
            ops.append(NativeOp("flat_pattern_view", {"model": model_file, "center": v.origin_on_sheet, "scale": v.scale}))
            continue
        if v.type == "section":
            f = next((x for x in model_features if x["name"] == v.target), None)
            z = (f["z0"] + f["z1"]) / 2 if f else 0.0
            main = plan.views[0]  # 轴类主视图：轴线水平，左端 = z 0（与 render_shaft 一致）
            (mcx, mcy), (mw, mh) = main.origin_on_sheet, main.size_mm
            from ...detailing import shaft_sx
            mx = shaft_sx(main, None, z)
            ops.append(NativeOp("section_view", {
                "label": v.label, "parent": main.type, "z": z, "center": v.origin_on_sheet,
                "points": [[round(mx, 3), round(mcy + mh / 2 + 5, 3)], [round(mx, 3), round(mcy - mh / 2 - 5, 3)]]},
                "主视图画垂直轴线的剖切线（上下伸出 5 mm）→ CreateSectionViewAt5（只显示切面）→ 解除对齐，移到规划位置"))
            continue
        if v.type == "detail":  # 局部放大图：主视图上画圆 → CreateDetailViewAt4（放大比例、罗马数字）
            from fractions import Fraction
            fr = Fraction(v.scale).limit_denominator(10)
            ops.append(NativeOp("detail_view", {
                "label": v.label, "parent": plan.views[0].type, "circle": list(v.detail["circle"]),
                "center": v.origin_on_sheet, "scale": [fr.numerator, fr.denominator]},
                "父视图中 CreateCircleByRadius → 选中 → IDrawingDoc.CreateDetailViewAt4（锯齿轮廓 = GB 波浪线）"))
            continue
        if any(s.view == v.type and s.kind != "local" for s in plan.sections):
            continue  # 由剖视图替代（在父视图之后生成）
        name, angle = (SW_SHAFT_VIEW if plan.layout == "shaft" and v.type == "front"
                       else ("*Back", 180) if plan.layout == "shaft" and v.type == "left"  # 从左端看，−Y 朝上（同主视图）
                       else SW_VIEW[v.type])
        ops.append(NativeOp("model_view", {"view": v.type, "sw_view": name, "model": model_file,
                                           "center": v.origin_on_sheet, "scale": v.scale * (0.5 if v.type == "iso" else 1),
                                           "rotate_deg": angle, "display": "hidden_removed" if v.type == "iso" else "hidden_visible"}))
        if v.breaks:  # 断开画法（GB/T 4458.1）：竖直断裂线，模型 z（mm，轴左端 = 0）；断开后移回规划中心
            from ...detailing import BREAK_GAP
            ops.append(NativeOp("break_view", {"view": v.type, "breaks": [list(b) for b in v.breaks], "scale": v.scale,
                                               "gap": BREAK_GAP, "center": v.origin_on_sheet},
                                "IView.InsertBreak3（竖直、曲线断裂线）→ IDrawingDoc.BreakView → 视图移到规划中心"))
    ops += section_ops(plan)
    for v in plan.views:  # 空心轴/套类：主视图原位全剖 = 包住整个视图的局部剖，深度到轴线
        if v.full_section:
            ops.append(NativeOp("broken_out_section", {
                "view": v.type, "label": "", "points": [list(q) for q in v.full_section["boundary"]],
                "depth": v.full_section["depth"]},
                "视图外框矩形 → CreateBreakOutSection(深度 = 外圆半径，剖到轴线)：原位全剖"))
    for v in plan.views:  # 孔/槽中心符号（中心线）：每个正投影视图自动插入（剖视图建好后，替代视图同名）
        if v.type != "iso" and plan.model_kind == "part" and plan.layout != "shaft":
            ops.append(NativeOp("center_marks", {"view": v.type, "types": 5, "connection": 0},
                                "IView.AutoInsertCenterMarks2：孔、槽中心线"))
    # 尺寸：有孔坐标的视图用 SW 自动坐标标注（左下角为基准），其余视图只标总体尺寸
    ordinate_views = sorted({d.view for d in plan.dims if d.kind == "ordinate"})
    for vt in ordinate_views:
        ops.append(NativeOp("auto_dimension", {"view": vt, "scheme": "ordinate", "origin": "lower_left"},
                            "IDrawingDoc.AutoDimension"))
    for d in plan.dims:
        ex = d.extra or {}
        if d.kind == "linear" and d.at is None and (d.view not in ordinate_views or ex.get("pitch") or ex.get("install")):  # 图纸坐标一律 mm（executor 内转 m）：pts = 两条被标注边线上的点，pos = 尺寸线/文字位置
            ops.append(NativeOp("overall_dimension", {"view": d.view, "axis": d.axis, "value": d.value, "ref": d.ref,
                                                      "pts": ex.get("sheet_pts"), "pos": ex.get("sheet_pos"),
                                                      "orient": ex.get("orient"),
                                                      **({"prefix": ex["prefix"], "suffix": ex.get("suffix", "")}
                                                         if ex.get("prefix") else {})},
                                "选两条轮廓边线 AddDimension2；等距尺寸加前后缀 n×p(=<DIM>)"))
    if plan.gear_table:  # 齿轮轴参数表
        t = plan.gear_table
        ops.append(NativeOp("table", {"origin": list(t["origin"]), "columns": [c for c, _ in t["columns"]],
                                      "widths": [w for _, w in t["columns"]], "rows": [list(r) for r in t["rows"]],
                                      "title": "齿轮参数表"}, "图框右上角齿轮参数表"))
    if plan.layout == "shaft":
        for d in plan.dims:  # 局部放大图内尺寸（槽宽、槽深）：放大图坐标下选边
            ex = d.extra or {}
            if d.view == "detail" and ex.get("pick_pts"):
                ops.append(NativeOp("shaft_dimension", {"view": f"detail:{ex['detail']}", "kind": "linear", "text": d.text,
                                                        "at": None, "ref": d.ref, "pts": ex["pick_pts"],
                                                        "pos": ex.get("sheet_pos"), "orient": ex.get("orient")},
                                    "放大图中选两条边线 AddDimension2"))
        for d in plan.dims:  # 左视图（套类内键槽）：选边点标注，同齿轮图
            ex = d.extra or {}
            if d.view == "left" and d.kind == "linear" and ex.get("pick_pts"):
                ops.append(NativeOp("shaft_dimension", {"view": "left", "kind": "linear", "text": d.text, "at": None,
                                                        "ref": d.ref, "pts": ex["pick_pts"], "pos": ex.get("sheet_pos"),
                                                        "orient": ex.get("orient")}, "键槽宽 JS9 / d+t2：选两条边线"))
        for d in plan.dims:
            if d.view in ("left", "detail"):
                continue
            if d.kind == "diameter" or (d.kind == "linear" and d.at is not None):
                ex = d.extra or {}  # 图纸坐标一律 mm（executor 内转 m）
                view = f"section:{ex['section']}" if ex.get("section") else d.view  # 移出断面图按标签区分
                ops.append(NativeOp("shaft_dimension", {"view": view, "kind": d.kind, "text": d.text,
                                                        "at": d.at, "ref": d.ref, "pts": ex.get("sheet_pts"),
                                                        "pos": ex.get("sheet_pos"), "orient": ex.get("orient"),
                                                        **({"arc_end": list(ex["arc_end"])} if ex.get("arc_end") else {})},
                                    "选两条边线 AddDimension2；直径用文字覆盖带公差带"))
    for d in plan.dims:
        if d.kind in ("callout", "radius") or d.kind == "diameter" and plan.layout != "shaft":
            lb = (d.extra or {}).get("label")
            pos = (d.extra or {}).get("sheet_pos")  # 无引线的说明文字（如展开图折弯说明）：文字左下角
            ops.append(NativeOp("leader_note", {"view": d.view, "text": d.text, "at_model": d.at, "ref": d.ref,
                                                **({"text_pos": lb["text_at"], "anchor": lb["anchor"]} if lb else
                                                   {"text_pos": pos} if pos else {})}))
    for d in plan.dims:  # 局部热处理部位：视图中画线，改为粗点画线
        if d.kind == "chain":
            ops.append(NativeOp("view_line", {"view": d.view, "points": [list(q) for q in d.extra["sheet_pts"]],
                                              "style": "CENTER", "weight": "thick", "ref": d.ref},
                                "视图草图直线 → SetLineStyle 点画线 + SetLineWidth 粗（GB/T 4457.4 粗点画线）"))
    for d in plan.dims:  # 焊缝符号：原生 InsertWeldSymbol3，失败时退回带引线注释
        if d.kind == "weld":
            ex = d.extra or {}
            text = (f"{d.text}{'/' + d.text if ex.get('both_sides') else ''} ◣{' ○' if ex.get('all_around') else ''}"
                    f"  {ex.get('count', 1)}处  {ex.get('process', '')}")
            ops.append(NativeOp("weld_symbol", {
                "view": d.view, "size": ex.get("size", d.value), "both_sides": bool(ex.get("both_sides")),
                "all_around": bool(ex.get("all_around")), "count": ex.get("count", 1), "process": ex.get("process", ""),
                "at": [v / 1000 for v in ex.get("sheet_at", (0, 0))], "pos": [v / 1000 for v in ex.get("sheet_pos", (0, 0))],
                "at_model": d.at, "fallback_text": text, "ref": d.ref},
                "选中焊缝处模型边线 → InsertWeldSymbol3 → SetText 角焊缝/焊脚/处数，周围焊、尾部焊接方法"))
    if plan.hole_table:
        ops.append(NativeOp("hole_tags", {"view": plan.hole_table_view, "tags": [
            {"text": d.text, "at": d.extra["sheet_at"], "origin": d.kind == "origin"}
            for d in plan.dims if d.kind in ("tag", "origin") and d.view == plan.hole_table_view]},
            "孔号注释放在孔旁；坐标原点标 X0 Y0"))
        ops.append(NativeOp("hole_table", {"box": plan.hole_table_box, "columns": ["孔号", "X", "Y", "规格"],
                                           "widths": [14, 22, 22, 122],
                                           "rows": [[r["tag"], f"{r['x']:g}", f"{r['y']:g}", r["size"]]
                                                    for r in plan.hole_table]},
                            "InsertTableAnnotation2 通用表格逐格写入 → 回读核对（D017）"))
    if plan.cut_list:
        ops.append(NativeOp("weldment_cut_list", {"view": plan.views[0].type, "anchor": "above_title_block",
                                                  "rows": [{k: r[k] for k in ("item", "kind", "spec", "qty", "length_mm",
                                                                             "size_mm") if k in r} for r in plan.cut_list],
                                                  "header_bottom": True},
                            "IView.InsertWeldmentTable；失败时以注释输出"))
        ops.append(NativeOp("auto_balloon", {"view": plan.views[0].type}))
    gdt = [d for d in plan.dims if d.kind in ("datum", "gtol", "roughness")]
    if gdt:
        ops.append(NativeOp("probe_gdt_constants", {}, "把类型库中 swGcs*/swMc*/swSF* 常量记入 oplog（实机核对枚举名）"))
    for d in gdt:  # 精度标注：原生符号；常量/接口不可用时执行器退回带引线注释并登记（D019）
        ex = d.extra or {}
        text = {"datum": f"基准 {d.text}", "gtol": d.text.replace(" | ", " "), "roughness": f"√{d.text}"}[d.kind]
        common = {"view": d.view, "ref": d.ref, "at": ex.get("sheet_at"), "pos": ex.get("sheet_pos"),
                  "fallback_text": text, "at_model": d.at}
        if d.kind == "datum":
            ops.append(NativeOp("datum_tag", {**common, "label": d.text},
                                "选中边线 → InsertDatumTag2 → SetLabel → 放到外法向位置"))
        elif d.kind == "gtol":
            sym, zone, *datums = ex.get("cells", ["", "", ""])
            ops.append(NativeOp("gtol", {**common, "type": d.ref.split(":")[1].split("@")[0],
                                         "diameter": zone.startswith("Φ"), "value": zone.lstrip("Φ"),
                                         "datums": datums[:3], "leader": ex.get("attach") != "callout"},
                                "选中被测要素边线 → InsertGtol → SetFrameSymbols2/SetFrameValues2"))
        else:
            ops.append(NativeOp("surface_finish", {**common, "ra": d.value,
                                                   "leader": tuple(ex.get("normal", (0, 1))) != (0, 1)},
                                "选中边线 → IModelDocExtension.InsertSurfaceFinishSymbol3（去除材料，Ra 值）"))
    if plan.title_block.get("surface_finish") and plan.model_kind == "part":
        sf = plan.title_block["surface_finish"]
        from ...detailing import general_roughness_at
        gx, gy = general_roughness_at(plan)
        ops.append(NativeOp("note", {"text": "其余 ∅√（不去除材料）" if sf == "cast" else f"其余 √{sf} (√)",
                                     "anchor": "above_notes", "at": [gx, gy + 4.5]}, "位置避开视图（与 SVG 同）"))
    from ...detailing import notes_origin
    nx, ny = notes_origin(plan)
    ops.append(NativeOp("note", {"text": "\n".join(plan.notes), "anchor": "above_title_block",
                                 "at": [nx, ny + 5 * max(len(plan.notes) - 1, 0) + 3.5]}))
    if plan.revisions:  # 原生修订表；失败时执行器退回注释
        from ...detailing import REV_COLUMNS, revision_rows, revision_table_origin
        rx, ry = revision_table_origin(plan)
        rrows = [[r.get("rev", ""), r.get("description", ""), r.get("date", ""), r.get("by", "")]
                 for r in revision_rows(plan)]
        ops.append(NativeOp("revision_table", {
            "origin": (rx, ry), "columns": [n for n, _ in REV_COLUMNS], "widths": [w for _, w in REV_COLUMNS],
            "rows": rrows, "fallback_text": "更改记录：\n" + "\n".join("  ".join(r) for r in rrows)},
            "InsertRevisionTable2（标题栏左侧）→ AddRevision 逐行 → 写单元格 → 回读行数（D020）"))
    if plan.bom:
        rows = [{"item": r["item"], "part_no": r["part_no"], "qty": r["qty"], "title": r["title"],
                 "material": r["material"] or r["spec"], "keys": _match_keys(r)} for r in plan.bom]
        ops.append(NativeOp("bom_table", {"view": plan.views[0].type, "anchor": "above_title_block",
                                          "columns": BOM_COLUMNS, "rows": rows, "header_bottom": True},
                            "InsertBomTable4 → 按方案序号重排行（MoveRow）→ 回读核对（D011）"))
    if plan.balloons and plan.model_kind == "assembly" and all("pos" in b for b in plan.balloons):
        keys = {r["item"]: _match_keys(r) for r in plan.bom} if plan.bom else {}
        ops.append(NativeOp("bom_balloons", {"view": plan.views[0].type, "balloons": [
            {"item": b["item"], "keys": keys.get(b["item"], [b["part_no"].lower()]),
             "anchor": [v / 1000 for v in b["anchor"]], "pos": [v / 1000 for v in b["pos"]]} for b in plan.balloons]},
            "逐个零件 InsertBOMBalloon2 并放到规划位置；找不到零件时退回 AutoBalloon5；回读核对（D012）"))
    ops.append(NativeOp("readback_annotations", {}, "回读尺寸/注释数量供出图检查"))
    ops.append(NativeOp("save_as", {"name": plan.name, "ext": ".SLDDRW"}))
    ops.append(NativeOp("export", {"format": "pdf"}))
    return ops
