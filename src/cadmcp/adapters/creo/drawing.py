"""Creo 工程图：DrawingPlan → creopyson 调用计划，以及逐 op 执行器。

分层：DrawingPlan 是 CAD 无关的规划；这里只做翻译。能力边界（CREOSON 的现实）：
  - 能做：新建图纸（模板）、general / 投影视图、比例、模型参数（驱动模板标题栏）、再生、存 .drw、导 PDF、回读视图列表；
  - 不能做（creopyson 没有对应函数）：创建尺寸/注释/表格/剖视图/几何公差。这些规划成企业扩展命令
    `ext.cadmcp_*`（CREOSON 自定义命令，J-Link 实现，与 compile.py 的 ext.* 约定一致）。
    扩展不可用时执行器只记警告，并在计划里放一条明确的 `warn` 提示，绝不静默。

坐标假设（需实机核对，见 docs/WINDOWS_TEST.md）：
  - origin_on_sheet 为图纸左下角起算的 mm；模板图纸单位为 mm 时 sheet_unit_scale=1.0，英寸模板填 1/25.4。
  - 模型命名视图 FRONT/TOP/LEFT/RIGHT/BOTTOM/BACK/TRIMETRIC 的朝向与规划一致（起始件按此定向）。
  - 模板 drawing setup 的 projection_type 须与 plan.projection 一致（first_angle），creopyson 无法设置。
"""
from __future__ import annotations

import json
import time
import traceback
from pathlib import Path
from typing import Any

from ...detailing import BOM_COLS, bom_display, dim_text_pos, gtol_frame_pos
from ..native import NativeOp

# 规划视图类型 → Creo 模型命名视图（可由配置 view_map 覆盖）
CREO_VIEW = {"front": "FRONT", "back": "BACK", "top": "TOP", "bottom": "BOTTOM", "left": "LEFT",
             "right": "RIGHT", "iso": "TRIMETRIC"}
PROJECTED = {"top", "bottom", "left", "right"}  # 由主视图投影得到的视图
TITLE_PARAMS = {  # 模型参数 → 标题栏字段（模板里用 &PART_NO 等引用）
    "PART_NO": "part_no", "TITLE": "name", "MATERIAL": "material", "SCALE": "scale", "MASS": "mass",
    "DRAWN_BY": "designer", "CHECKED_BY": "checker", "DATE": "date", "REVISION": "revision"}

# 失败只记警告、不中断出图的 creoson 方法（其余 creoson 失败即终止；ext.* 一律只记警告）
NON_CRITICAL = {"parameter_set", "drawing_create_proj_view", "drawing_regenerate", "drawing_list_views",
                "interface_export_pdf"}


def _pt(xy: tuple[float, float], k: float) -> dict[str, float]:
    return {"x": round(xy[0] * k, 4), "y": round(xy[1] * k, 4), "z": 0.0}


def view_name(vtype: str) -> str:
    return f"V_{vtype.upper()}"


def detail_view_name(label: str) -> str:
    """局部放大图视图别名：I → V_DET_I"""
    return "V_DET_" + label


def section_view_name(label: str) -> str:
    """移出断面图视图名：A-A → V_SEC_A"""
    return "V_SEC_" + (label.split("-")[0] or "X")


def shaft_section_z(plan, v) -> float:
    """移出断面的轴向位置（IR Z，mm）：轴类主视图轴线水平、左端为 z=0（detailing 中断面图放在剖切位置正下方）。"""
    if getattr(v, "cut_z", None) is not None:  # 规划层定的剖切位置（断面图互相推开后不再在剖切位置正下方）
        return round(v.cut_z, 4)
    main = plan.views[0]
    return round((v.origin_on_sheet[0] - (main.origin_on_sheet[0] - main.size_mm[0] / 2)) / main.scale, 4)


def compile_drawing(plan, model_file: str, templates: dict[str, str] | None = None, sheet_unit_scale: float = 1.0,
                    view_map: dict[str, str] | None = None, toolkit: bool = False) -> list[NativeOp]:
    """DrawingPlan → 操作计划。op 名 `creoson.<creopyson 方法>` / `ext.<扩展命令>` / `tk.<cadmcp_tk 命令>` / `warn`（仅提示）。
    toolkit=True：视图仍由 CREOSON 建，细节（尺寸/注释/符号/表格/剖视/球标）逐项走 cadmcp_tk（docs/CREO_TOOLKIT.md §3.3）；
    否则走粗粒度 CREOSON 扩展 ext.cadmcp_*。
    args 中的 "$drawing" / "$out_dir" 在执行时替换为 drawing_create 返回的图纸名 / 输出目录。"""
    templates = templates or {}
    vm = {**CREO_VIEW, **(view_map or {})}
    k = sheet_unit_scale
    model = Path(model_file).name
    tpl = templates.get(plan.sheet) or templates.get(f"drawing_{plan.sheet}", "")
    ops: list[NativeOp] = [
        NativeOp("creoson.file_open", {"file_": model, "dirname": str(Path(model_file).parent), "display": True,
                                       "activate": True}, "打开模型（图纸引用它）"),
    ]
    for p, key in TITLE_PARAMS.items():  # 模型参数驱动模板中的参数化标题栏
        ops.append(NativeOp("creoson.parameter_set", {"name": p, "value": str(plan.title_block.get(key, "")),
                                                      "file_": model, "type_": "STRING"},
                            f"模板标题栏用 &{p} 引用"))
    if not tpl:
        ops.append(NativeOp("warn", {"message": f"配置中没有图幅 {plan.sheet} 的 Creo 图纸模板"
                                                f"（templates.drawing_{plan.sheet}），drawing_create 将失败"}))
    ops.append(NativeOp("creoson.drawing_create", {"template": tpl, "model": model, "drawing": plan.name or None,
                                                   "scale": plan.scale, "display": True, "activate": True},
                        f"图幅 {plan.sheet}；模板来自配置 templates.drawing_{plan.sheet}"))
    if plan.layout == "shaft":
        ops.append(NativeOp("warn", {"message": "轴类图主视图需轴线水平：creopyson 不能改视图朝向，"
                                                "请在起始件/模板中把 FRONT 命名视图定向为轴线水平"}))
    if plan.projection != "first_angle":
        ops.append(NativeOp("warn", {"message": f"投影角 {plan.projection}：需模板 projection_type 与之一致，creopyson 无法设置"}))

    if plan.layout == "gear":
        return ops + _gear_view_ops(plan, model, vm, k, toolkit)
    front = next((v for v in plan.views if v.type == "front"), None)
    # 由剖视图替代的视图：扩展路径整体由扩展生成；TOOLKIT 路径先照常建投影视图，再由 drw.section 设为剖视
    replaced = set() if toolkit else {s.view for s in plan.sections if s.kind != "local"}
    for v in plan.views:
        if toolkit and v.type == "section":  # 移出断面：沿轴线观察的一般视图，随后 drw.removed_section 设为断面
            ops.append(NativeOp("creoson.drawing_create_gen_view", {
                "model_view": vm.get("section", "TOP"), "point": _pt(v.origin_on_sheet, k), "drawing": "$drawing",
                "view": section_view_name(v.label), "sheet": 1, "model": model, "scale": v.scale},
                f"移出断面 {v.label}：沿轴线（IR Z）观察"))
            continue
        if v.type in ("section", "flat_pattern", "detail") or v.type in replaced:
            continue  # 局部放大图：TOOLKIT 路径由 tk.drw.detail_view 建（依附父视图），扩展路径暂不支持
        if v.type in PROJECTED and front is not None and "front" not in replaced:
            ops.append(NativeOp("creoson.drawing_create_proj_view", {
                "parent_view": view_name("front"), "point": _pt(v.origin_on_sheet, k), "drawing": "$drawing",
                "view": view_name(v.type), "sheet": 1}, "投影视图：方向由相对主视图的位置决定，比例继承主视图"))
        else:
            ops.append(NativeOp("creoson.drawing_create_gen_view", {
                "model_view": vm[v.type], "point": _pt(v.origin_on_sheet, k), "drawing": "$drawing",
                "view": view_name(v.type), "sheet": 1, "model": model,
                "scale": v.scale * (0.5 if v.type == "iso" else 1)}, f"{v.type} 一般视图"))
    if toolkit:
        ops += tk_detail_ops(plan)
        ops.append(NativeOp("creoson.drawing_list_views", {"drawing": "$drawing"}, "回读视图名供 check_drawing 核对"))
        ops.append(NativeOp("creoson.file_save", {"file_": "$drawing"}, "保存 .drw"))
        ops.append(NativeOp("creoson.interface_export_pdf", {"file_": "$drawing", "dirname": "$out_dir",
                                                             "filename": f"{plan.name or Path(model).stem}.pdf"}, "导出 PDF"))
        return ops
    fv = next((v for v in plan.views if v.type == "flat_pattern"), None)
    if fv is not None:
        ops.append(NativeOp("ext.cadmcp_flat_pattern_view", {
            "drawing": "$drawing", "point": _pt(fv.origin_on_sheet, k), "scale": fv.scale, "view": "V_FLAT"},
            "钣金展开图：CREOSON 原生没有，需扩展（展开状态）"))
    for s in plan.sections:
        ops.append(NativeOp("ext.cadmcp_section_view", {
            "drawing": "$drawing", "label": s.label, "kind": s.kind, "parent": view_name(s.parent),
            "replaces": view_name(s.view), "cut_points_model": [list(p) for p in s.cut_points],
            "path": [list(p) for p in s.path], "half_from": s.half_from,
            "center": _pt(next((v.origin_on_sheet for v in plan.views if v.type == s.view), (0, 0)), k)},
            "剖视图/局部剖：需扩展创建剖截面 + 视图"))
    for v in plan.views:
        if v.type == "section":  # 移出断面图
            ops.append(NativeOp("ext.cadmcp_section_view", {
                "drawing": "$drawing", "label": v.label, "kind": "removed", "target": v.target,
                "parent": view_name("front"), "center": _pt(v.origin_on_sheet, k)}, "移出断面图"))
    if any(v.type == "detail" for v in plan.views):
        ops.append(NativeOp("warn", {"message": "局部放大图只在 TOOLKIT 路径生成（tk.drw.detail_view），扩展路径未建"}))
    ops.append(NativeOp("creoson.drawing_regenerate", {"drawing": "$drawing"}, "再生后视图才有边界"))

    # 尺寸：CREOSON 不能逐个建驱动/草绘尺寸 → 扩展"显示模型尺寸"
    sizing = [d for d in plan.dims if d.kind in ("linear", "ordinate", "diameter", "radius", "callout")]
    if sizing:
        ops.append(NativeOp("warn", {"message": f"{len(sizing)} 个尺寸/孔标注无法用 creopyson 创建，已规划扩展 "
                                                "cadmcp_show_dims（显示模型尺寸）；扩展不可用时图纸将没有尺寸"}))
        ops.append(NativeOp("ext.cadmcp_show_dims", {"drawing": "$drawing", "dims": [
            {"view": view_name(d.view), "kind": d.kind, "axis": d.axis, "value": d.value, "text": d.text,
             "ref": d.ref, "at_model": d.at, "extra": d.extra} for d in sizing]}, "显示模型尺寸 + 注释引线"))
    gdt = [d for d in plan.dims if d.kind in ("datum", "gtol", "roughness", "weld", "tag", "origin")]
    if gdt:
        ops.append(NativeOp("warn", {"message": f"{len(gdt)} 个基准/公差/粗糙度/焊缝/孔号标注依赖扩展 cadmcp_gdt"}))
        ops.append(NativeOp("ext.cadmcp_gdt", {"drawing": "$drawing", "items": [
            {"view": view_name(d.view), "kind": d.kind, "text": d.text, "ref": d.ref, "at_model": d.at,
             "extra": d.extra} for d in gdt]}, "基准/几何公差/粗糙度/焊缝符号"))
    # 注释：技术要求、"其余"粗糙度
    texts = list(plan.notes)
    sf = plan.title_block.get("surface_finish")
    if sf and plan.model_kind == "part":
        texts.insert(0, "其余 ∅√（不去除材料）" if sf == "cast" else f"其余 √{sf} (√)")
    if texts:
        ops.append(NativeOp("ext.cadmcp_note", {"drawing": "$drawing", "text": "\n".join(texts),
                                                "anchor": "above_title_block"}, "技术要求 / 其余粗糙度"))
    if plan.revisions:
        ops.append(NativeOp("ext.cadmcp_note", {"drawing": "$drawing", "anchor": "left_of_title_block",
                                                "text": "更改记录：\n" + "\n".join(
                                                    f"{r.get('rev', '')}  {r.get('description', '')}  "
                                                    f"{r.get('date', '')}  {r.get('by', '')}" for r in plan.revisions)}))
    tables = []
    if plan.bom:
        tables.append(("bom", ["序号", "代号", "数量", "名称", "材料"], [
            [(d := bom_display(r))[0], d[1], d[3], d[2], d[4]] for r in plan.bom]))
    if plan.hole_table:
        tables.append(("hole_table", ["孔号", "X", "Y", "规格"],
                       [[r["tag"], f"{r['x']:g}", f"{r['y']:g}", r["size"]] for r in plan.hole_table]))
    if plan.cut_list:
        cols = ("item", "kind", "spec", "qty", "length_mm")
        tables.append(("cut_list", list(cols), [[r.get(c, "") for c in cols] for r in plan.cut_list]))
    for name, cols, rows in tables:
        ops.append(NativeOp("ext.cadmcp_table", {"drawing": "$drawing", "table": name, "columns": cols, "rows": rows,
                                                 "box": plan.hole_table_box if name == "hole_table" else None},
                            "Creo 表格（pfcTable）；creopyson 无表格 API"))
    if plan.balloons and plan.model_kind == "assembly":
        ops.append(NativeOp("ext.cadmcp_balloons", {"drawing": "$drawing", "balloons": plan.balloons}, "BOM 球标"))

    ops.append(NativeOp("creoson.drawing_list_views", {"drawing": "$drawing"}, "回读视图名供 check_drawing 核对"))
    ops.append(NativeOp("creoson.file_save", {"file_": "$drawing"}, "保存 .drw"))
    ops.append(NativeOp("creoson.interface_export_pdf", {"file_": "$drawing", "dirname": "$out_dir",
                                                         "filename": f"{plan.name or Path(model).stem}.pdf"}, "导出 PDF"))
    return ops


TITLE_W, TITLE_H = 180, 56  # 标题栏（与 render_svg 一致）
CUT_COLS = [("序号", 12), ("规格", 52), ("长度/尺寸", 36), ("数量", 14), ("备注", 66)]
HOLE_COLS = [("孔号", 14), ("X", 22), ("Y", 22), ("规格", 122)]


def _xy(p) -> list[float]:
    return [round(float(p[0]), 3), round(float(p[1]), 3)]


def _plain_number(s: str) -> bool:
    try:
        float(s)
        return True
    except ValueError:
        return False


def _cols(cols) -> list[dict[str, Any]]:
    return [{"title": n, "width": w} for n, w in cols]


def _rows(rows) -> list[list[str]]:
    return [["" if v is None else (f"{v:g}" if isinstance(v, float) else str(v)) for v in r] for r in rows]


def _gear_view_ops(plan, model: str, vm: dict, k: float, toolkit: bool) -> list[NativeOp]:
    """齿轮（GB/T 4459.2）：左视图 = 沿轴线（IR Z）的端面一般视图；主视图 = 轴线水平的一般视图（RIGHT，
    TODO(实机核对) 命名视图朝向），再由 drw.section 过轴线与键槽设为全剖。"""
    main, end = plan.views[0], plan.views[1]
    ops = [NativeOp("creoson.drawing_create_gen_view", {
               "model_view": vm.get("section", "TOP"), "point": _pt(end.origin_on_sheet, k), "drawing": "$drawing",
               "view": view_name("left"), "sheet": 1, "model": model, "scale": end.scale}, "端面视图（沿轴线）"),
           NativeOp("creoson.drawing_create_gen_view", {
               "model_view": vm.get("gear_axial", "RIGHT"), "point": _pt(main.origin_on_sheet, k), "drawing": "$drawing",
               "view": view_name("front"), "sheet": 1, "model": model, "scale": main.scale}, "主视图（轴线水平）")]
    if not toolkit:
        ops.append(NativeOp("warn", {"message": "齿轮图的全剖、尺寸与参数表需 TOOLKIT 路径"}))
        return ops
    g = plan.gear_table
    R = plan.views[1].size_mm[0] / plan.views[1].scale / 2
    ops.append(NativeOp("tk.drw.section", {
        "drawing": "$drawing", "parent": view_name("left"), "replaces": view_name("front"), "label": "A", "kind": "full",
        "cut_points": [[0.0, -R - 5, 0.0], [0.0, R + 5, 0.0]], "center": _xy(main.origin_on_sheet)},
        "过轴线（YZ 平面，键槽在 +Y）的全剖"))
    ops += tk_detail_ops(plan)
    ops.append(NativeOp("tk.drw.table", {"drawing": "$drawing", "origin": _xy((g["origin"][0], g["origin"][1] - g["size"][1])),
                                         "columns": _cols(g["columns"]), "rows": _rows([list(r) for r in g["rows"]]),
                                         "row_height": g["row_height"], "header_bottom": False},
                        "齿轮参数表（图框右上角，表头在上）"))
    ops.append(NativeOp("creoson.file_save", {"file_": "$drawing"}, "保存 .drw"))
    ops.append(NativeOp("creoson.interface_export_pdf", {"file_": "$drawing", "dirname": "$out_dir",
                                                         "filename": f"{plan.name or Path(model).stem}.pdf"}, "导出 PDF"))
    return ops


def tk_detail_ops(plan) -> list[NativeOp]:
    """DrawingPlan → 逐项 `tk.drw.*`。坐标全部是规划层已算好的图纸坐标（mm，左下角原点），与 SVG 预览一致：
    尺寸 sheet_pts/sheet_pos/orient，引线标注 extra["label"]，精度符号 sheet_at/sheet_pos，孔表 hole_table_box，
    修订表 revision_table_origin，球标 anchor/pos。缺坐标的项不发命令，汇总成一条 warn。"""
    from ...detailing import REV_COLUMNS, general_roughness_at, local_section_sheet, revision_rows, revision_table_origin
    ops: list[NativeOp] = []
    D = "$drawing"
    skipped: dict[str, int] = {}

    def skip(what: str) -> None:
        skipped[what] = skipped.get(what, 0) + 1

    sec_a = getattr(plan, "asm_section", None)
    if sec_a:  # 装配图主视图全剖：剖切面过主轴线（装配坐标 y = plane），轴/标准件不剖
        ops.append(NativeOp("tk.drw.section", {
            "drawing": D, "parent": view_name(sec_a["parent"]), "replaces": view_name(sec_a["view"]),
            "label": sec_a["label"] or "A", "kind": "full", "plane": {"normal": [0, 1, 0], "offset": sec_a["plane"]},
            "exclude_components": list(sec_a["exclude"])},
            "装配剖面（xsec）+ 排除零部件 TODO(实机核对)：组件剖面线方向/间隔、排除件"))
    # 剖视：先由 CREOSON 建好被替代的投影视图，这里设为 2D 剖视（首期 full/half；step/local 服务端报未实现）
    for s in plan.sections:
        if s.kind == "local":  # 在原视图上局部剖：边界 = 规划层波浪线区域（图纸 mm）
            loc = local_section_sheet(plan, s)
            if loc is None:
                skip("局部剖（缺区域几何）")
                continue
            ops.append(NativeOp("tk.drw.section", {
                "drawing": D, "parent": view_name(s.parent) if s.parent else "", "replaces": view_name(s.view),
                "label": s.label, "kind": "local", "cut_points": [list(p) for p in loc["cut_points"]],
                "boundary": [list(p) for p in loc["boundary"]]}, "局部剖：样条边界 + 剖截面"))
            continue
        ops.append(NativeOp("tk.drw.section", {
            "drawing": D, "parent": view_name(s.parent), "replaces": view_name(s.view), "label": s.label,
            "kind": s.kind, "cut_points": [list(p) for p in s.cut_points],
            "center": _xy(next((v.origin_on_sheet for v in plan.views if v.type == s.view), (0, 0)))},
            "剖截面 + 视图设为 2D 剖视"))
    for v in plan.views:  # 实心轴竖直横孔：主视图局部剖（剖截面过轴线，边界 = 孔周围矩形）
        for ls in v.local_sections:
            ops.append(NativeOp("tk.drw.section", {
                "drawing": D, "parent": "", "replaces": view_name(v.type), "label": "", "kind": "local",
                "cut_points": [[0.0, 0.0, 0.0], [0.0, 0.0, 1.0]], "boundary": [list(q) for q in ls["boundary"]]},
                f"{ls['name']} 局部剖；TODO(实机核对) 同一视图多处局部剖"))
    for v in plan.views:  # 空心轴/套类：主视图原位全剖（局部剖边界包住整个视图，剖截面过轴线）
        if v.full_section:
            ops.append(NativeOp("tk.drw.section", {
                "drawing": D, "parent": "", "replaces": view_name(v.type), "label": "", "kind": "local",
                "cut_points": v.full_section["cut_points"], "boundary": [list(q) for q in v.full_section["boundary"]]},
                "原位全剖：边界 = 视图外框；TODO(实机核对) cut_points 取轴线两点时剖截面是否过轴线且垂直视线"))
    for v in plan.views:
        if v.type == "section":
            ops.append(NativeOp("tk.drw.removed_section", {
                "drawing": D, "view": section_view_name(v.label), "label": v.label.split("-")[0],
                "z": shaft_section_z(plan, v), "parent": view_name(plan.views[0].type)},
                "垂直轴线的剖截面 + 只显示切面"))
        elif v.type == "detail" and v.detail and "circle" in v.detail:
            ops.append(NativeOp("tk.drw.detail_view", {
                "drawing": D, "parent": view_name(plan.views[0].type), "view": detail_view_name(v.label),
                "circle": list(v.detail["circle"]), "center": _xy(v.origin_on_sheet), "scale": v.scale},
                f"局部放大图 {v.label}：父视图中圆形边界 → ProDrawingViewDetailCreate"))
        elif v.type == "flat_pattern":
            skip("钣金展开图")
    for v in plan.views:  # 孔/圆柱中心线：显示零件基准轴（轴类主视图也要轴线）
        if v.type in ("iso", "flat_pattern", "section") or plan.model_kind != "part":
            continue
        ops.append(NativeOp("tk.drw.show_axes", {"drawing": D, "view": view_name(v.type)}, "显示基准轴（孔中心线）"))

    for d in plan.dims:
        ex = d.extra or {}
        vw = view_name(d.view)
        if d.view == "section":  # 断面图尺寸：按断面标签找视图；文字覆盖（槽宽 N9、d−t 须量到圆的远端）
            if not ex.get("section"):
                skip("断面图标注（未关联断面）")
                continue
            vw = section_view_name(ex["section"])
        elif d.view == "detail":
            vw = detail_view_name(ex.get("detail", ""))
        elif d.view == "flat_pattern":  # 展开图首期不建（见上），其标注一并跳过
            skip("展开图标注")
            continue
        if plan.layout == "gear" and d.kind in ("linear", "diameter") and not ex.get("pick_pts"):
            skip("分度线/分度圆尺寸（非模型几何）")
            continue
        if d.kind in ("linear", "diameter", "ordinate") and ex.get("sheet_pts"):
            p1, p2 = ex.get("pick_pts") or ex["sheet_pts"]  # pick_pts：原生选边点（与预览尺寸界线起点不同时）
            args = {"drawing": D, "view": vw, "kind": "ordinate" if d.kind == "ordinate" else "linear",
                    "p1": _xy(p1), "p2": _xy(p2), "at": _xy(dim_text_pos(ex)), "orient": ex.get("orient") or "aligned"}
            if ex.get("reference"):  # 参考尺寸（封闭链开口环）：保留实测值、加括号
                args["text"] = "(@D)"
            elif d.kind == "diameter" or d.view == "section" or not _plain_number(d.text):  # Φ20k6 等：文字覆盖；纯数值保留 Creo 实测值
                args["text"] = d.text
                from ...detailing import split_deviation
                sp = split_deviation(d.text)
                if sp:  # 极限偏差交给 Creo 原生公差显示（叠写），文字只留代号
                    args["text"] = sp[0] + sp[3]
                    args["tol"] = [float(sp[1]), float(sp[2])]
            ops.append(NativeOp("tk.drw.dim", args, "坐标尺寸：基准边 + 要素边" if d.kind == "ordinate"
                                else "两条轮廓边上的点 → 线性尺寸"))
        elif d.kind in ("callout", "radius", "diameter"):
            lb = ex.get("label")
            if not lb:
                skip(f"{d.kind} 标注（无引线布置）")
                continue
            ops.append(NativeOp("tk.drw.note", {"drawing": D, "view": vw, "text": d.text, "at": _xy(lb["text_at"]),
                                                "leader_to": _xy(lb["anchor"])}, "孔/圆角/倒角引线标注"))
        elif d.kind in ("linear", "ordinate"):
            skip("坐标/定位尺寸（规划层未给图纸坐标）")
        elif d.kind == "chain":
            ops.append(NativeOp("tk.drw.line", {"drawing": D, "view": vw, "points": [_xy(q) for q in ex["sheet_pts"]],
                                                "font": "CTRLFONT", "width": 0.5}, "局部热处理部位：粗点画线"))
        elif d.kind in ("tag", "origin"):
            if "sheet_at" not in ex:
                skip("孔号")
                continue
            ops.append(NativeOp("tk.drw.note", {"drawing": D, "text": d.text, "at": _xy(ex["sheet_at"]),
                                                "height": 2.5}, "孔号 / 坐标原点"))
        elif d.kind in ("datum", "gtol", "roughness", "weld"):
            if "sheet_at" not in ex or "sheet_pos" not in ex:
                skip(f"{d.kind} 符号（无图纸坐标）")
                continue
            at, pos = _xy(ex["sheet_at"]), _xy(gtol_frame_pos(ex) if d.kind == "gtol" else ex["sheet_pos"])
            if d.kind == "datum":
                ops.append(NativeOp("tk.drw.datum", {"drawing": D, "view": vw, "at": at, "pos": pos, "label": d.text}))
            elif d.kind == "gtol":
                sym, zone, *datums = ex.get("cells", ["", "", ""])
                try:
                    value = float(str(zone).lstrip("Φ"))
                except ValueError:
                    skip("几何公差（公差值无法解析）")
                    continue
                ops.append(NativeOp("tk.drw.gtol", {
                    "drawing": D, "view": vw, "at": None if ex.get("attach") == "callout" else at, "pos": pos,
                    "type": d.ref.split(":")[1].split("@")[0], "value": value, "diameter": str(zone).startswith("Φ"),
                    "datums": [x for x in datums[:3] if x]}))
            elif d.kind == "roughness":
                if ex.get("rot"):  # 标在尺寸线上（转 90°，无被选边线，自由放置）
                    ops.append(NativeOp("tk.drw.surface_finish", {"drawing": D, "view": vw, "at": None, "pos": pos,
                                                                  "ra": d.value, "removal": "required",
                                                                  "angle": float(ex["rot"])}))
                else:
                    ops.append(NativeOp("tk.drw.surface_finish", {"drawing": D, "view": vw, "at": at, "pos": pos,
                                                                  "ra": d.value, "removal": "required"}))
            else:  # 焊缝：TOOLKIT 无通用焊缝符号接口，用带引线注释
                text = (f"{d.text}{'/' + d.text if ex.get('both_sides') else ''} ◣{' ○' if ex.get('all_around') else ''}"
                        f"  {ex.get('count', 1)}处  {ex.get('process', '')}").strip()
                ops.append(NativeOp("tk.drw.note", {"drawing": D, "view": vw, "text": text, "at": pos, "leader_to": at},
                                    "焊缝：带引线注释"))

    W, _ = plan.sheet_size
    x0, y0 = W - 5 - TITLE_W, 5.0
    if plan.hole_table and plan.hole_table_box:
        bx0, by0, *_ = plan.hole_table_box
        ops.append(NativeOp("tk.drw.table", {
            "drawing": D, "origin": _xy((bx0, by0)), "columns": _cols(HOLE_COLS), "row_height": 5,
            "rows": _rows([[r["tag"], r["x"], r["y"], r["size"]] for r in plan.hole_table]), "header_bottom": False},
            "孔表（位置同 SVG 预览）"))
    if plan.gear_table and plan.layout != "gear":  # 齿轮轴参数表（齿轮零件图的表在 _gear_view_ops 里）
        g = plan.gear_table
        ops.append(NativeOp("tk.drw.table", {"drawing": D, "origin": _xy((g["origin"][0], g["origin"][1] - g["size"][1])),
                                             "columns": _cols(g["columns"]), "rows": _rows([list(r) for r in g["rows"]]),
                                             "row_height": g["row_height"], "header_bottom": False}, "齿轮参数表"))
    texts = list(plan.notes)
    if texts:  # 技术要求：首行在上，文字块左下角
        from ...detailing import notes_x
        ops.append(NativeOp("tk.drw.note", {"drawing": D, "text": "\n".join(texts),
                                            "at": [notes_x(plan), y0 + TITLE_H + 11]}, "技术要求（避开视图时右移，同 SVG）"))
    above = y0 + TITLE_H + 12 + 5 * len(texts)  # 标题栏上方表格的底边（同 render_svg）
    sf = plan.title_block.get("surface_finish")
    if sf and plan.model_kind == "part":
        gx, gy = general_roughness_at(plan)
        ops.append(NativeOp("tk.drw.note", {"drawing": D, "at": [gx, gy + 1],
                                            "text": "其余 ∅√（不去除材料）" if sf == "cast" else f"其余 √{sf} (√)"},
                            "其余表面粗糙度"))
    if plan.cut_list:
        rows = []
        for r in sorted(plan.cut_list, key=lambda r: r["item"]):
            size = f"L={r['length_mm']:g}" if r.get("kind") == "member" else "×".join(f"{v:g}" for v in r.get("size_mm", []))
            note = "两端平切 90°" if r.get("kind") == "member" else ("焊后加工" if r.get("machined") else "")
            rows.append([r["item"], r.get("spec", ""), size, r.get("qty", ""), note])
        ops.append(NativeOp("tk.drw.table", {"drawing": D, "origin": [x0, above], "columns": _cols(CUT_COLS),
                                             "rows": _rows(rows), "row_height": 7, "header_bottom": True},
                            "切割清单（GB/T 10609.2 表头在下）"))
    for ax in getattr(plan, "axes", []):  # 装配图回转件轴线：细点画线
        ops.append(NativeOp("tk.drw.line", {"drawing": D, "points": [_xy(q) for q in ax["pts"]],
                                            "font": "CTRLFONT", "width": 0.25}, "回转件轴线（细点画线）"))
    if plan.bom:
        rows = [bom_display(r) for r in sorted(plan.bom, key=lambda r: r["item"])]
        ops.append(NativeOp("tk.drw.table", {"drawing": D, "origin": [x0, above], "columns": _cols(BOM_COLS),
                                             "rows": _rows(rows), "row_height": 7, "header_bottom": True},
                            "明细表（GB/T 10609.2 表头在下，序号自下而上）"))
    if plan.revisions:
        rows = [[r.get("rev", ""), r.get("description", ""), r.get("date", ""), r.get("by", "")]
                for r in revision_rows(plan)][::-1]  # SVG：最新在最下行、表头在上
        ops.append(NativeOp("tk.drw.table", {"drawing": D, "origin": _xy(revision_table_origin(plan)),
                                             "columns": _cols(REV_COLUMNS), "rows": _rows(rows), "row_height": 7,
                                             "header_bottom": False}, "更改记录（标题栏左侧）"))
    if plan.balloons and plan.model_kind == "assembly":
        main = view_name(plan.views[0].type)
        for b in plan.balloons:
            if "pos" not in b:
                skip("球标")
                continue
            ops.append(NativeOp("tk.drw.balloon", {"drawing": D, "view": main, "component": [b["instance"]],
                                                   "item": str(b["item"]), "at": _xy(b["anchor"]), "pos": _xy(b["pos"])}))
    if skipped:
        ops.append(NativeOp("warn", {"message": "TOOLKIT 出图未生成：" + "；".join(f"{k} ×{n}" for k, n in skipped.items())}))
    ops.append(NativeOp("tk.drw.readback", {"drawing": D}, "回读尺寸/注释/表格/符号数"))
    return ops


class CreoDrawingExecutor:
    """逐 op 调 creopyson Client；oplog 格式与 SW 执行器一致 {i, op, args, ok, error}。"""

    def __init__(self, client: Any, tk: Any = None):
        self.c = client
        self.tk = tk  # ToolkitClient；tk.* op 需要
        self.log: list[dict[str, Any]] = []
        self.warnings: list[str] = []
        self.readback: dict[str, Any] = {}
        self.drawing: str | None = None
        self.out_dir = Path(".")

    def _sub(self, v: Any) -> Any:
        if v == "$drawing":
            return self.drawing
        if v == "$out_dir":
            return str(self.out_dir)
        if isinstance(v, dict):
            return {k: self._sub(x) for k, x in v.items()}
        return v

    def _drawing_name(self) -> str:
        d = self.drawing or ""
        return d[:-4] if d.lower().endswith(".drw") else d

    def _call(self, op: NativeOp) -> Any:
        args = {k: self._sub(v) for k, v in op.args.items()}
        if op.op == "warn":
            self.warnings.append(args["message"])
            return None
        kind, _, fn = op.op.partition(".")
        if kind == "creoson":
            if fn == "drawing_create":
                if not args.get("drawing"):
                    args.pop("drawing", None)  # 不指定则由 Creo 按模型名命名
            elif "drawing" in args:
                args["drawing"] = args["drawing"] or self.drawing
            return getattr(self.c, fn)(**args)
        if kind == "tk":  # cadmcp_tk（TOOLKIT 异步程序）
            if self.tk is None:
                raise RuntimeError("cadmcp_tk 未配置（config.yaml 中 creo 目标加 toolkit: {host, port}）")
            if args.get("drawing") is None or args.get("drawing") == self.drawing:
                args["drawing"] = self._drawing_name()
            return self.tk.call(fn, **args)
        if kind == "ext":  # CREOSON 自定义命令：command=cadmcp, function=<name>
            # creopyson Client._creoson_post(command, function, data=None, key_data=None)（0.7.8 源码核对）
            return self.c._creoson_post("cadmcp", fn.removeprefix("cadmcp_"), args)
        raise RuntimeError(f"unknown op {op.op!r}")

    def run(self, ops: list[NativeOp], out_dir: Path, name: str) -> dict[str, Any]:
        self.out_dir = out_dir
        status = "ok"
        for i, op in enumerate(ops):
            t0 = time.perf_counter()
            rec: dict[str, Any] = {"i": i, "op": op.op, "args": op.args}
            try:
                res = self._call(op)
                rec.update(ok=True, result=res)
                if op.op == "creoson.drawing_create":
                    self.drawing = res if isinstance(res, str) and res else (
                        op.args.get("drawing") or Path(op.args["model"]).stem)
                elif op.op == "tk.drw.readback" and isinstance(res, dict):
                    self.readback.update({k: res[k] for k in ("dims", "notes", "symbols", "tables") if k in res})
                elif op.op == "creoson.drawing_list_views":
                    self.readback["views"] = [v if isinstance(v, str) else str(v.get("view", v)) for v in (res or [])]
            except Exception as e:  # noqa: BLE001
                rec.update(ok=False, error=f"{type(e).__name__}: {e}", trace=traceback.format_exc(limit=3))
                if op.op.startswith("ext."):
                    self.warnings.append(f"{op.op} 失败（CREOSON 扩展 cadmcp 未部署？）：{e}")
                elif op.op.startswith("tk."):
                    self.warnings.append(f"{op.op} 失败：{e}")
                critical = op.op.startswith("creoson.") and op.op.partition(".")[2] not in NON_CRITICAL
                if critical:
                    status = "errors"
                    rec["ms"] = round((time.perf_counter() - t0) * 1000)
                    self.log.append(rec)
                    break
                status = "warnings" if status == "ok" else status
            rec["ms"] = round((time.perf_counter() - t0) * 1000)
            self.log.append(rec)
        if status == "ok" and self.warnings:
            status = "warnings"
        (out_dir / f"{name}.oplog.json").write_text(json.dumps(self.log, ensure_ascii=False, indent=1, default=str),
                                                     encoding="utf-8")
        return {"status": status, "log": self.log}
