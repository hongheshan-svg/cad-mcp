"""Creo 编译器：ResolvedPart → CREOSON / 企业扩展命令计划。

J-Link/CREOSON 不能直接创建草图特征。从零建模采用"起始件 + UDF 库"：
  - 起始件 cadmcp_start.prt：已含 CS_Origin、基准面、单位 mmNs、标准参数（PART_NO/TITLE/MATERIAL…）
  - UDF 库：udf_base_rect(W,H,T,R)、udf_base_circle、udf_shaft_stepN、udf_csys_offset(X,Y,Z)、
            udf_hole_{type}_{size}(深度) 、udf_pocket_rect …（由 Creo 工程师一次性建好）
  - 企业扩展命令 cadmcp_udf_place 在 J-Link 中放置 UDF（pfcUDFCustomCreateInstructions）
若有 TOOLKIT 授权，可改为元素树（ProFeatureCreate）直接建特征，计划格式不变。
"""
from __future__ import annotations

from ...ir.part import PartSpec
from ...ir.resolve import ResolvedPart
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
