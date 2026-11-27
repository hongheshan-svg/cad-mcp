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
    assert not any(r[0] == "R014" for r in _rules([b, t([35, 25])], t=10))  # 板面上的孔自动从板面起算
    assert not any(r[0] == "R014" for r in _rules([b, t([35, 25], at=10)], t=10))
    two = {**t([35, 25]), "positions": [[35, 25], [0, 0]]}  # 同组孔一个在板面、一个在凸台顶：须拆组
    assert ("R014", "T[0]", "error") in _rules([b, two], t=10) or ("R014", "T[1]", "error") in _rules([b, two], t=10)
    assert not any(r[0] == "R014" for r in _rules([b, t([0, 0])], t=10))  # 凸台顶上的孔
    assert ("R014", "T[0]", "error") in _rules([b, t([15, 0])], t=10)  # 骑在凸台边上
    assert ("R014", "T[0]", "error") in _rules([b, t([0, 0], at=10)], t=10)  # 凸台上的孔从板面起算：埋在凸台里
    assert not any(r[0] == "R014" for r in _rules([b, t([0, 0], at=25)], t=10))


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


def test_boss_section_no_seam():
    """剖视图：凸台与板是同一实体，相接处不画线（剖面多边形共线重合的边不画）。"""
    from cadmcp.detailing import plan_part_drawing
    from cadmcp.ir import DrawingSpec
    from cadmcp.render_svg import render
    spec = {"name": "B", "part_no": "B-01", "title": "凸台板", "material": "Q235", "surface_treatment": "发黑",
            "base": {"type": "extrude", "profile": {"shape": "rect", "width": 200, "height": 120}, "thickness": 15},
            "features": [{"feature": "hole", "name": "H0", "type": "counterbore", "size": "M8", "at": 15,
                          "positions": [[-70, -40], [70, -40], [-70, 40], [70, 40]]},
                         {"feature": "hole", "name": "H1", "type": "tapped", "size": "M6", "depth": 12,
                          "positions": [[-15, 0], [15, 0]]},
                         {"feature": "boss", "name": "B", "height": 10,
                          "profile": {"shape": "rect", "width": 60, "height": 48, "center": [0, 0]}}]}
    p = resolve_part(PartSpec.model_validate(spec))
    plan = plan_part_drawing(DrawingSpec(model="B"), p)
    sec = next(x for x in plan.sections if x.kind in ("full", "step"))
    v = next(x for x in plan.views if x.type == sec.view)
    svg, out = render(plan, p)
    import re
    sc, (cx, cy) = v.scale, v.origin_on_sheet
    y_seam = 297 - (cy + (15 - 25 / 2) * sc)  # 板顶面（凸台底）在 SVG 中的 y
    x_lo, x_hi = cx - 25 * sc, cx + 25 * sc  # 凸台底边中段
    seam = [m for m in re.findall(r"<line x1='([\d.]+)' y1='([\d.]+)' x2='([\d.]+)' y2='([\d.]+)'[^>]*stroke-width='0.5", svg)
            if abs(float(m[1]) - y_seam) < 0.1 and abs(float(m[3]) - y_seam) < 0.1
            and min(float(m[0]), float(m[2])) < x_hi and max(float(m[0]), float(m[2])) > x_lo]
    assert seam == []


def test_boss_auto_surface():
    """有凸台：未写 at 的孔/型腔从所在处的实际表面起算（与 CAD 射线选面一致）。"""
    spec = {"name": "P", "part_no": "P-01", "title": "块", "material": "45#", "surface_treatment": "发黑",
            "base": {"type": "extrude", "profile": {"shape": "rect", "width": 160, "height": 100}, "thickness": 15},
            "features": [{"feature": "boss", "name": "B", "height": 10,
                          "profile": {"shape": "circle", "diameter": 30, "center": [-40, 0]}},
                         {"feature": "pocket", "name": "PK", "depth": 6,
                          "profile": {"shape": "rect", "width": 40, "height": 30, "center": [40, 0]}},
                         {"feature": "hole", "name": "T1", "type": "tapped", "size": "M6", "depth": 8,
                          "positions": [[60, 40], [-60, 40]]},
                         {"feature": "hole", "name": "T2", "type": "tapped", "size": "M6", "depth": 8,
                          "positions": [[-40, 0]]}]}
    p = resolve_part(PartSpec.model_validate(spec))
    assert p.pockets[0]["at"] == 15
    h = {x.name: x for x in p.holes}
    assert h["T1"].instances[0].xyz[2] == 15 and h["T2"].instances[0].xyz[2] == 25


def test_sw_boss_sketch_on_own_base():
    """SolidWorks：凸台草图射线打在凸台自己的位置、底面高度（第二个凸台不落到第一个凸台顶上）。"""
    from cadmcp.adapters.solidworks.compile import compile_part
    spec = PartSpec.model_validate({
        "name": "P", "part_no": "P-01", "title": "块", "material": "45#", "surface_treatment": "发黑",
        "base": {"type": "extrude", "profile": {"shape": "rect", "width": 160, "height": 100}, "thickness": 15},
        "features": [{"feature": "boss", "name": "B1", "height": 10,
                      "profile": {"shape": "circle", "diameter": 40, "center": [0, 0]}},
                     {"feature": "boss", "name": "B2", "height": 5,
                      "profile": {"shape": "rect", "width": 20, "height": 20, "center": [50, 20]}}]})
    ops = compile_part(spec, resolve_part(spec))
    sk = {o.args["sketch"]: o.args for o in ops if o.op == "sketch_on_face"}
    assert sk["SK_B1"]["plane_coord"] == 15 and sk["SK_B2"]["plane_coord"] == 15
    assert sk["SK_B2"]["ray_from"][:2] == (50, 20)
