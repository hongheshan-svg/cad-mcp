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
    assert not d["rules"]["findings"], d["rules"]["findings"]   # 放得下（让出轴测图），渲染回读一致（D017）
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

