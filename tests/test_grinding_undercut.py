"""砂轮越程槽（GB/T 6403.5）与局部放大图（GB/T 4458.1）。"""
import pytest

from cadmcp.adapters.creo.drawing import compile_drawing as creo_drawing
from cadmcp.adapters.solidworks.compile import compile_drawing, compile_part
from cadmcp.detailing import plan_part_drawing
from cadmcp.errors import CadError
from cadmcp.ir import DrawingSpec, PartSpec
from cadmcp.ir.resolve import resolve_part
from cadmcp.render_svg import render
from cadmcp.standards import grinding_undercut

GR = {"name": "GR1", "part_no": "GR-01", "title": "轴", "material": "45#",
      "base": {"type": "shaft", "chamfer": 1, "steps": [
          {"length": 30, "diameter": 25, "tolerance": "k6"}, {"length": 50, "diameter": 32},
          {"length": 40, "diameter": 30, "tolerance": "m6"}, {"length": 40, "diameter": 25}]},
      "features": [{"feature": "groove", "name": "G1", "type": "grinding", "step": 0},
                   {"feature": "groove", "name": "G2", "type": "grinding", "step": 2},
                   {"feature": "groove", "name": "G3", "type": "circlip", "step": 3, "position": "start"}],
      "roughness": [{"target": "step:0", "ra": 0.8}]}


def test_table_and_position():
    assert grinding_undercut(8) == (1.0, 0.2, 0.5) and grinding_undercut(25) == (3.0, 0.4, 1.0)
    p = resolve_part(PartSpec.model_validate(GR))
    g1, g2 = (f for f in p.shaft_features if f["name"] in ("G1", "G2"))
    assert (g1["z0"], g1["z1"], g1["d2"], g1["callout"]) == (27, 30, 24.2, "越程槽 3×0.4")  # 右侧 Φ32 轴肩
    assert (g2["z0"], g2["z1"]) == (80, 83)  # 左侧 Φ32 轴肩
    with pytest.raises(CadError, match="超出轴段"):
        resolve_part(PartSpec.model_validate({**GR, "features": [
            {"feature": "groove", "name": "G1", "type": "grinding", "step": 0, "position": 29}]}))


def test_detail_views():
    spec = PartSpec.model_validate(GR)
    p = resolve_part(spec)
    plan = plan_part_drawing(DrawingSpec(model="GR1"), p)
    dv = [v for v in plan.views if v.type == "detail"]
    assert [(v.label, v.scale, v.target) for v in dv] == [("I", 5.0, "G1"), ("II", 5.0, "G2")]
    assert all("circle" in v.detail for v in dv)
    a, b = dv
    assert a.origin_on_sheet[0] + a.size_mm[0] / 2 < b.origin_on_sheet[0] - b.size_mm[0] / 2  # 互不重叠
    texts = {d.ref: d.text for d in plan.dims}
    assert texts["G1_w"] == "3" and texts["G1_h"] == "0.4" and "G1" not in texts  # 主视图不再引线标注
    assert "G3" in texts  # 挡圈槽照旧
    svg, out = render(plan, p)
    assert not out["crossings"] and not out["collisions"]
    assert {"G1_w", "G1_h", "G2_w", "G2_h"} <= set(out["dim_refs"])
    assert svg.count(">5:1<") == 2
    for f in plan.views:  # 放大图在图框内
        x, y = f.origin_on_sheet
        assert 0 < x - f.size_mm[0] / 2 and x + f.size_mm[0] / 2 < plan.sheet_size[0] and 0 < y - f.size_mm[1] / 2
    sw = compile_part(spec, p)
    assert any(o.op == "revolve" and o.args.get("feature") == "G1" for o in sw)
    ops = compile_drawing(plan, "GR1.SLDPRT")
    det = [o for o in ops if o.op == "detail_view"]
    assert [o.args["label"] for o in det] == ["I", "II"] and det[0].args["scale"] == [5, 1]
    assert {o.args["ref"] for o in ops if o.op == "shaft_dimension" and o.args["view"].startswith("detail:")} == \
        {"G1_w", "G1_h", "G2_w", "G2_h"}
    tk = creo_drawing(plan, "GR1.prt", toolkit=True)
    assert [o.args["view"] for o in tk if o.op == "tk.drw.detail_view"] == ["V_DET_I", "V_DET_II"]
    assert any(o.op == "tk.drw.dim" and o.args["view"] == "V_DET_I" for o in tk)
    assert not any(o.op == "creoson.drawing_create_gen_view" and "DETAIL" in str(o.args) for o in tk)
