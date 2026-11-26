import math

import pytest
from motor_plate import MP

from cadmcp import section as sx
from cadmcp.adapters.solidworks.compile import compile_drawing
from cadmcp.ir import DrawingSpec, PartSpec
from cadmcp.ir.resolve import resolve_part


def _part(**kw):
    return resolve_part(PartSpec.model_validate({**MP, **kw}))


def test_auto_step_section_covers_all_internal_features():
    p = _part()
    s = sx.auto_section(p)
    # 电机螺纹孔（PCD70）在 H 上落在 Φ50 止口孔内：转到它的转折点必在止口孔里（GB/T 4458.6 不允许），舍去
    assert s.kind == "step" and set(s.features) == {"Fix", "Pilot", "Relief"}
    assert len(s.path) == 2                                   # 每组只剖一个实例，转折最少
    assert not -25 - 1 < s.path[0][1] < 25 + 1                # 转折点不在止口孔内
    assert s.path[0][0] == p.bbox.xmin and s.path[-1][1] == p.bbox.xmax
    assert all(a[1] == b[0] for a, b in zip(s.path, s.path[1:]))   # 连续


def test_auto_full_section_when_one_plane_suffices():
    feats = [f for f in MP["features"] if f["name"] in ("Pilot", "Fix")]
    feats[1] = {**feats[1], "positions": [[-110, 0], [110, 0]]}
    s = sx.auto_section(_part(features=feats))
    assert s.kind == "full" and s.path == [(-125.0, 125.0, 0.0)]


def test_no_internal_features_no_section():
    p = _part(features=[{"feature": "hole", "name": "H", "type": "simple", "size": 10, "positions": [[0, 0]]}])
    assert sx.auto_section(p) is None


def test_counterbore_void_shape():
    feats = [{"feature": "hole", "name": "Fix", "type": "counterbore", "size": "M6", "positions": [[0, 0]]}]
    p = _part(features=feats)
    s = sx.auto_section(p)
    g = sx.geometry(p, s)
    void = g["voids"][0]
    xs_top = sorted(q[0] for q in void if abs(q[2] - 12) < 1e-6)
    xs_bot = sorted(q[0] for q in void if abs(q[2]) < 1e-6)
    assert xs_top == pytest.approx([-5.5, 5.5]) and xs_bot == pytest.approx([-3.3, 3.3])  # 沉孔 Φ11 / 通孔 Φ6.6
    assert len(g["openings"]) == 2 and len(g["edges"]) == 2      # 通孔：上下开口、两侧孔壁
    assert g["axes"]


def test_tapped_blind_hole_has_thread_lines_and_tip():
    feats = [{"feature": "hole", "name": "T", "type": "tapped", "size": "M8", "depth": 10, "thread_depth": 8,
              "positions": [[0, 0]]}]
    p = _part(features=feats)
    g = sx.geometry(p, sx.auto_section(p))
    assert len(g["thin"]) == 2 and all(abs(math.dist(*[(q[0], q[2]) for q in seg]) - 8) < 1e-6 for seg in g["thin"])
    assert len(g["thick"]) == 1                                   # 螺纹终止线
    assert min(q[2] for q in g["voids"][0]) < 12 - 10             # 钻尖低于孔深
    assert len(g["openings"]) == 1


def test_drawing_section_auto_and_sw_ops(svc):
    svc.change_begin("MP")
    svc.build(PartSpec.model_validate(MP))
    d = svc.build(DrawingSpec.model_validate({"model": MP["name"], "title_block": {"designer": "AI"}}))
    assert not [f for f in d["rules"]["findings"] if f["rule"] in ("D015", "D016")]
    plan = svc._get(MP["name"] + "_DRW").resolved
    assert plan.sections and next(v for v in plan.views if v.type == "front").label == "A-A"
    ops = compile_drawing(plan, "x.SLDPRT")
    assert not any(o.op == "model_view" and o.args["view"] == "front" for o in ops)
    sc = next(o for o in ops if o.op == "section_cut")
    assert sc.args["parent"] == "top" and len(sc.args["points"]) == 4 and sc.args["label"] == "A"
    names = [o.op for o in ops]
    assert names.index("section_cut") > max(i for i, o in enumerate(ops) if o.op == "model_view")


def test_section_none_warns_and_explicit_half_left(svc):
    svc.change_begin("MP")
    svc.build(PartSpec.model_validate(MP))
    d = svc.build(DrawingSpec.model_validate({"model": MP["name"], "sections": "none"}))
    assert any(f["rule"] == "D015" for f in d["rules"]["findings"])
    d = svc.build(DrawingSpec.model_validate({"model": MP["name"], "sections": [
        {"kind": "half", "through": ["Pilot"]}, {"kind": "full", "view": "left", "through": ["Pilot"], "label": "B"}]}))
    plan = svc._get(MP["name"] + "_DRW").resolved
    half, left = plan.sections
    assert half.kind == "half" and half.half_from == 0 and half.geometry["outline"]
    assert all(min(q[0] for q in poly) >= -1e-6 for poly in half.geometry["solid"])  # 只剖右半
    assert left.view == "left" and left.path[0][2] == 0 and left.path[0][:2] == (-75.0, 75.0)
    assert {v.label for v in plan.views if v.type in ("front", "left")} == {"A-A", "B-B"}
    with pytest.raises(Exception):
        svc.build(DrawingSpec.model_validate({"model": MP["name"], "views": [{"type": "front"}],
                                              "sections": [{"kind": "full"}]}))


BAR = {"name": "LB-001_Bar", "part_no": "LB-001", "title": "压条", "material": "Q235",
       "base": {"type": "extrude", "profile": {"shape": "rect", "width": 240, "height": 40}, "thickness": 20},
       "features": [
           {"feature": "hole", "name": "M8", "type": "tapped", "size": "M8", "depth": 16, "thread_depth": 12,
            "positions": [[-90, 0]]},
           {"feature": "hole", "name": "Pass", "type": "simple", "size": 9, "positions": [[40, 0], [100, 0]]}]}


def test_auto_local_section_for_concentrated_features(svc):
    p = resolve_part(PartSpec.model_validate(BAR))
    s = sx.auto_section(p)
    assert s.kind == "local" and s.parent == "" and s.features == ["M8"]
    h0, h1, d = s.path[0]
    assert h0 < -90 - 4 and h1 > -90 + 4 and h1 - h0 < 0.35 * 240 and d == 0
    svc.change_begin("B")
    svc.build(PartSpec.model_validate(BAR))
    d_ = svc.build(DrawingSpec.model_validate({"model": BAR["name"], "title_block": {"designer": "AI"}}))
    assert not [f for f in d_["rules"]["findings"] if f["rule"] in ("D015", "D016")]
    plan = svc._get(BAR["name"] + "_DRW").resolved
    sec = plan.sections[0]
    assert sec.cut_points == [] and next(v for v in plan.views if v.type == "front").label in (None, "")
    g = sec.geometry
    assert len(g["breaks"]) == 2 and g["region"] and g["thin"] and g["thick"]   # 波浪线 + 螺纹线
    for w in g["breaks"]:                                                        # 波浪线在区间两端摆动
        hs = [q[0] for q in w]
        assert max(hs) - min(hs) > 0.5 * sec.wave_amp
    ops = compile_drawing(plan, "x.SLDPRT")
    assert any(o.op == "model_view" and o.args["view"] == "front" for o in ops)   # 视图保留
    bo = next(o for o in ops if o.op == "broken_out_section")
    assert bo.args["depth"] == pytest.approx(20.0) and len(bo.args["points"]) > 8   # 深度：前表面 y=-20 到 y=0


def test_explicit_local_section_needs_through(svc):
    svc.change_begin("B")
    svc.build(PartSpec.model_validate(BAR))
    svc.build(DrawingSpec.model_validate({"model": BAR["name"], "sections": [{"kind": "local", "through": ["Pass"]}]}))
    plan = svc._get(BAR["name"] + "_DRW").resolved
    assert plan.sections[0].kind == "local" and plan.sections[0].path[0][0] < 40 < plan.sections[0].path[0][1]
    with pytest.raises(Exception):
        svc.build(DrawingSpec.model_validate({"model": BAR["name"], "sections": [{"kind": "local"}]}))


def test_step_turn_inside_feature_falls_back_to_full():
    """法兰：销孔与中心孔 H 相同，阶梯剖转折只能落在孔内 → 过中心全剖（中心孔 + 两螺栓孔）。"""
    fl = {"name": "FL1", "part_no": "FL-01", "title": "法兰盘", "material": "45#",
          "base": {"type": "extrude", "profile": {"shape": "circle", "diameter": 160}, "thickness": 20},
          "features": [
              {"feature": "hole", "name": "Bore", "type": "simple", "size": 60, "tolerance": "H7", "positions": [[0, 0]]},
              {"feature": "hole", "name": "Bolt", "type": "counterbore", "size": "M10",
               "pattern": {"kind": "circular", "radius": 60, "count": 6, "start_angle": 0}},
              {"feature": "hole", "name": "Pin", "type": "simple", "size": 8, "tolerance": "H7", "positions": [[0, 45]]}]}
    s = sx.auto_section(resolve_part(PartSpec.model_validate(fl)))
    assert s.kind == "full" and s.path[0][2] == 0.0 and {"Bore", "Bolt"} <= set(s.features)


def test_section_outline_continuous_across_hole_mouths():
    """剖视图孔口处画剖切面后方可见的孔口棱线：板的上下轮廓线连续（GB/T 4458.6）。"""
    from cadmcp.render_svg import _draw_section, _Svg
    from cadmcp.section import auto_section, geometry
    p = resolve_part(PartSpec.model_validate({
        "name": "PL", "part_no": "PL", "title": "板", "material": "45#",
        "base": {"type": "extrude", "profile": {"shape": "rect", "width": 100, "height": 40}, "thickness": 10},
        "features": [{"feature": "hole", "name": "B", "type": "simple", "size": 20, "tolerance": "H7",
                      "positions": [[0, 0]]}]}))
    sec = auto_section(p)
    sec.geometry = geometry(p, sec)
    s = _Svg(420, 297)
    segs = []
    s.line = lambda x1, y1, x2, y2, w=0.25, dash=None: segs.append((x1, y1, x2, y2))
    _draw_section(s, sec, lambda q: (q[0], q[2]), None, 0, 0)
    for z in (0, 10):  # 上下表面在孔口 x ∈ [-10, 10] 处有线
        assert any(abs(a[1] - z) < 1e-6 and abs(a[3] - z) < 1e-6 and min(a[0], a[2]) < 0 < max(a[0], a[2]) for a in segs)


def test_full_section_hides_holes_in_front_of_cut():
    """全剖视图：剖切面前方被移去部分里的孔不画；剖到的孔只由剖面几何画截面（不再画螺纹 3/4 圆弧）。"""
    from cadmcp.render_svg import _hole_cut_by
    from cadmcp.section import SectionPlan

    def hole(depth):
        p = resolve_part(PartSpec.model_validate({
            "name": "BK", "part_no": "BK", "title": "块", "material": "45#",
            "base": {"type": "extrude", "profile": {"shape": "rect", "width": 100, "height": 60}, "thickness": 50},
            "features": [{"feature": "hole", "name": "P", "face": "front", "type": "tapped", "size": "M12",
                          "depth": depth, "thread_depth": min(20, depth - 4), "positions": [[0, 25]]}]}))
        return p.holes[0]
    sec = SectionPlan(label="A", kind="full", view="front", parent="top", path=[(-50.0, 50.0, 0.0)])
    for depth, cut in ((40, True), (12, False)):  # 前表面 y=-30，剖切面 y=0
        h = hole(depth)
        assert _hole_cut_by(sec, h, h.instances[0]) is cut


def test_front_face_hole_cut_at_mid_depth_not_on_surface():
    """前表面上的孔：自动剖切面取在孔深中点，不能落在零件表面上（剖了等于没剖）。"""
    from cadmcp.section import auto_section
    p = resolve_part(PartSpec.model_validate({
        "name": "BK", "part_no": "BK", "title": "块", "material": "45#",
        "base": {"type": "extrude", "profile": {"shape": "rect", "width": 100, "height": 60}, "thickness": 50},
        "features": [{"feature": "hole", "name": "P", "face": "front", "type": "tapped", "size": "M12",
                      "depth": 40, "thread_depth": 20, "positions": [[0, 25]]}]}))
    sec = auto_section(p)
    assert all(abs(d - p.bbox.ymin) > 1 for _, _, d in sec.path)
    assert any(abs(d - (p.bbox.ymin + 20)) < 1e-6 for _, _, d in sec.path)


def test_through_hole_walls_stop_at_cut_pocket():
    """通孔穿进被剖的型腔：型腔空腔内不画孔壁线。"""
    from cadmcp.section import auto_section, geometry
    p = resolve_part(PartSpec.model_validate({
        "name": "PK", "part_no": "PK", "title": "座", "material": "45#",
        "base": {"type": "extrude", "profile": {"shape": "rect", "width": 200, "height": 100}, "thickness": 30},
        "features": [{"feature": "hole", "name": "B", "type": "simple", "size": 40, "tolerance": "H7", "positions": [[0, 0]]},
                     {"feature": "pocket", "name": "R", "face": "bottom", "depth": 10,
                      "profile": {"shape": "rect", "width": 100, "height": 50, "center": [0, 0]}}]}))
    sec = auto_section(p)
    g = geometry(p, sec)
    zlo = p.bbox.zmin
    for pl in g["edges"]:
        for a, b in zip(pl, pl[1:]):
            for k in range(1, 20):  # 沿线段采样：不得有点落在型腔空腔内部
                x, z = a[0] + (b[0] - a[0]) * k / 20, a[2] + (b[2] - a[2]) * k / 20
                assert not (-50 + 1e-6 < x < 50 - 1e-6 and zlo + 1e-6 < z < zlo + 10 - 1e-6), (a, b)


def test_hole_roughness_moves_to_section_wall():
    """大孔孔壁的表面结构：孔被剖开时标在剖视图孔壁上（符号朝孔腔内），俯视图孔旁不再放。"""
    from cadmcp.detailing import plan_part_drawing
    from cadmcp.ir import DrawingSpec
    from cadmcp.render_svg import render
    p = resolve_part(PartSpec.model_validate({
        "name": "FLx", "part_no": "FLx", "title": "板", "material": "45#",
        "base": {"type": "extrude", "profile": {"shape": "rect", "width": 160, "height": 100}, "thickness": 20},
        "features": [{"feature": "hole", "name": "Bore", "type": "simple", "size": 60, "tolerance": "H7", "positions": [[0, 0]]}],
        "roughness": [{"target": "hole:Bore", "ra": 0.8}]}))
    plan = plan_part_drawing(DrawingSpec(model="FLx"), p)
    ra = next(d for d in plan.dims if d.ref == "ra:hole:Bore")
    assert ra.view == "front" and ra.extra["normal"] == (1, 0) and ra.extra.get("in_section")
    assert abs(ra.at[0] - (-30)) < 1e-6 and 0 < ra.at[1] < 20
    _, out = render(plan, p)
    assert out["crossings"] == [] and out["collisions"] == []


def test_cut_line_extension_is_sheet_based():
    """剖切线伸出外形按图纸 6 mm 计（模型 6/比例），放大比例下不至于远离视图。"""
    from cadmcp.section import SectionPlan, cut_line
    p = resolve_part(PartSpec.model_validate({
        "name": "T", "part_no": "T", "title": "块", "material": "45#",
        "base": {"type": "extrude", "profile": {"shape": "rect", "width": 20, "height": 12}, "thickness": 8}}))
    sec = SectionPlan(label="A", kind="full", view="front", parent="top", path=[(-10.0, 10.0, 0.0)])
    for scale in (5.0, 1.0, 0.5):
        pts = cut_line(p, sec, scale)
        assert abs(pts[0][0] - (-10 - 6 / scale)) < 1e-9 and abs(pts[-1][0] - (10 + 6 / scale)) < 1e-9


def test_sw_center_marks_after_section_views():
    """SW 工程图：每个正投影视图插入孔/槽中心符号；剖视图替代的视图在剖视图建好之后插入。"""
    from cadmcp.adapters.solidworks.compile import compile_drawing
    from cadmcp.detailing import plan_part_drawing
    from cadmcp.ir import DrawingSpec
    p = resolve_part(PartSpec.model_validate({
        "name": "CM", "part_no": "CM", "title": "板", "material": "45#",
        "base": {"type": "extrude", "profile": {"shape": "rect", "width": 100, "height": 60}, "thickness": 10},
        "features": [{"feature": "hole", "name": "P", "type": "simple", "size": 8, "tolerance": "H7", "positions": [[0, 0]]}]}))
    plan = plan_part_drawing(DrawingSpec(model="CM"), p)
    ops = compile_drawing(plan, "CM.SLDPRT")
    names = [o.op for o in ops]
    cm = [o.args["view"] for o in ops if o.op == "center_marks"]
    assert "top" in cm and "front" in cm and "iso" not in cm
    if "section_cut" in names or "section_view" in names:
        last_sec = max(i for i, n in enumerate(names) if n in ("section_cut", "section_view"))
        assert names.index("center_marks") > last_sec
