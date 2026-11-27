"""板块类加工审查规则 R009–R012。"""
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


def test_pocket_floor_roughness():
    from cadmcp.detailing import plan_part_drawing
    from cadmcp.ir import DrawingSpec
    import pytest
    from cadmcp.errors import CadError
    spec = {"name": "PF", "part_no": "PF-01", "title": "型腔板", "material": "45#", "surface_treatment": "发黑",
            "base": {"type": "extrude", "profile": {"shape": "rect", "width": 120, "height": 80}, "thickness": 25},
            "features": [{"feature": "pocket", "name": "PK", "depth": 15,
                          "profile": {"shape": "rect", "width": 60, "height": 40, "corner_radius": 4}}],
            "roughness": [{"target": "pocket:PK", "ra": 1.6}]}
    p = resolve_part(PartSpec.model_validate(spec))
    plan = plan_part_drawing(DrawingSpec(model="PF"), p)
    d = next(d for d in plan.dims if d.ref == "ra:pocket:PK")
    assert d.view == "front" and d.at == (-15.0, 10.0) and d.extra["normal"] == (0, 1)  # 底面 z = 25 − 15
    bad = {**spec, "tolerances": [{"type": "flatness", "value": 0.02, "target": "pocket:PK"}]}
    with pytest.raises(CadError):
        resolve_part(PartSpec.model_validate(bad))


def test_views_balanced_vertically():
    """视图组上下留白均匀，不挤在左上角；轴测图原位。"""
    import sys
    sys.path.insert(0, "examples")
    from cast_bracket import cast_base
    from cadmcp.detailing import plan_part_drawing
    from cadmcp.ir import DrawingSpec
    p = resolve_part(PartSpec.model_validate(cast_base()))
    plan = plan_part_drawing(DrawingSpec(model=p.name), p)
    W, H = plan.sheet_size
    proj = [v for v in plan.views if v.type != "iso"]
    top = H - 7 - max(v.origin_on_sheet[1] + v.size_mm[1] / 2 + 12 for v in proj)
    bot = min(v.origin_on_sheet[1] - v.size_mm[1] / 2 - 30 for v in proj) - 7
    assert abs(top - bot) < 3, (top, bot)


def test_hole_mouth_chamfer():
    spec = {"name": "HC", "part_no": "HC-01", "title": "板", "material": "45#", "surface_treatment": "发黑",
            "base": {"type": "extrude", "profile": {"shape": "rect", "width": 100, "height": 60}, "thickness": 20},
            "features": [{"feature": "hole", "name": "T", "type": "tapped", "size": "M8", "depth": 16, "thread_depth": 12,
                          "chamfer": 1, "positions": [[-30, 0]]},
                         {"feature": "hole", "name": "P", "type": "simple", "size": 8, "tolerance": "H7", "chamfer": 0.5,
                          "positions": [[0, 0]]}]}
    p = resolve_part(PartSpec.model_validate(spec))
    t, pin = p.holes
    assert t.csink == 10 and t.callout.endswith("孔口C1") and pin.csink == 9 and pin.max_dia == 9
    from cadmcp.adapters.solidworks.compile import compile_part
    ops = compile_part(PartSpec.model_validate(spec), p)
    assert any(o.op == "extrude_cut" and o.args["feature"] == "T_CB" and o.args["draft_deg"] == 45 for o in ops)
    import pytest
    with pytest.raises(Exception):
        PartSpec.model_validate({**spec, "features": [{**spec["features"][0], "type": "counterbore"}]})


def test_iso_hidden_lines():
    """轴测图消隐：长方体只画 9 条可见棱（顶 4 + 底 2 + 竖 3）；凹轮廓回退旧画法。"""
    from cadmcp.render_svg import _clip_convex, _iso_prism_visible

    class S:
        def __init__(self):
            self.lines = []

        def line(self, *a):
            self.lines.append(a)
    s = S()
    rect = [(0, 0), (10, 0), (10, 5), (0, 5)]
    assert _iso_prism_visible(s, lambda x, y, z: (x - y, (x + y) / 2 + z), rect, 0, 2)
    assert len(s.lines) == 2 + 3
    assert not _iso_prism_visible(S(), lambda x, y, z: (x, y), [(0, 0), (4, 0), (4, 4), (2, 1), (0, 4)], 0, 1)
    sq = [(0, 0), (1, 0), (1, 1), (0, 1)]
    assert _clip_convex((-1, 0.5), (2, 0.5), sq) == ((0, 0.5), (1, 0.5))
    assert _clip_convex((-1, 2), (2, 2), sq) is None


def test_painter_order():
    from cadmcp.ir.resolve import Box
    from cadmcp.render_svg import _painter_order
    top = Box(0, 10, 0, 10, 10, 11)    # 顶板（近：+Z）
    leg = Box(0, 1, 0, 1, 0, 10)       # 腿在顶板下方 → 先画
    far = Box(9, 10, 9, 10, 0, 10)     # 远角的腿（+X+Y 远）
    order = _painter_order([top, leg, far])
    assert order.index(leg) < order.index(top) and order.index(far) < order.index(leg)


def test_hole_on_pocket_wall():
    pk = {"feature": "pocket", "name": "PK", "profile": {"shape": "rect", "width": 40, "height": 30, "corner_radius": 4},
          "depth": 10}
    h = lambda x: {"feature": "hole", "name": "H", "type": "clearance", "size": "M6", "positions": [[x, 0]]}  # noqa: E731
    assert ("R012", "H[0]", "error") in _rules([pk, h(20)], t=20)  # 孔心正在型腔边上
    assert ("R012", "H[0]", "warning") in _rules([pk, h(23.5)], t=20)  # 孔边离型腔边 0.2 mm
    assert not any(r[0] == "R012" for r in _rules([pk, h(0)], t=20))  # 整孔在型腔底
    assert not any(r[0] == "R012" for r in _rules([pk, h(30)], t=20))


def test_hole_in_pocket_floor_needs_at():
    pk = {"feature": "pocket", "name": "PK", "profile": {"shape": "rect", "width": 40, "height": 30, "corner_radius": 5},
          "depth": 12}
    t = {"feature": "hole", "name": "T", "type": "tapped", "size": "M6", "depth": 10, "positions": [[0, 0]]}
    assert ("R013", "T[0]", "error") in _rules([pk, t], t=20)
    assert ("R013", "T[0]", "error") in _rules([pk, {**t, "depth": 16}], t=20)  # 穿过型腔底但孔深按原表面量
    assert not any(r[0] == "R013" for r in _rules([pk, {**t, "at": 8, "depth": 6}], t=20))  # 从型腔底面起算
    assert not any(r[0] == "R013" for r in _rules([pk, {**t, "depth": "through"}], t=20))


def test_hole_off_boss_needs_at():
    b = {"feature": "boss", "name": "B", "height": 15, "profile": {"shape": "circle", "diameter": 30, "center": [0, 0]}}
    t = lambda pos, **k: {"feature": "hole", "name": "T", "type": "tapped", "size": "M6", "depth": 8,  # noqa: E731
                          "positions": [pos], **k}
    assert ("R014", "T[0]", "error") in _rules([b, t([35, 25])], t=10)  # 板面上的孔从凸台顶起算：悬空
    assert not any(r[0] == "R014" for r in _rules([b, t([35, 25], at=10)], t=10))
    assert not any(r[0] == "R014" for r in _rules([b, t([0, 0])], t=10))  # 凸台顶上的孔
    assert ("R014", "T[0]", "error") in _rules([b, t([15, 0])], t=10)  # 骑在凸台边上


def test_boss_drawn_and_dimensioned():
    """凸台：俯视图画轮廓、主视图外形为板 + 凸台（不是整块包围盒）、标注“Φd 高h”并定位圆心。"""
    from cadmcp.detailing import plan_part_drawing
    from cadmcp.ir import DrawingSpec
    from cadmcp.render_svg import render
    spec = {"name": "B", "part_no": "B-01", "title": "凸台板", "material": "Q235", "surface_treatment": "发黑",
            "base": {"type": "extrude", "profile": {"shape": "rect", "width": 160, "height": 120}, "thickness": 10},
            "features": [{"feature": "boss", "name": "BS", "height": 15,
                          "profile": {"shape": "circle", "diameter": 30, "center": [10, 0]}}]}
    p = resolve_part(PartSpec.model_validate(spec))
    plan = plan_part_drawing(DrawingSpec(model="B"), p)
    texts = {d.text for d in plan.dims}
    assert "Φ30 高15" in texts
    assert {"90", "60"} <= {d.text for d in plan.dims if d.kind == "ordinate"}  # 圆心距左下角
    svg, out = render(plan, p)
    assert out["crossings"] == [] and out["collisions"] == []
    import re
    sc = next(v for v in plan.views if v.type == "front").scale
    sizes = set()
    for pts in re.findall(r"<polygon points='([^']*)'", svg):
        q = [tuple(map(float, xy.split(","))) for xy in pts.split()]
        xs, ys = [a for a, _ in q], [b for _, b in q]
        sizes.add((round(max(xs) - min(xs), 1), round(max(ys) - min(ys), 1)))
    # 主视图外形：板 160×10 + 凸台 30×15（不是 160×25 的整块包围盒）
    assert (round(160 * sc, 1), round(10 * sc, 1)) in sizes and (round(30 * sc, 1), round(15 * sc, 1)) in sizes
    assert (round(160 * sc, 1), round(25 * sc, 1)) not in sizes
