"""法兰/端盖轴向均布孔：校验、规定画法（旋转到剖切平面）、左视图 n×Φd EQS 与分布圆、原生建模。"""
import pytest

from cadmcp.adapters.creo.compile import compile_part_tk
from cadmcp.adapters.solidworks.compile import compile_drawing, compile_part
from cadmcp.detailing import plan_part_drawing
from cadmcp.errors import CadError
from cadmcp.ir import DrawingSpec, PartSpec
from cadmcp.ir.resolve import resolve_part
from cadmcp.render_svg import render

EC = {"name": "EC1", "part_no": "EC-01", "title": "轴承端盖", "material": "HT200",
      "base": {"type": "shaft", "chamfer": 1, "steps": [
          {"length": 10, "diameter": 110}, {"length": 18, "diameter": 72, "tolerance": "f9"}],
               "bores": [{"diameter": 40}],
               "flange_holes": [{"step": 0, "count": 6, "pcd": 92, "diameter": 9}]}}


def _spec(**fh):
    return {**EC, "base": {**EC["base"], "flange_holes": [{**EC["base"]["flange_holes"][0], **fh}]}}


def test_resolve():
    p = resolve_part(PartSpec.model_validate(EC))
    fh = p.flange_holes[0]
    assert (fh["z0"], fh["z1"], fh["callout"]) == (0, 10, "6×Φ9 EQS")
    assert fh["pts"][0] == pytest.approx((0, -46), abs=1e-6)  # 第一个孔在主视图正上方（模型 −Y）


@pytest.mark.parametrize("kw,msg", [
    ({"pcd": 104}, "超出"), ({"pcd": 46}, "超出"), ({"step": 1, "pcd": 60}, "挡住"), ({"step": 5}, "超出范围"),
])
def test_bad(kw, msg):
    with pytest.raises(CadError, match=msg):
        resolve_part(PartSpec.model_validate(_spec(**kw)))


def test_needs_bore():
    with pytest.raises(CadError, match="内孔"):
        resolve_part(PartSpec.model_validate({**EC, "base": {**EC["base"], "bores": []}}))


@pytest.mark.parametrize("n", [3, 4, 6, 8])
def test_drawing(n):
    p = resolve_part(PartSpec.model_validate(_spec(count=n)))
    plan = plan_part_drawing(DrawingSpec(model="EC1"), p)
    assert [v.type for v in plan.views] == ["front", "left"]
    left = {d.ref: d for d in plan.dims if d.view == "left"}
    assert left["FH1"].text == f"{n}×Φ9 EQS" and left["FH1.pcd"].text == "Φ92"
    svg, out = render(plan, p)
    assert not out["crossings"] and not out["collisions"]
    assert {"FH1", "FH1.pcd"} <= set(out["dim_refs"])
    W = plan.sheet_size[0]
    assert all(d.extra["label"]["end"][0] < W - 10 for d in left.values())


def test_native():
    sp = PartSpec.model_validate(EC)
    p = resolve_part(sp)
    sw = compile_part(sp, p)
    circ = next(o for o in sw if o.op == "sketch_circles" and o.args["sketch"] == "SK_FH1")
    assert len(circ.args["points"]) == 6 and circ.args["diameter"] == 9
    assert any(o.op == "keyway_cut" and o.args["feature"] == "FH1" and o.args["depth"] == 10 for o in sw)
    plan = plan_part_drawing(DrawingSpec(model="EC1"), p)
    notes = [o for o in compile_drawing(plan, "EC1.SLDPRT") if o.op == "leader_note" and o.args["view"] == "left"]
    assert {o.args["text"] for o in notes} == {"6×Φ9 EQS", "Φ92"}
    tk = compile_part_tk(sp, p)
    assert len(next(o for o in tk if o.op == "tk.feat.extrude" and o.args["feature"] == "FH1").args["entities"]) == 6
