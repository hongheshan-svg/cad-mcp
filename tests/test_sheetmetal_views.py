"""钣金件默认视图：自动加展开图；俯视图/轴测图画出翻边；背面盲特征画虚线。"""
from cadmcp.detailing import plan_part_drawing
from cadmcp.ir import DrawingSpec, PartSpec
from cadmcp.ir.resolve import resolve_part
from cadmcp.render_svg import render

SM = {"name": "SMB", "part_no": "SM-01", "title": "钣金支架", "material": "SPCC", "category": "sheet_metal",
      "base": {"type": "sheet_metal", "profile": {"shape": "rect", "width": 120, "height": 80}, "thickness": 2,
               "flanges": [{"edge": "front", "length": 40}, {"edge": "back", "length": 40}]}}


def _part():
    return resolve_part(PartSpec.model_validate(SM))


def test_flat_pattern_added_by_default():
    plan = plan_part_drawing(DrawingSpec(model="SMB"), _part())
    assert "flat_pattern" in [v.type for v in plan.views]
    assert any(f["rule"] == "D021" and "展开图" in f["message"] for f in plan.extra_findings)


def test_explicit_views_respected():
    plan = plan_part_drawing(DrawingSpec.model_validate({"model": "SMB", "views": [{"type": "front"}, {"type": "top"}]}),
                             _part())
    assert [v.type for v in plan.views] == ["front", "top"]


def test_iso_and_top_draw_flanges():
    p = _part()
    plan = plan_part_drawing(DrawingSpec.model_validate({"model": "SMB", "views": [{"type": "top"}, {"type": "iso"}]}), p)
    bp = resolve_part(PartSpec.model_validate({**SM, "base": {**SM["base"], "flanges": []}}))
    bare = plan_part_drawing(DrawingSpec.model_validate({"model": "SMB", "views": [{"type": "top"}, {"type": "iso"}]}), bp)
    n = lambda svg: svg.count("<line") + svg.count("<poly")  # noqa: E731
    assert n(render(plan, p)[0]) >= n(render(bare, bp)[0]) + 2 * 9 + 2  # 每个翻边：轴测 9 条棱 + 俯视板厚条


def test_back_face_blind_pocket_is_hidden():
    from cast_bracket import cast_base
    p = resolve_part(PartSpec.model_validate(cast_base()))
    plan = plan_part_drawing(DrawingSpec(model=p.name), p)
    svg, _ = render(plan, p)
    assert "stroke-dasharray='3,1'" in svg  # 底面 120×60 型腔在俯视图中为细虚线


def test_flat_pattern_bend_lines_located():
    """展开图：折弯线自外轮廓起有基线定位尺寸；尺寸值不带多余的 .0。"""
    p = _part()
    plan = plan_part_drawing(DrawingSpec(model="SMB"), p)
    fl = {d.ref: d for d in plan.dims if d.view == "flat_pattern" and d.kind == "linear"}
    assert fl["flat_w"].text == "120"
    bends = sorted((d for r, d in fl.items() if r.startswith("flat_bend")), key=lambda d: d.value)
    assert len(bends) == 2 and bends[0].value < bends[1].value < fl["flat_h"].value
    assert all(len(d.extra["sheet_pts"]) == 2 and d.extra["orient"] == "v" for d in bends)
    assert bends[0].extra["sheet_pos"][0] > bends[1].extra["sheet_pos"][0]  # 远的一道在外层
    from cadmcp.adapters.solidworks.compile import compile_drawing
    ops = [o for o in compile_drawing(plan, "SMB.SLDPRT") if o.op == "overall_dimension"]
    assert {"flat_bend0", "flat_bend1"} <= {o.args["ref"] for o in ops}
