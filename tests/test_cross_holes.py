"""轴上径向横孔（销孔/油孔/紧定螺钉孔）：校验、主视图画法与标注、SW/Creo 原生建模。"""
import pytest

from cadmcp.adapters.creo.compile import compile_part_tk as creo_part
from cadmcp.adapters.solidworks.compile import compile_drawing, compile_part
from cadmcp.detailing import plan_part_drawing, shaft_dia_z
from cadmcp.errors import CadError
from cadmcp.ir import DrawingSpec, PartSpec
from cadmcp.ir.resolve import resolve_part
from cadmcp.render_svg import render

XS = {"name": "XS1", "part_no": "XS-01", "title": "销轴", "material": "45#",
      "base": {"type": "shaft", "chamfer": 1, "steps": [{"length": 40, "diameter": 30, "tolerance": "f7"},
                                                       {"length": 60, "diameter": 25}],
               "cross_holes": [{"step": 0, "diameter": 6, "tolerance": "H7"},
                               {"step": 1, "position": 45, "size": "M6", "depth": 8}]},
      "features": [{"feature": "keyway", "name": "KW", "step": 1, "length": 25, "offset": 5}]}


def _p(spec=XS):
    return resolve_part(PartSpec.model_validate(spec))


def test_resolve_and_volume():
    p = _p()
    assert [(h["name"], h["z"], h["callout"]) for h in p.cross_holes] == [
        ("XH1", 20.0, "Φ6H7 通"), ("XH2", 85.0, "M6-6H↓8 孔↓11")]
    assert p.cross_holes[1]["hole"] == 5.0
    solid = _p({**XS, "base": {**XS["base"], "cross_holes": []}})
    assert solid.volume_mm3 - p.volume_mm3 > 3.14 * 9 * 25


@pytest.mark.parametrize("holes,msg", [
    ([{"step": 0, "diameter": 6, "size": "M6"}], "二选一|只能给一个"),
    ([{"step": 0, "diameter": 16}], "一半"),
    ([{"step": 0, "diameter": 6, "position": 3}], "端面"),
    ([{"step": 1, "diameter": 6, "position": 20}], "重叠"),           # 与键槽（z 45–70）重叠
    ([{"step": 0, "diameter": 6, "depth": 40}], "超过轴径"),
    ([{"step": 0, "diameter": 6, "position": 15}, {"step": 0, "diameter": 6, "position": 20}], "重叠"),
    ([{"step": 5, "diameter": 6}], "超出范围"),
])
def test_bad_cross_holes(holes, msg):
    with pytest.raises(CadError, match=msg):
        _p({**XS, "base": {**XS["base"], "cross_holes": holes}})


def test_sleeve_only_through():
    sl = {**XS, "features": [], "base": {**XS["base"], "bores": [{"diameter": 12}], "cross_holes": [{"step": 0, "diameter": 6}]}}
    assert _p(sl).cross_holes[0]["through"]
    with pytest.raises(CadError, match="只支持通孔"):
        _p({**sl, "base": {**sl["base"], "cross_holes": [{"step": 0, "diameter": 6, "depth": 5}]}})


def test_drawing():
    p = _p()
    plan = plan_part_drawing(DrawingSpec(model="XS1"), p)
    texts = {d.ref: d.text for d in plan.dims}
    assert texts["xh:XH1"] == "Φ6H7(+0.012/0) 通" and texts["XH1_pos"] == "20"  # 缺省带极限偏差
    assert texts["xh:XH2"] == "M6-6H↓8 孔↓11" and texts["XH2_pos"] == "15"  # 轴端段：从轴端面定位
    pos2 = next(d for d in plan.dims if d.ref == "XH2_pos")
    assert pos2.extra["track"] == 1  # 与键槽定位尺寸 30 重叠 → 低一道
    svg, out = render(plan, p)
    assert not out["crossings"] and not out["collisions"]
    assert abs(shaft_dia_z(p, 0, plan.scale) - 20) >= 3 + 1  # 直径尺寸线避开横孔
    plan2 = plan_part_drawing(DrawingSpec(model="XS1", fit_deviations=False), p)
    assert next(d.text for d in plan2.dims if d.ref == "xh:XH1") == "Φ6H7 通"
    # 原生：定位尺寸第二点在孔圆上；横孔标注是带引线注释
    (x0, y0), (x1, y1) = next(d for d in plan.dims if d.ref == "XH1_pos").extra["sheet_pts"]
    cy = plan.views[0].origin_on_sheet[1]
    assert y1 == pytest.approx(cy + 3 * plan.scale, abs=0.01)
    ops = compile_drawing(plan, "XS1.SLDPRT")
    assert {o.args["ref"] for o in ops if o.op == "leader_note"} >= {"xh:XH1", "xh:XH2"}


def test_native_ops():
    sp = PartSpec.model_validate(XS)
    p = resolve_part(sp)
    sw = compile_part(sp, p)
    cuts = {o.args["feature"]: o.args for o in sw if o.op == "keyway_cut" and o.args["feature"].startswith("XH")}
    assert cuts["XH1"]["flip"] is False and cuts["XH1_B"]["flip"] is True and cuts["XH1"]["depth"] == 16
    assert cuts["XH2"]["start_offset"] == 12.5 - 11 and cuts["XH2"]["depth"] == 12
    thr = next(o for o in sw if o.op == "cosmetic_thread" and o.args["feature"] == "XH2_THR")
    assert thr.args["edge_pts"][0][1:] == [2.5, 85.0] and thr.args["depth"] == 8
    tk = {o.args["feature"]: o.args for o in creo_part(sp, p) if o.args.get("feature", "").startswith("XH")}
    assert tk["XH1"]["plane"] == {"ref": "RIGHT", "offset": 16} and tk["XH1"]["depth"] == 32
    assert tk["XH2"]["direction"] == "normal" and "XH2_THR" in tk


def test_at_assembly_pin_hole():
    """配作销孔：标注加“配作”，不注定位尺寸，技术要求注明配钻铰；不报 S007。"""
    from cadmcp import rules
    sp = {**XS, "base": {**XS["base"], "cross_holes": [{"step": 0, "diameter": 6, "tolerance": "H7", "at_assembly": True}]}}
    p = _p(sp)
    plan = plan_part_drawing(DrawingSpec(model="XS1", fit_deviations=False), p)
    assert next(d.text for d in plan.dims if d.ref == "xh:XH1") == "Φ6H7 通 配作"
    assert not any(d.ref == "XH1_pos" for d in plan.dims)
    assert any("配钻铰" in n for n in plan.notes)
    assert not any(f["rule"] == "S007" for f in rules.check_part(p))
    svg, out = render(plan, p)
    assert not out["crossings"] and not out["collisions"]
