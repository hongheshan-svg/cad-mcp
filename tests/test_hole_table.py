"""孔表（孔多时代替逐孔坐标）与引出标注排布。"""
import pytest
from motor_plate import MP

from cadmcp.adapters.solidworks.compile import compile_drawing
from cadmcp.detailing import check_drawing
from cadmcp.ir import DrawingSpec, PartSpec

GRID = {"name": "GP-001_GridPlate", "part_no": "GP-001", "title": "孔板", "material": "Q235",
        "base": {"type": "extrude", "profile": {"shape": "rect", "width": 300, "height": 200}, "thickness": 15},
        "features": [
            {"feature": "hole", "name": "M6", "type": "tapped", "size": "M6", "depth": 12, "thread_depth": 10,
             "pattern": {"kind": "grid", "nx": 5, "ny": 3, "dx": 50, "dy": 50, "origin": [-100, -50]}},
            {"feature": "hole", "name": "Pin", "type": "simple", "size": 8, "tolerance": "H7",
             "positions": [[-135, -85], [135, 85]]},
            {"feature": "hole", "name": "Fix", "type": "counterbore", "size": "M8",
             "positions": [[-135, 85], [135, -85], [0, 85], [0, -85]]}]}


def _grid(svc, **kw):
    svc.change_begin("G")
    svc.build(PartSpec.model_validate(GRID))
    d = svc.build(DrawingSpec.model_validate({"model": GRID["name"], "title_block": {"designer": "AI"}, **kw}))
    return d, svc._get(GRID["name"] + "_DRW").resolved


def test_auto_hole_table_replaces_ordinates(svc):
    d, plan = _grid(svc)
    assert len(plan.hole_table) == 21 and plan.hole_table_view == "top"
    assert {r["tag"] for r in plan.hole_table} >= {"A1", "A15", "B1", "B2", "C4"}
    a1 = next(r for r in plan.hole_table if r["tag"] == "A1")
    assert (a1["x"], a1["y"]) == (50.0, 50.0) and a1["size"].startswith("M6-6H")   # 相对左下角
    assert not [x for x in plan.dims if x.view == "top" and x.kind == "ordinate"]
    assert not [x for x in plan.dims if x.view == "top" and x.kind == "callout" and x.ref in ("M6", "Pin", "Fix")]
    assert sum(1 for x in plan.dims if x.kind == "tag") == 21
    assert not [f for f in d["rules"]["findings"] if f["severity"] != "info"], d["rules"]["findings"]   # 放得下（让出轴测图），渲染回读一致（D017）
    assert not any(v.type == "iso" for v in plan.views)
    ops = compile_drawing(plan, "x.SLDPRT")
    ht = next(o for o in ops if o.op == "hole_table")
    assert len(ht.args["rows"]) == 21 and ht.args["rows"][0][:3] == ["A1", "50", "50"]
    tags = next(o for o in ops if o.op == "hole_tags")
    assert len(tags.args["tags"]) == 22                                         # 21 孔号 + 原点
    assert not any(o.op == "auto_dimension" and o.args["view"] == "top" for o in ops)


def test_hole_table_off_and_mismatch(svc):
    _, plan = _grid(svc, hole_table=False)
    assert not plan.hole_table and any(x.kind == "ordinate" for x in plan.dims)
    _, plan = _grid(svc)
    rows = [[r["tag"], r["x"], r["y"]] for r in plan.hole_table]
    rows[0][1] = 51
    f = check_drawing(plan, {"hole_table_rows": rows, "dim_count": 1, "notes": []})
    assert any(x["rule"] == "D017" for x in f)


def test_few_holes_no_table_and_labels_do_not_collide(svc):
    svc.change_begin("MP")
    svc.build(PartSpec.model_validate(MP))
    d = svc.build(DrawingSpec.model_validate({"model": MP["name"], "title_block": {"designer": "AI"}}))
    plan = svc._get(MP["name"] + "_DRW").resolved
    assert not plan.hole_table
    assert not [f for f in d["rules"]["findings"] if f["rule"] == "D009"]
    labels = [x for x in plan.dims if x.extra and "label" in x.extra]
    assert {x.text for x in labels} >= {"4×R5", "40×20 深4"}
    boxes = []
    for x in labels:
        lb = x.extra["label"]
        x0, x1 = sorted((lb["elbow"][0], lb["end"][0]))
        boxes.append((x0, lb["elbow"][1], x1, lb["elbow"][1] + 3))
    for i, a in enumerate(boxes):
        for b in boxes[i + 1:]:
            assert min(a[2], b[2]) <= max(a[0], b[0]) or min(a[3], b[3]) <= max(a[1], b[1])



def test_general_roughness_and_view_pruning_by_review():
    """肉眼审图发现的问题：其余粗糙度压断面图、薄板多余左视图（D021 info）。"""
    from test_core import SHAFT

    from cadmcp.detailing import _box_hit, general_roughness_at, plan_part_drawing
    from cadmcp.ir.resolve import resolve_part
    p = resolve_part(PartSpec.model_validate(SHAFT))
    plan = plan_part_drawing(DrawingSpec(model=p.name), p)
    x, y = general_roughness_at(plan)
    for v in plan.views:
        cx, cy = v.origin_on_sheet
        w, h = v.size_mm
        assert not _box_hit((x, y - 2, x + 48, y + 10), (cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2)), v.type
    g = resolve_part(PartSpec.model_validate(GRID))
    gp = plan_part_drawing(DrawingSpec(model=g.name), g)
    assert "left" not in {v.type for v in gp.views}
    assert any(f["rule"] == "D021" and "左视图" in f["message"] for f in gp.extra_findings)
    keep = plan_part_drawing(DrawingSpec.model_validate({"model": g.name, "views": [{"type": "front"}, {"type": "top"},
                                                                                     {"type": "left"}]}), g)
    assert "left" in {v.type for v in keep.views}   # 用户显式指定时不省略


def test_polygon_profile_dims_and_callout_anchor_choice():
    """L 形外形：壁厚经坐标尺寸标出；多孔引出标注可指向任一实例（选最好放的），局部剖区域文字不压。"""
    from test_core import L_BRACKET

    from cadmcp.detailing import plan_part_drawing
    from cadmcp.ir.resolve import resolve_part
    p = resolve_part(PartSpec.model_validate(L_BRACKET))
    plan = plan_part_drawing(DrawingSpec(model=p.name), p)
    top_ords = {(d.axis, d.value) for d in plan.dims if d.view == "top" and d.kind == "ordinate"}
    t = next(v for (a, v) in top_ords if v not in (0.0,))
    assert 0 < t < max(p.bbox.size)                      # 壁厚坐标（L 形内角）
    slot = next(d for d in plan.dims if d.kind == "callout" and "腰型孔" in d.text)
    lb = slot.extra["label"]
    assert lb["anchor"] != lb["elbow"]


def test_callout_with_attached_gtol_keeps_instance():
    from test_core import PLATE_PRECISION

    from cadmcp.detailing import plan_part_drawing
    from cadmcp.ir.resolve import resolve_part
    p = resolve_part(PartSpec.model_validate(PLATE_PRECISION))
    plan = plan_part_drawing(DrawingSpec(model=p.name), p)
    co = next(d for d in plan.dims if d.kind == "callout" and d.ref == "Pin")
    g = next(d for d in plan.dims if d.kind == "gtol" and d.ref.endswith("hole:Pin"))
    assert co.at == g.at   # 位置度框格挂在孔标注下方，必须是同一个孔


def test_regular_row_uses_pitch_dim_not_hole_table():
    """等距一排孔：首孔坐标 + 19×50(=950) 节距尺寸，不用孔表；SW 加前后缀保持关联值。"""
    from cadmcp.adapters.solidworks.compile import compile_drawing
    from cadmcp.detailing import plan_part_drawing
    from cadmcp.ir import DrawingSpec, PartSpec
    from cadmcp.ir.resolve import resolve_part
    rail = {"name": "RAIL", "part_no": "RL-01", "title": "导轨垫条", "material": "45#",
            "base": {"type": "extrude", "profile": {"shape": "rect", "width": 1000, "height": 50}, "thickness": 20},
            "features": [{"feature": "hole", "name": "Mnt", "type": "counterbore", "size": "M6",
                          "pattern": {"kind": "grid", "origin": [-475, 0], "nx": 20, "ny": 1, "dx": 50, "dy": 0}}]}
    p = resolve_part(PartSpec.model_validate(rail))
    plan = plan_part_drawing(DrawingSpec(model="RAIL"), p)
    assert not plan.hole_table
    pd = next(d for d in plan.dims if d.ref == "Mnt.pitch")
    assert pd.text == "19×50(=950)" and pd.value == 950 and pd.extra["orient"] == "h"
    ords = sorted(d.value for d in plan.dims if d.kind == "ordinate" and d.view == "top" and d.axis == "x")
    assert ords == [0, 25]                                          # 只标首孔
    ops = compile_drawing(plan, "x.SLDDRW")
    op = next(o for o in ops if o.op == "overall_dimension" and o.args["ref"] == "Mnt.pitch")
    assert op.args["prefix"] == "19×50(=" and op.args["suffix"] == ")" and len(op.args["pts"]) == 2
