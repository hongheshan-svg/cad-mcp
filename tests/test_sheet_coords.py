"""规划层（CAD 无关）给断面图、展开图、轴类引出标注算的图纸坐标：与 SVG 预览一致，供 SW / Creo 原生标注选边。"""
from test_core import SHAFT

from cadmcp.adapters.creo.drawing import tk_detail_ops
from cadmcp.detailing import plan_part_drawing
from cadmcp.ir import DrawingSpec, PartSpec
from cadmcp.ir.resolve import resolve_part

SM = {"name": "SMB", "part_no": "SM-01", "title": "钣金支架", "material": "SPCC", "category": "sheet_metal",
      "base": {"type": "sheet_metal", "profile": {"shape": "rect", "width": 120, "height": 80}, "thickness": 2,
               "flanges": [{"edge": "front", "length": 40}, {"edge": "back", "length": 40}]}}


def _plan(spec, **kw):
    r = resolve_part(PartSpec.model_validate(spec))
    return plan_part_drawing(DrawingSpec.model_validate({"model": r.name, **kw}), r), r


def test_shaft_section_dims_inside_section_view():
    plan, r = _plan(SHAFT)
    secs = {v.label: v for v in plan.views if v.type == "section"}
    dims = [d for d in plan.dims if d.view == "section"]
    assert len(dims) == 4
    for d in dims:
        ex = d.extra
        v = secs[ex["section"]]
        f = next(k for k in r.shaft_features if k["name"] == v.target)
        (vx, vy), R = v.origin_on_sheet, f["d"] / 2 * v.scale
        for x, y in ex["sheet_pts"]:  # 被标注点都在断面圆内（含圆周）
            assert (x - vx) ** 2 + (y - vy) ** 2 <= (R + 0.02) ** 2
        (x1, y1), (x2, y2) = ex["sheet_pts"]
        if d.ref.endswith("_w"):
            assert ex["orient"] == "h" and abs((x2 - x1) - f["width"] * v.scale) < 0.02
        else:
            assert ex["orient"] == "v" and ex["arc_end"] == ("max", None)
            assert abs((y2 - y1) - (f["d"] - f["depth"]) * v.scale) < 0.02  # 圆周最低点到槽底 = d − t


def test_shaft_callouts_have_labels():
    plan, _ = _plan(SHAFT)
    for d in plan.dims:
        if d.kind == "callout":
            lb = d.extra["label"]
            assert lb["anchor"] != lb["text_at"]


def test_flat_pattern_dims_on_outline_and_clear_of_bends():
    plan, r = _plan(SM, views=[{"type": "front"}, {"type": "top"}, {"type": "flat_pattern"}])
    fv = next(v for v in plan.views if v.type == "flat_pattern")
    fu0, fu1, fv0, fv1 = r.flat_box
    cx, cy = fv.origin_on_sheet
    x0, x1 = cx - (fu1 - fu0) / 2 * fv.scale, cx + (fu1 - fu0) / 2 * fv.scale
    y0, y1 = cy - (fv1 - fv0) / 2 * fv.scale, cy + (fv1 - fv0) / 2 * fv.scale
    w = next(d for d in plan.dims if d.ref == "flat_w").extra
    h = next(d for d in plan.dims if d.ref == "flat_h").extra
    assert [round(p[0], 1) for p in w["sheet_pts"]] == [round(x0, 1), round(x1, 1)] and w["orient"] == "h"
    assert [round(p[1], 1) for p in h["sheet_pts"]] == [round(y0, 1), round(y1, 1)] and h["orient"] == "v"
    bend_y = [cy + (b["flat_pos"] - (fv0 + fv1) / 2) * fv.scale for b in r.bends if b["flat_axis"] == "v"]
    assert all(abs(w["sheet_pts"][0][1] - y) > 1 for y in bend_y)  # 选边点避开折弯线
    assert all("sheet_pos" in d.extra for d in plan.dims if d.ref.startswith("bend"))


def test_creo_tk_skips_unbuilt_views():
    plan, _ = _plan(SHAFT)
    ops = tk_detail_ops(plan)
    assert not [o for o in ops if o.op == "tk.drw.dim" and o.args["view"] == "V_SECTION"]
    notes = [o for o in ops if o.op == "tk.drw.note" and "leader_to" in o.args]
    assert len(notes) >= 2  # 倒角、挡圈槽引出标注现在有规划位置
