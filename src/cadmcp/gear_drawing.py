"""直齿圆柱齿轮零件图规划（GB/T 4459.2 规定画法、GB/T 1095 轮毂键槽、参数表）。CAD 无关。

布局：主视图轴线水平、过轴线全剖（剖切面过键槽，轮齿按不剖处理：齿顶线、齿根线粗实线，分度线细点画线），
左视图（第一角放在主视图右侧）画齿顶圆粗实线、分度圆细点画线、齿根圆细实线，以及轴孔与键槽。
参数表放在图框右上角。尺寸的图纸坐标（sheet_pts / sheet_pos / orient）与 SVG 预览、CAD 原生标注共用。
"""
from __future__ import annotations

from datetime import date
from typing import Any

from .detailing import (GAP, MARGIN, SHEETS, DrawingPlan, Dim, ViewPlan, _choose, _notes_and_title, text_width)
from .ir.drawing import DrawingSpec
from .ir.resolve import ResolvedPart

TABLE_COLS = (("名称", 30.0), ("代号", 14.0), ("数值", 36.0))
TABLE_ROW_H = 7.0


def gear_parts(p: ResolvedPart) -> dict[str, Any]:
    """识别轴孔（中心孔）与轮毂键槽（贯通、矩形、贴着轴孔的型腔）。"""
    bore = None
    for h in p.holes:
        if any(abs(i.xyz[0]) < 1e-6 and abs(i.xyz[1]) < 1e-6 for i in h.instances):
            if bore is None or h.max_dia > bore.max_dia:
                bore = h
    kw = None
    if bore is not None:
        rb = bore.max_dia / 2
        for pk in p.pockets:
            if not pk.get("through") or pk["profile"].shape != "rect":
                continue
            u0, u1, v0, v1 = pk["profile"].bbox2d
            if u0 < 0 < u1 and v0 < rb < v1:  # 跨在 +Y 方向的孔壁上
                kw = {"name": pk["name"], "width": u1 - u0, "top": v1}
    others = [h for h in p.holes if h is not bore]
    return {"bore": bore, "keyway": kw, "others": others}


def hub_t2_tol(width: float) -> float:
    """GB/T 1095 轮毂槽深 t2 的上偏差（下偏差 0）：b ≤ 6 为 +0.1，否则 +0.2。"""
    return 0.1 if width <= 6 else 0.2


def plan_gear_drawing(spec: DrawingSpec, p: ResolvedPart, today: date | None = None) -> DrawingPlan:
    from .standards import fit_text, tol_text
    g = p.gear
    b = p.bbox.zmax - p.bbox.zmin
    da, d, df = g["da"], g["d"], g["df"]
    parts = gear_parts(p)
    bore, kw = parts["bore"], parts["keyway"]
    min_feat = min((h.drill for h in p.holes), default=0.0)
    # 主视图宽 b（左侧留直径尺寸 3 道），左视图 da
    sheet, scale = _choose(p.bbox, [b + 40 / 1, da], [da], spec.sheet, min_feat, "part")
    W, H = SHEETS[sheet]
    sc = scale
    x0 = MARGIN + GAP + 20  # 左侧留给 Φda、Φd 两道竖直尺寸
    cy = H - MARGIN - GAP / 2 - da * sc / 2 - 12
    main = ViewPlan("front", sc, (b * sc, da * sc), (x0 + b * sc / 2, cy))
    ex = x0 + b * sc + GAP + 10 + da * sc / 2
    end = ViewPlan("left", sc, (da * sc, da * sc), (ex, cy))
    views = [main, end]

    xl, xr = x0, x0 + b * sc
    dims: list[Dim] = []

    def dev(text_full: str) -> str:
        return text_full if spec.fit_deviations else text_full.split("(")[0]

    xm = (xl + xr) / 2

    def vdim(ref, value, text, y_lo, y_hi, x_line, pick=True):
        """主视图左侧竖直直径尺寸：尺寸界线从视图左端引出，尺寸线在 x_line；
        pick_pts = 原生标注选边点（齿顶线中点；分度线不是模型几何 → 无）。"""
        ex_ = {"sheet_pts": [(round(xl, 2), round(y_lo, 2)), (round(xl, 2), round(y_hi, 2))],
               "sheet_pos": (round(x_line, 2), round((y_lo + y_hi) / 2, 2)), "orient": "v"}
        if pick:
            ex_["pick_pts"] = [(round(xm, 2), round(y_lo, 2)), (round(xm, 2), round(y_hi, 2))]
        dims.append(Dim("front", "diameter", None, value, text, ref, None, ex_))

    if spec.dimensions != "none":
        vdim("gear_da", da, dev(fit_text(da, g["tip_tolerance"])), cy - da / 2 * sc, cy + da / 2 * sc, xl - 20)
        vdim("gear_d", d, f"Φ{d:g}", cy - d / 2 * sc, cy + d / 2 * sc, xl - 10, pick=False)
        yb_ = cy - da / 2 * sc
        dims.append(Dim("front", "linear", "z", b, f"{b:g}", "gear_b", None, {
            "sheet_pts": [(round(xl, 2), round(yb_, 2)), (round(xr, 2), round(yb_, 2))],
            "pick_pts": [(round(xl, 2), round(cy - (da + df) / 4 * sc, 2)), (round(xr, 2), round(cy - (da + df) / 4 * sc, 2))],
            "sheet_pos": (round(xm, 2), round(yb_ - 12, 2)), "orient": "h"}))
        if bore is not None:  # 轴孔：剖切面过键槽，主视图无孔径上边界 → 左视图引线标注（箭头指在孔壁上）
            rb = bore.max_dia / 2
            k = 0.7071
            anchor = (ex - rb * sc * k, cy - rb * sc * k)
            rr = da / 2 * sc + 6
            elbow = (ex - rr * k, cy - rr * k)
            text = dev(fit_text(2 * rb, bore.tolerance)) if bore.tolerance else f"Φ{2 * rb:g}"
            tw = text_width(text, 3.5)
            dims.append(Dim("left", "callout", None, 2 * rb, text, "gear_bore", None, {"label": {
                "anchor": (round(anchor[0], 2), round(anchor[1], 2)), "elbow": (round(elbow[0], 2), round(elbow[1], 2)),
                "end": (round(elbow[0] - tw - 1, 2), round(elbow[1], 2)),
                "text_at": (round(elbow[0] - tw - 0.5, 2), round(elbow[1] + 0.8, 2)), "on_edge": True, "dir": -1}}))
        if kw is not None and bore is not None:
            rb = bore.max_dia / 2
            w = kw["width"]
            top = kw["top"]
            yk = cy + top * sc
            tw_ = dev(tol_text(w, "JS9"))
            tx = ex - w / 2 * sc - 1.5 - text_width(tw_, 3.5) / 2  # 文字放在左尺寸界线之外，不压中心线与界线
            yw = (yk + cy + (rb * rb - w * w / 4) ** 0.5 * sc) / 2  # 键槽侧壁中点
            dims.append(Dim("left", "linear", "x", w, tw_, f"{kw['name']}_w", None, {
                "sheet_pts": [(round(ex - w / 2 * sc, 2), round(yk, 2)), (round(ex + w / 2 * sc, 2), round(yk, 2))],
                "pick_pts": [(round(ex - w / 2 * sc, 2), round(yw, 2)), (round(ex + w / 2 * sc, 2), round(yw, 2))],
                "sheet_pos": (round(tx, 2), round(yk + 8, 2)), "orient": "h"}))
            t = hub_t2_tol(w)
            tt = dev(f"{top + rb:g}(+{t:g}/0)")
            half = text_width(tt, 3.5) / 2
            ty = max(cy + (top - rb) / 2 * sc, cy + half + 1.5)  # 文字不压水平中心线
            if ty + half > yk - 1:  # 也不压槽底的尺寸界线：放不下就移到尺寸界线之外（上方），尺寸线延长
                ty = yk + 1.5 + half
            dims.append(Dim("left", "linear", "y", top + rb, tt, f"{kw['name']}_t", None, {
                "sheet_pts": [(round(ex, 2), round(cy - rb * sc, 2)), (round(ex + w / 2 * sc, 2), round(yk, 2))],
                "pick_pts": [(round(ex, 2), round(cy - rb * sc, 2)), (round(ex, 2), round(yk, 2))],  # 孔底象限点、槽底中点
                "sheet_pos": (round(ex + rb * sc + 8, 2), round(ty, 2)), "orient": "v"}))
        for h in parts["others"]:
            dims.append(Dim("left", "callout", None, None, h.callout, h.name, None, {}))
    # 表面结构：齿面 Ra 注在分度线上（GB/T 4459.2），轴孔 Ra 注在孔壁（剖视）
    dims.append(Dim("front", "roughness", None, g["flank_ra"], f"Ra {g['flank_ra']:g}", "ra:flank", None, {
        "normal": (0, 1), "sheet_at": (round(xr + 2.5, 2), round(cy + d / 2 * sc, 2)),
        "sheet_pos": (round(xr + 2.5, 2), round(cy + d / 2 * sc, 2))}))  # 分度线右端延长线上
    if bore is not None and bore.tolerance:
        digits = "".join(ch for ch in bore.tolerance if ch.isdigit())
        ra = 0.8 if digits and int(digits) <= 6 else 1.6
        rb = bore.max_dia / 2
        dims.append(Dim("front", "roughness", None, ra, f"Ra {ra:g}", f"ra:hole:{bore.name}", None, {
            "normal": (0, 1), "sheet_at": (round(xl + min(3.0, b * sc * 0.15), 2), round(cy - rb * sc, 2)),
            "sheet_pos": (round(xl + min(3.0, b * sc * 0.15), 2), round(cy - rb * sc, 2)),
            "in_section": True}))  # 站在下半孔壁上（孔内空白处）

    tr = g.get("blank_runout")
    if tr and bore is not None and spec.dimensions != "none":
        # 基准 A：轴孔（左视图孔壁左上 150°，沿径向引出到腹板空白处；避开孔径引线与键槽尺寸）
        import math
        rb = bore.max_dia / 2
        nx, ny = math.cos(math.radians(150)), math.sin(math.radians(150))
        at = (ex + rb * sc * nx, cy + rb * sc * ny)
        dims.append(Dim("left", "datum", None, None, "A", "datum:A", None, {
            "normal": (round(nx, 4), round(ny, 4)), "sheet_at": (round(at[0], 2), round(at[1], 2)),
            "sheet_pos": (round(at[0] + 10 * nx, 2), round(at[1] + 10 * ny, 2))}))
        cells = ["↗", f"{tr:g}", "A"]
        # 齿顶圆径向圆跳动：箭头垂直指向主视图上齿顶线
        tip = (xl + b * sc * 0.3, cy + da / 2 * sc)
        dims.append(Dim("front", "gtol", None, tr, " | ".join(cells), "gtol:circular_runout@tip", None, {
            "normal": (0, 1), "cells": cells, "reach": 10.0, "sheet_at": (round(tip[0], 2), round(tip[1], 2)),
            "sheet_pos": (round(tip[0], 2), round(tip[1] + 10, 2))}))
        # 基准端面圆跳动：箭头水平指向右端面（剖面区，键槽之上）
        y_face = cy + ((kw["top"] if kw else rb) + df / 2) / 2 * sc
        dims.append(Dim("front", "gtol", None, tr, " | ".join(cells), "gtol:circular_runout@face", None, {
            "normal": (1, 0), "cells": cells, "reach": 8.0, "sheet_at": (round(xr, 2), round(y_face, 2)),
            "sheet_pos": (round(xr + 8, 2), round(y_face, 2))}))
    notes, tb = _notes_and_title(spec, p, sheet, scale, today)
    plan = DrawingPlan(p.name, "part", sheet, SHEETS[sheet], scale, spec.projection, views, dims, notes, tb,
                       layout="gear", revisions=list(spec.revisions))
    plan.gear_table = param_table(plan, p)
    return plan


def param_table_rows(plan: DrawingPlan) -> dict[str, Any]:
    """参数表（已在规划时存入 plan.gear_table）。"""
    return plan.gear_table


def param_table(plan: DrawingPlan, p: ResolvedPart) -> dict[str, Any]:
    """参数表：图框右上角，表头在上。返回列宽、行、左上角坐标（图纸 mm）。"""
    from .gear import param_rows
    W, H = plan.sheet_size
    rows = param_rows(p.gear, p.gear["accuracy"], p.gear.get("mate"))
    width = sum(w for _, w in TABLE_COLS)
    return {"origin": (W - MARGIN - width, H - MARGIN), "columns": list(TABLE_COLS), "rows": rows,
            "row_height": TABLE_ROW_H, "size": (width, TABLE_ROW_H * (len(rows) + 1))}
