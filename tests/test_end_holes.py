"""轴端螺孔：底孔/螺纹深、校验、虚线表示与 GB/T 4459.1 简化标注、原生建模操作、断面切到螺孔。"""
import pytest

from cadmcp.adapters.creo.compile import compile_part_tk
from cadmcp.adapters.solidworks.compile import compile_drawing, compile_part
from cadmcp.detailing import plan_part_drawing, shaft_dia_z
from cadmcp.errors import CadError
from cadmcp.ir import DrawingSpec, PartSpec
from cadmcp.ir.resolve import resolve_part
from cadmcp.render_svg import render

SP = {"name": "EH1", "part_no": "EH-01", "title": "轴", "material": "45#",
      "base": {"type": "shaft", "chamfer": 1, "end_holes": [{"size": "M8", "depth": 16}, {"side": "end", "size": "M6", "depth": 12}],
               "steps": [{"length": 40, "diameter": 25, "tolerance": "k6"}, {"length": 60, "diameter": 30},
                         {"length": 50, "diameter": 20, "tolerance": "k6"}]},
      "features": [{"feature": "keyway", "name": "KW", "step": 0, "length": 28}]}


def _spec(**kw):
    return PartSpec.model_validate({**SP, "base": {**SP["base"], **kw}} if kw else SP)


def test_resolve():
    p = resolve_part(_spec())
    a, b = p.end_holes
    assert (a["tap"], a["depth"], a["drill"], a["z0"], a["z1"]) == (6.75, 16, 20, 0, 20)
    assert (b["drill"], b["z0"], b["z1"]) == (15, 135, 150)
    assert a["callout"] == "M8-6H↓16 孔↓20"


@pytest.mark.parametrize("holes,msg", [
    ([{"size": "M24", "depth": 20}], "超出轴端"),
    ([{"size": "M8", "depth": 16, "drill_depth": 10}], "底孔深"),
])
def test_bad(holes, msg):
    with pytest.raises(CadError, match=msg):
        resolve_part(_spec(end_holes=holes))


def test_conflict_with_center_hole():
    with pytest.raises(CadError, match="中心孔"):
        resolve_part(_spec(center_holes={"ends": "start"}))


def test_drawing_and_native():
    spec = _spec()
    p = resolve_part(spec)
    plan = plan_part_drawing(DrawingSpec(model="EH1"), p)
    co = [d for d in plan.dims if d.ref.startswith("eh:")]
    assert [d.text for d in co] == ["M8-6H↓16 孔↓20", "M6-6H↓12 孔↓15"] and all("label" in d.extra for d in co)
    assert not 0 <= shaft_dia_z(p, 0, plan.scale) <= 20 + 2  # 直径文字避开螺孔虚线
    svg, out = render(plan, p)
    assert "stroke-dasharray='3,1'" in svg and not out["crossings"] and not out["collisions"]
    sw = compile_part(spec, p)
    names = [(o.op, o.args.get("feature")) for o in sw]
    assert ("keyway_cut", "EH1") in names and ("cosmetic_thread", "EH1_THR") in names
    thr = next(o for o in sw if o.op == "cosmetic_thread" and o.args["feature"] == "EH2_THR")
    assert thr.args["external"] is False and thr.args["edge_pts"][0][2] == 150
    assert any(o.op == "leader_note" and o.args["text"].startswith("M8") for o in compile_drawing(plan, "EH1.SLDPRT"))
    tk = compile_part_tk(spec, p)
    assert any(o.op == "tk.feat.cosmetic_thread" and o.args.get("hole_feature") == "EH1" for o in tk)
