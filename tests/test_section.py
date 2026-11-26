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
    assert s.kind == "step" and set(s.features) == {"Fix", "Motor", "Pilot", "Relief"}
    assert len(s.path) == 3                                   # 每组只剖一个实例，转折最少
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
    assert sc.args["parent"] == "top" and len(sc.args["points"]) == 6 and sc.args["label"] == "A"
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
