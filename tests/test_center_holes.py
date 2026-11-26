"""轴端中心孔：GB/T 145 查表、按轴径推荐、GB/T 4459.5 简化标注（预览 + SW/Creo 引线注释）、技术要求。"""
import pytest

from cadmcp.adapters.creo.drawing import tk_detail_ops
from cadmcp.adapters.solidworks.compile import compile_drawing
from cadmcp.detailing import plan_part_drawing
from cadmcp.errors import CadError
from cadmcp.ir import DrawingSpec, PartSpec
from cadmcp.ir.resolve import resolve_part
from cadmcp.render_svg import render
from cadmcp.standards import center_hole


def _shaft(ch, d_end=20):
    return resolve_part(PartSpec.model_validate({
        "name": "CH1", "part_no": "CH-01", "title": "轴", "material": "45#",
        "base": {"type": "shaft", "chamfer": 1, "center_holes": ch,
                 "steps": [{"length": 40, "diameter": 25}, {"length": 60, "diameter": 30}, {"length": 30, "diameter": d_end}]}}))


def test_table_and_recommendation():
    assert center_hole("B", 2.5, 30)["code"] == "B2.5/8"
    assert center_hole("A", 4.0, 60)["code"] == "A4/8.5"
    assert center_hole("B", None, 25)["d"] == 2.5 and center_hole("B", None, 40)["d"] == 3.15
    with pytest.raises(ValueError):
        center_hole("A", 3.0, 30)


def test_too_big_for_shaft_end():
    with pytest.raises(CadError, match="超出轴端"):
        _shaft({"d": 4}, d_end=12)


def test_drawing_callouts_and_notes():
    p = _shaft({"keep": "required"})
    assert [h["end"] for h in p.center_holes] == ["start", "end"]
    plan = plan_part_drawing(DrawingSpec(model="CH1"), p)
    chs = [d for d in plan.dims if d.ref.startswith("ch:")]
    assert [d.text for d in chs] == ["GB/T 4459.5-B2.5/8", "GB/T 4459.5-B2.5/8"]
    W, _ = plan.sheet_size
    for d in chs:
        lb = d.extra["label"]
        assert 10 < min(lb["end"][0], lb["elbow"][0]) and max(lb["end"][0], lb["elbow"][0]) < W - 10
    assert any("中心孔完工后须保留" in n for n in plan.notes)
    svg, out = render(plan, p)
    assert "GB/T 4459.5-B2.5/8" in svg and not out["crossings"] and not out["collisions"]
    sw = [o for o in compile_drawing(plan, "CH1.SLDPRT") if o.op == "leader_note" and o.args["ref"].startswith("ch:")]
    assert len(sw) == 2 and all("anchor" in o.args for o in sw)
    creo = [o for o in tk_detail_ops(plan) if o.op == "tk.drw.note" and o.args["text"].startswith("GB/T 4459.5")]
    assert len(creo) == 2 and all("leader_to" in o.args for o in creo)


def test_optional_has_no_note():
    p = _shaft({"type": "A", "ends": "start"})
    plan = plan_part_drawing(DrawingSpec(model="CH1"), p)
    assert [d.text for d in plan.dims if d.ref.startswith("ch:")] == ["GB/T 4459.5-A2.5/5.3"]
    assert not any("中心孔" in n for n in plan.notes)
