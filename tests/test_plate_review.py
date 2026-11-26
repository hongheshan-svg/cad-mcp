"""板块类加工审查规则 R009–R011。"""
from cadmcp import rules
from cadmcp.ir import PartSpec
from cadmcp.ir.resolve import resolve_part


def _rules(features, t=60):
    spec = {"name": "P", "part_no": "P-01", "title": "块", "material": "45#", "surface_treatment": "发黑",
            "base": {"type": "extrude", "profile": {"shape": "rect", "width": 100, "height": 80}, "thickness": t},
            "features": features}
    return {(f["rule"], f["target"], f["severity"]) for f in rules.check_part(resolve_part(PartSpec.model_validate(spec)))}


def test_deep_hole():
    assert ("R009", "H", "warning") in _rules([{"feature": "hole", "name": "H", "type": "simple", "size": 4,
                                                 "positions": [[0, 0]]}])
    assert not any(r[0] == "R009" for r in _rules([{"feature": "hole", "name": "H", "type": "simple", "size": 8,
                                                      "positions": [[0, 0]]}]))


def test_blind_tap_drill_margin():
    r = _rules([{"feature": "hole", "name": "T", "type": "tapped", "size": "M8", "depth": 14, "thread_depth": 12,
                 "positions": [[0, 0]]}])
    assert ("R010", "T", "warning") in r


def test_pocket_corners():
    pk = {"feature": "pocket", "name": "PK", "profile": {"shape": "rect", "width": 40, "height": 30}, "depth": 18}
    assert ("R011", "PK", "warning") in _rules([pk])
    small = {**pk, "profile": {**pk["profile"], "corner_radius": 2}}
    assert ("R011", "PK", "info") in _rules([small])
    ok = {**pk, "profile": {**pk["profile"], "corner_radius": 4}}
    assert not any(r[0] == "R011" for r in _rules([ok]))


def test_pocket_corner_radius_drawn():
    """型腔/板圆角：画图轮廓带圆弧，标注写 4×R，引线落在弧中点上。"""
    from cadmcp.detailing import plan_part_drawing
    from cadmcp.ir import DrawingSpec
    from cadmcp.render_svg import render
    spec = {"name": "PK", "part_no": "PK-01", "title": "型腔板", "material": "AL6061-T6", "surface_treatment": "阳极氧化",
            "base": {"type": "extrude", "profile": {"shape": "rect", "width": 120, "height": 80, "corner_radius": 6},
                     "thickness": 25},
            "features": [{"feature": "pocket", "name": "PK", "depth": 15,
                          "profile": {"shape": "rect", "width": 60, "height": 40, "corner_radius": 4}}]}
    p = resolve_part(PartSpec.model_validate(spec))
    pr = p.pockets[0]["profile"]
    assert len(pr.drawn()) == 40 and len(pr.outline) == 4
    plan = plan_part_drawing(DrawingSpec(model="PK"), p)
    d = next(d for d in plan.dims if d.ref == "PK")
    assert d.text == "60×40 深15 4×R4"
    import math
    assert math.isclose(math.hypot(d.at[0] - 26, d.at[1] - 16), 4, abs_tol=0.05)  # 右上圆角圆心 (26, 16)
    assert not render(plan, p)[1]["crossings"]
    ords = {(x.axis, x.value) for x in plan.dims if x.kind == "ordinate" and x.view == "top"}
    assert {("x", 30.0), ("y", 20.0)} <= ords  # 型腔左下角定位
