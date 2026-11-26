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


def compile_drawing(plan, model_file: str, templates: dict[str, str] | None = None, sheet_unit_scale: float = 1.0,
                    view_map: dict[str, str] | None = None) -> list[NativeOp]:
    """DrawingPlan → 操作计划。op 名 `creoson.<creopyson 方法>` / `ext.<扩展命令>` / `warn`（仅提示）。
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

    front = next((v for v in plan.views if v.type == "front"), None)
    replaced = {s.view for s in plan.sections if s.kind != "local"}  # 由剖视图替代的视图（经扩展生成）
    for v in plan.views:
        if v.type in ("section", "flat_pattern") or v.type in replaced:
            continue
        if v.type in PROJECTED and front is not None and "front" not in replaced:
            ops.append(NativeOp("creoson.drawing_create_proj_view", {
                "parent_view": view_name("front"), "point": _pt(v.origin_on_sheet, k), "drawing": "$drawing",
                "view": view_name(v.type), "sheet": 1}, "投影视图：方向由相对主视图的位置决定，比例继承主视图"))
        else:
            ops.append(NativeOp("creoson.drawing_create_gen_view", {
                "model_view": vm[v.type], "point": _pt(v.origin_on_sheet, k), "drawing": "$drawing",
                "view": view_name(v.type), "sheet": 1, "model": model,
                "scale": v.scale * (0.5 if v.type == "iso" else 1)}, f"{v.type} 一般视图"))
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
            [r["item"], r["part_no"], r["qty"], r["title"], r.get("material") or r.get("spec", "")] for r in plan.bom]))
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


class CreoDrawingExecutor:
    """逐 op 调 creopyson Client；oplog 格式与 SW 执行器一致 {i, op, args, ok, error}。"""

    def __init__(self, client: Any):
        self.c = client
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
                elif op.op == "creoson.drawing_list_views":
                    self.readback["views"] = [v if isinstance(v, str) else str(v.get("view", v)) for v in (res or [])]
            except Exception as e:  # noqa: BLE001
                rec.update(ok=False, error=f"{type(e).__name__}: {e}", trace=traceback.format_exc(limit=3))
                if op.op.startswith("ext."):
                    self.warnings.append(f"{op.op} 失败（CREOSON 扩展 cadmcp 未部署？）：{e}")
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
