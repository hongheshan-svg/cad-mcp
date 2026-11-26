"""热处理：整体调质/正火 → 技术要求；轴颈局部淬火 → 技术要求 + 粗点画线（预览、SW、Creo）。"""
import pytest

from cadmcp.adapters.creo.drawing import compile_drawing as creo_drawing
from cadmcp.adapters.solidworks.compile import compile_drawing
from cadmcp.detailing import check_drawing, plan_part_drawing
from cadmcp.errors import CadError
from cadmcp.ir import DrawingSpec, PartSpec
from cadmcp.ir.resolve import resolve_part
from cadmcp.render_svg import render

HT = {"name": "HT1", "part_no": "HT-01", "title": "轴", "material": "45#",
      "heat_treatment": {"local": [{"target": "step:0"}, {"target": "step:2"}]},
      "base": {"type": "shaft", "chamfer": 1, "steps": [
          {"length": 30, "diameter": 25, "tolerance": "k6"}, {"length": 50, "diameter": 32},
          {"length": 30, "diameter": 25, "tolerance": "k6"}]}}


def _plan(spec):
    p = resolve_part(PartSpec.model_validate(spec))
    return p, plan_part_drawing(DrawingSpec(model="HT1"), p)


def test_notes_and_chain_lines():
    p, plan = _plan(HT)
    assert "4. 调质处理 220~250HBW。" in plan.notes
    assert "5. Φ25k6（2 处） 轴颈表面（图中粗点画线处）高频感应淬火 40~45HRC，淬硬层深 1~1.5。" in plan.notes
    ch = [d for d in plan.dims if d.kind == "chain"]
    assert [d.ref for d in ch] == ["heat:0", "heat:2"]
    (xa, ya), (xb, yb) = ch[0].extra["sheet_pts"]
    assert ya == yb and xb - xa == pytest.approx(30 * plan.scale - 1)
    svg, out = render(plan, p)
    assert svg.count("stroke-dasharray='8,1.5,1,1.5'") == 2 and not out["crossings"]
    assert not [f for f in check_drawing(plan, out) if f["severity"] == "error"]
    assert len([o for o in compile_drawing(plan, "HT1.SLDPRT") if o.op == "view_line"]) == 2
    tk = [o for o in creo_drawing(plan, "HT1.prt", toolkit=True) if o.op == "tk.drw.line"]
    assert len(tk) == 2 and tk[0].args["font"] == "CTRLFONT"


def test_custom_and_errors():
    _, plan = _plan({**HT, "material": "40Cr", "heat_treatment": {"process": "normalize", "local": [
        {"target": "step:0", "method": "flame", "hardness": "50~55HRC", "depth": "2~3"}]}})
    assert "4. 正火处理 179~229HBW。" in plan.notes and any("Φ25k6 轴颈表面（图中粗点画线处）火焰淬火 50~55HRC，淬硬层深 2~3" in n for n in plan.notes)
    with pytest.raises(CadError, match="hardness"):
        resolve_part(PartSpec.model_validate({**HT, "material": "Q235", "heat_treatment": {}}))
    with pytest.raises(CadError, match="step:"):
        resolve_part(PartSpec.model_validate({**HT, "heat_treatment": {"local": [{"target": "step:9"}]}}))
    _, plan = _plan({**HT, "heat_treatment": {"process": "none", "local": [{"target": "step:1"}]}})
    assert not any("调质" in n for n in plan.notes) and any(n.startswith("4. Φ32 轴颈") for n in plan.notes)
