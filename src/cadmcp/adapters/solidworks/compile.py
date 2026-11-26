"""SolidWorks 编译器：ResolvedPart → NativeOp 列表。

约定：IR 坐标系 = SW 模型坐标系；IR 的 XY 草图面 = SW "Front Plane"（前视基准面，法向 +Z）。
所有长度在计划中为 mm，执行器负责换算为 SW API 的米/弧度。
参数化：spec.parameters → 全局变量；尺寸通过方程式链接到全局变量，模型在 SW 里仍可手工改参。
"""
from __future__ import annotations

from ...errors import CAPABILITY_UNSUPPORTED, CadError

from ...ir.part import PartSpec
from ...ir.resolve import ResolvedPart
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


def compile_part(spec: PartSpec, p: ResolvedPart, template: str = "", holes_via: str = "cut") -> list[NativeOp]:
    def _end(h) -> str:
        # 钣金翻边上的"通孔"只穿板厚：through_all 会连对面翻边一起打穿
        if p.base_type == "sheet_metal" and h.face not in ("top", "bottom"):
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
        ops.append(NativeOp("dimension_profile", {"sketch": "SK_Base", "links": _base_links(base)},
                            "加尺寸并用方程式链接全局变量"))
        ops.append(NativeOp("extrude_boss", {"feature": "Base", "sketch": "SK_Base", "depth": p.thickness,
                                             "depth_equation": to_sw_equation(base.thickness)}))
    elif p.base_type == "shaft":
        ops.append(NativeOp("sketch_on_plane", {"sketch": "SK_Shaft", "plane": "Right Plane"}))
        ops.append(NativeOp("sketch_shaft_half_profile", {"sketch": "SK_Shaft", "steps": p.shaft_steps,
                                                           "axis": "Z", "chamfer": p.shaft_chamfer}))
        ops.append(NativeOp("revolve", {"feature": "Base", "sketch": "SK_Shaft", "angle": 360}))
        for f in p.shaft_features:
            sk = f"SK_{f['name']}"
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
    else:  # sheet_metal
        ops.append(NativeOp("sketch_on_plane", {"sketch": "SK_Base", "plane": "Front Plane"}))
        ops += _profile_ops(p.profile, "SK_Base")
        ops.append(NativeOp("sheet_metal_base_flange", {"feature": "Base-Flange", "sketch": "SK_Base",
                                                        "thickness": p.thickness,
                                                        "bend_radius": p.bends[0]["radius"] if p.bends else p.thickness,
                                                        "k_factor": base.k_factor}))
        for i, b in enumerate(p.bends):
            ops.append(NativeOp("sheet_metal_edge_flange", {"feature": f"Edge-Flange{i + 1}", "edge": b["edge"],
                                                            "length": b["length"], "angle": b["angle"],
                                                            "radius": b["radius"], "length_type": "outer_virtual_sharp"}))

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
                ops.append(NativeOp("cosmetic_thread", {"feature": h.name, "size": h.size_label,
                                                        "depth": h.thread_depth}, "装饰螺纹线（失败不影响几何）"))
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


def _base_links(base) -> dict[str, str]:
    links = {"thickness": to_sw_equation(base.thickness)}
    prof = base.profile
    for k in ("width", "height", "diameter", "length", "corner_radius"):
        if hasattr(prof, k):
            links[k] = to_sw_equation(getattr(prof, k))
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
            ops.append(NativeOp("mate_coordinate", {
                "instance": inst.id, "ref": c.place.align,
                "target_instance": None if tgt == "ASM" else (tgt if tgt in {i.id for i in asm.instances} else f"{tgt}#1"),
                "target_ref": tref, "offset": list(c.place.offset), "rotate": list(c.place.rotate),
                "fallback": "fix_at_transform"}))
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
            "CheckedBy": plan.title_block.get("checker", ""), "Date": plan.title_block.get("date", "")}},
            "模板标题栏用 $PRP 链接这些属性"),
    ]
    for v in plan.views:
        if v.type == "flat_pattern":
            ops.append(NativeOp("flat_pattern_view", {"model": model_file, "center": v.origin_on_sheet, "scale": v.scale}))
            continue
        if v.type == "section":
            f = next((x for x in model_features if x["name"] == v.target), None)
            ops.append(NativeOp("section_view", {"label": v.label, "parent": plan.views[0].type,
                                                 "z": (f["z0"] + f["z1"]) / 2 if f else 0.0,
                                                 "center": v.origin_on_sheet}, "移出断面图（剖切线垂直轴线）"))
            continue
        name, angle = SW_SHAFT_VIEW if plan.layout == "shaft" and v.type == "front" else SW_VIEW[v.type]
        ops.append(NativeOp("model_view", {"view": v.type, "sw_view": name, "model": model_file,
                                           "center": v.origin_on_sheet, "scale": v.scale * (0.5 if v.type == "iso" else 1),
                                           "rotate_deg": angle, "display": "hidden_removed" if v.type == "iso" else "hidden_visible"}))
    # 尺寸：有孔坐标的视图用 SW 自动坐标标注（左下角为基准），其余视图只标总体尺寸
    ordinate_views = sorted({d.view for d in plan.dims if d.kind == "ordinate"})
    for vt in ordinate_views:
        ops.append(NativeOp("auto_dimension", {"view": vt, "scheme": "ordinate", "origin": "lower_left"},
                            "IDrawingDoc.AutoDimension"))
    for d in plan.dims:
        if d.kind == "linear" and d.view not in ordinate_views and d.at is None:
            ops.append(NativeOp("overall_dimension", {"view": d.view, "axis": d.axis, "value": d.value}))
    if plan.layout == "shaft":
        for d in plan.dims:
            if d.kind == "diameter" or (d.kind == "linear" and d.at is not None):
                ops.append(NativeOp("shaft_dimension", {"view": d.view, "kind": d.kind, "text": d.text,
                                                        "at": d.at, "ref": d.ref}))
    for d in plan.dims:
        if d.kind in ("callout", "radius") or d.kind == "diameter" and plan.layout != "shaft":
            ops.append(NativeOp("leader_note", {"view": d.view, "text": d.text, "at_model": d.at, "ref": d.ref}))
    for d in plan.dims:  # 精度标注：先用注释承载（P1：InsertGtol / InsertDatumTag2 / InsertSurfaceFinishSymbol3 原生符号）
        if d.kind in ("datum", "gtol", "roughness"):
            text = {"datum": f"基准 {d.text}", "gtol": d.text.replace(" | ", " "), "roughness": f"√{d.text}"}[d.kind]
            ops.append(NativeOp("leader_note", {"view": d.view, "text": text, "at_model": d.at, "ref": d.ref}))
    if plan.title_block.get("surface_finish") and plan.model_kind == "part":
        ops.append(NativeOp("note", {"text": f"其余 √{plan.title_block['surface_finish']} (√)", "anchor": "above_notes"}))
    ops.append(NativeOp("note", {"text": "\n".join(plan.notes), "anchor": "above_title_block"}))
    if plan.bom:
        ops.append(NativeOp("bom_table", {"view": plan.views[0].type, "anchor": "above_title_block",
                                          "columns": ["序号", "图号", "名称", "数量", "材料/规格"]}))
        ops.append(NativeOp("auto_balloon", {"view": plan.views[0].type}))
    ops.append(NativeOp("readback_annotations", {}, "回读尺寸/注释数量供出图检查"))
    ops.append(NativeOp("save_as", {"name": plan.name, "ext": ".SLDDRW"}))
    ops.append(NativeOp("export", {"format": "pdf"}))
    return ops
