"""出图质量：引线/尺寸线不得穿过标注文字（D022）。"""
from cast_bracket import cast_base

from cadmcp.detailing import plan_part_drawing
from cadmcp.ir import DrawingSpec, PartSpec
from cadmcp.ir.resolve import resolve_part
from cadmcp.render_svg import _seg_hits_box, _Svg, render


def test_seg_hits_box():
    assert _seg_hits_box(0, 0, 10, 10, 4, 4, 6, 6)
    assert not _seg_hits_box(0, 0, 10, 0, 4, 1, 6, 3)
    assert _seg_hits_box(5, 5, 5, 5.1, 4, 4, 6, 6)  # 线段完全在框内


def test_crossing_detected():
    s = _Svg(420, 297)
    s.text(10, 10, "Ra 1.6", 3)
    s.line(0, 11, 30, 11, 0.18)
    assert s.crossings() == ["Ra 1.6"]
    s2 = _Svg(420, 297)
    s2.text(10, 10, "Ra 1.6", 3)
    s2.line(0, 9.5, 30, 9.5, 0.18)  # 文字下方的标注横线不算
    assert s2.crossings() == []


def test_casting_drawing_no_crossings():
    p = resolve_part(PartSpec.model_validate(cast_base()))
    plan = plan_part_drawing(DrawingSpec(model=p.name), p)
    _, out = render(plan, p)
    assert out["crossings"] == [] and out["collisions"] == []
