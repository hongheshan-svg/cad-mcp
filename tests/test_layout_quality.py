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


def test_large_hole_label_leaves_view_from_contour():
    """大孔标注：引线从孔轮廓沿径向引出（箭头），带框格的标注放到视图外，不骑在视图轮廓线上。"""
    p = resolve_part(PartSpec.model_validate(cast_base()))
    plan = plan_part_drawing(DrawingSpec(model=p.name), p)
    v = next(v for v in plan.views if v.type == "top")
    cx, cy = v.origin_on_sheet
    w, h = v.size_mm
    big = max(p.holes, key=lambda h_: h_.max_dia)
    d = next(d for d in plan.dims if d.kind == "callout" and d.ref == big.name)
    lb = d.extra["label"]
    assert lb.get("on_edge")
    hx, hy = d.at
    from cadmcp.detailing import _uv_sheet
    sx, sy = _uv_sheet(v, p.bbox, (hx, hy))
    r = big.max_dia / 2 * v.scale
    assert abs(((lb["anchor"][0] - sx) ** 2 + (lb["anchor"][1] - sy) ** 2) ** 0.5 - r) < 0.05
    x0, x1 = sorted((lb["elbow"][0], lb["end"][0]))
    outside = x1 < cx - w / 2 or x0 > cx + w / 2 or lb["elbow"][1] - 7 > cy + h / 2 or lb["elbow"][1] + 4 < cy - h / 2
    assert outside
