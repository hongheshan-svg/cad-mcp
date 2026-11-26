"""套类/空心轴：内孔（通孔、盲孔、沉孔）建模、主视图原位全剖、内孔尺寸与偏差。"""
import pytest

from cadmcp.adapters.creo.compile import compile_part_tk as creo_part
from cadmcp.adapters.creo.drawing import tk_detail_ops
from cadmcp.adapters.solidworks.compile import compile_drawing, compile_part
from cadmcp.detailing import bore_dia_place, plan_part_drawing, shaft_dia_z
from cadmcp.errors import CadError
from cadmcp.ir import DrawingSpec, PartSpec
from cadmcp.ir.resolve import resolve_part
from cadmcp.render_svg import render

SL = {"name": "SL1", "part_no": "SL-01", "title": "轴套", "material": "45#",
      "base": {"type": "shaft", "chamfer": 1, "steps": [
          {"length": 12, "diameter": 60}, {"length": 48, "diameter": 45, "tolerance": "n6"}],
          "bores": [{"diameter": 30, "tolerance": "H7"}, {"diameter": 36, "length": 8, "side": "start"}]}}


def _p(spec=SL):
    return resolve_part(PartSpec.model_validate(spec))


def test_resolve_bores_and_mass():
    p = _p()
    assert [(b["z0"], b["z1"], b["through"]) for b in p.shaft_bores] == [(0, 60, True), (0, 8, False)]
    solid = _p({**SL, "base": {**SL["base"], "bores": []}})
    assert p.volume_mm3 < solid.volume_mm3


@pytest.mark.parametrize("bores,msg", [
    ([{"diameter": 44}], "壁厚"),
    ([{"diameter": 20, "length": 40, "side": "start"}, {"diameter": 25, "length": 30, "side": "end"}], "重叠"),
])
def test_bad_bores(bores, msg):
    with pytest.raises(CadError, match=msg):
        _p({**SL, "base": {**SL["base"], "bores": bores}})


def test_bores_with_keyway_unsupported():
    with pytest.raises(CadError, match="暂不支持"):
        _p({**SL, "features": [{"feature": "keyway", "name": "KW", "step": 1, "length": 28}]})


def test_drawing_full_section_and_dims():
    p = _p()
    plan = plan_part_drawing(DrawingSpec(model="SL1"), p)
    texts = [d.text for d in plan.dims]
    assert "Φ30H7(+0.021/0)" in texts and "Φ36" in texts and "8" in texts
    assert plan.views[0].full_section and plan.views[0].full_section["depth"] == 30
    svg, out = render(plan, p)
    assert "hollowU" in svg and not out["crossings"] and not out["collisions"]
    # 外径尺寸线放在实壁较厚处，内孔尺寸线不与之重叠
    assert 8 <= shaft_dia_z(p, 0) <= 12
    assert abs(bore_dia_place(p, 1, plan.scale) - shaft_dia_z(p, 0)) * plan.scale >= 4
    # 原生：剖视中端面/孔底选边点在有线的位置
    depth = next(d for d in plan.dims if d.ref == "BORE2_depth")
    (x0, y0), (x1, y1) = depth.extra["sheet_pts"]
    cy = plan.views[0].origin_on_sheet[1]
    assert abs(y0 - cy) > 18 * plan.scale and abs(y1 - cy) < 18 * plan.scale


def test_native_ops():
    spec = PartSpec.model_validate(SL)
    p = resolve_part(spec)
    sw = compile_part(spec, p)
    cuts = [o for o in sw if o.op == "keyway_cut" and o.args["feature"].startswith("BORE")]
    assert [(o.args["start_offset"], o.args["depth"]) for o in cuts] == [(0, 60), (0, 8)]
    plan = plan_part_drawing(DrawingSpec(model="SL1"), p)
    ops = compile_drawing(plan, "SL1.SLDPRT")
    bo = next(o for o in ops if o.op == "broken_out_section")
    assert bo.args["view"] == "front" and bo.args["depth"] == 30 and len(bo.args["points"]) == 4
    creo = creo_part(spec, p)
    assert sum(1 for o in creo if o.op == "tk.feat.extrude" and o.args.get("op") == "cut") == 2
    sec = [o for o in tk_detail_ops(plan) if o.op == "tk.drw.section"]
    assert sec and sec[0].args["kind"] == "local"


def test_bore_precision_targets_and_auto():
    from cadmcp import rules
    p0 = _p()
    assert any(f["rule"] == "G004" and f["target"] == "bore:0" for f in rules.check_part(p0))
    p = _p({**SL, "auto_precision": True})
    assert [(d["name"], d["target"]) for d in p.datums] == [("A", "bore:0")]
    assert [(g["type"], g["target"], g["datums"]) for g in p.geotols] == [("circular_runout", "step:1", ["A"])]
    assert {r["target"] for r in p.roughness} == {"bore:0", "step:1"}
    assert not any(f["rule"] == "G004" for f in rules.check_part(p))
    plan = plan_part_drawing(DrawingSpec(model="SL1"), p)
    dat = next(d for d in plan.dims if d.kind == "datum")
    zd = bore_dia_place(p, 0, plan.scale)
    assert dat.at == (zd, -15) and dat.extra["normal"] == (0, -1)  # 与孔径尺寸线对齐 → 以孔轴线为基准
    ra = next(d for d in plan.dims if d.ref == "ra:bore:0")
    assert ra.extra["normal"] == (0, 1)  # 立在下孔壁上（孔内）
    svg, out = render(plan, p)
    assert not out["crossings"] and not out["collisions"]


def test_bad_bore_target():
    with pytest.raises(CadError, match="invalid target"):
        _p({**SL, "datums": [{"name": "A", "target": "bore:5"}]})


def test_hub_keyway_sleeve():
    spec = {**SL, "base": {**SL["base"], "bores": [{"diameter": 30, "tolerance": "H7", "keyway": True},
                                                   {"diameter": 36, "length": 8, "side": "start"}]}}
    sp = PartSpec.model_validate(spec)
    p = resolve_part(sp)
    assert p.shaft_bores[0]["kw"] == {"name": "BORE1_KW", "width": 8.0, "t2": 3.3, "top": 18.3}
    plan = plan_part_drawing(DrawingSpec(model="SL1"), p)
    assert [v.type for v in plan.views] == ["front", "left"]
    texts = {d.ref: d.text for d in plan.dims}
    assert texts["BORE1_KW_w"] == "8JS9(+0.018/-0.018)" and texts["BORE1_KW_t"] == "33.3(+0.2/0)"
    assert next(d for d in plan.dims if d.ref == "bore0_dia").view == "left"  # 剖切面过键槽：孔径移到左视图
    svg, out = render(plan, p)
    assert not out["crossings"] and not out["collisions"]
    sw = compile_part(sp, p)
    kw = next(o for o in sw if o.op == "sketch_polyline_closed" and o.args["sketch"] == "SK_BORE1_KW")
    assert min(y for _, y in kw.args["points"]) == -18.3  # 键槽在 −Y（SW 主视图朝上）
    ops = compile_drawing(plan, "SL1.SLDPRT")
    assert any(o.op == "model_view" and o.args["view"] == "left" and o.args["sw_view"] == "*Back" for o in ops)
    assert {o.args["ref"] for o in ops if o.op == "shaft_dimension" and o.args["view"] == "left"} == {"BORE1_KW_w", "BORE1_KW_t"}
    assert any(o.op == "tk.feat.extrude" and o.args["feature"] == "BORE1_KW" for o in creo_part(sp, p))
    with pytest.raises(CadError, match="GB/T 1095"):
        resolve_part(PartSpec.model_validate({**SL, "base": {**SL["base"], "bores": [{"diameter": 30, "keyway": True, "keyway_width": 7}]}}))
