"""渐开线直齿圆柱齿轮：几何（GB/T 1356）、公法线（GB/T 6443）、规定画法与参数表（GB/T 4459.2）、SW/Creo 出图。"""
import pytest

from cadmcp.detailing import plan_part_drawing
from cadmcp.gear import geometry, outline, polygon_area
from cadmcp.ir import DrawingSpec, PartSpec
from cadmcp.ir.resolve import resolve_part
from cadmcp.render_svg import render

GEAR = {"name": "GR1", "part_no": "GR-01", "title": "直齿圆柱齿轮", "material": "45#",
        "base": {"type": "extrude", "profile": {"shape": "circle", "diameter": 84}, "thickness": 20},
        "gear": {"module": 2, "teeth": 40},
        "features": [{"feature": "hole", "name": "Bore", "type": "simple", "size": 25, "tolerance": "H7",
                      "positions": [[0, 0]]},
                     {"feature": "pocket", "name": "KW", "face": "top", "depth": "through",
                      "profile": {"shape": "rect", "width": 8, "height": 5.8, "center": [0, 12.9]}}]}


def _p(spec=GEAR):
    return resolve_part(PartSpec.model_validate(spec))


@pytest.mark.parametrize("m,z,x,k,W", [(1, 20, 0, 3, 7.660), (2, 20, 0, 3, 15.321), (2, 40, 0, 5, 27.690),
                                       (3, 17, 0, 2, 13.998)])
def test_geometry_and_span(m, z, x, k, W):
    g = geometry(m, z, 20, x)
    assert g["d"] == m * z and g["da"] == pytest.approx(m * (z + 2)) and g["df"] == pytest.approx(m * (z - 2.5))
    assert g["k"] == k and g["Wk"] == pytest.approx(W, abs=0.002)


def test_outline_area_between_root_and_tip():
    import math
    pts = outline(2, 40)
    a = polygon_area(pts)
    assert math.pi * 37.5 ** 2 < a < math.pi * 42 ** 2
    assert max(math.hypot(*p) for p in pts) == pytest.approx(42, abs=1e-3)
    assert min(math.hypot(*p) for p in pts) == pytest.approx(37.5, abs=1e-3)  # 齿根圆 df = m(z − 2.5)


def test_resolve_tip_diameter_check_and_mass():
    p = _p()
    assert p.profile.shape == "polygon" and p.gear["da"] == 84
    assert 0.6 < p.mass_kg < 0.75
    bad = {**GEAR, "base": {**GEAR["base"], "profile": {"shape": "circle", "diameter": 80}}}
    with pytest.raises(Exception, match="tip diameter"):
        _p(bad)


def test_plan_gear_drawing():
    p = _p()
    plan = plan_part_drawing(DrawingSpec(model="GR1"), p)
    assert plan.layout == "gear" and [v.type for v in plan.views] == ["front", "left"]
    texts = {d.ref: d.text for d in plan.dims}
    assert texts["gear_da"] == "Φ84h11(0/-0.220)" and texts["gear_d"] == "Φ80" and texts["gear_b"] == "20"
    assert texts["gear_bore"] == "Φ25H7(+0.021/0)"
    assert texts["KW_w"] == "8JS9(+0.018/-0.018)" and texts["KW_t"] == "28.3(+0.2/0)"
    assert texts["gtol:circular_runout@tip"] == "↗ | 0.018 | A" and texts["datum:A"] == "A"  # d=80 ≤125、8 级
    rows = {r[0]: r[2] for r in plan.gear_table["rows"]}
    assert rows["模数"] == "2" and rows["齿数"] == "40" and rows["公法线长度"] == "27.690" and rows["跨齿数"] == "5"
    svg, out = render(plan, p)
    assert not out["crossings"] and not out["collisions"]
    assert "gear_table" in out["dim_refs"] and "gear_bore" in out["dim_refs"]
    assert svg.count("<polygon") < 60  # 规定画法：不画齿形


def test_sw_gear_drawing_ops():
    from cadmcp.adapters.solidworks.compile import compile_drawing
    plan = plan_part_drawing(DrawingSpec(model="GR1"), _p())
    ops = compile_drawing(plan, "GR1.SLDPRT")
    names = [o.op for o in ops]
    assert names.index("model_view") < names.index("section_cut")
    cut = next(o for o in ops if o.op == "section_cut")
    assert cut.args["parent"] == "left" and cut.args["points"][0][0] == cut.args["points"][1][0]
    dims = {o.args["ref"]: o for o in ops if o.op == "shaft_dimension"}
    assert set(dims) == {"gear_da", "gear_b", "KW_w", "KW_t"}
    assert any(o.op == "leader_note" and o.args["ref"] == "gear_bore" for o in ops)
    assert sum(o.op == "gtol" for o in ops) == 2 and any(o.op == "datum_tag" for o in ops)
    assert any(o.op == "warn" and "Φ80" in o.args["message"] for o in ops)
    tbl = next(o for o in ops if o.op == "table")
    assert tbl.args["columns"] == ["名称", "代号", "数值"] and len(tbl.args["rows"]) == 9


def test_creo_gear_drawing_ops():
    from cadmcp.adapters.creo.drawing import compile_drawing
    plan = plan_part_drawing(DrawingSpec(model="GR1"), _p())
    ops = compile_drawing(plan, "GR1.prt", toolkit=True)
    sec = next(o for o in ops if o.op == "tk.drw.section")
    assert sec.args["kind"] == "full" and sec.args["replaces"] == "V_FRONT"
    dims = [o for o in ops if o.op == "tk.drw.dim"]
    assert len(dims) == 4 and any(o.args.get("tol") == [0, -0.22] for o in dims)
    assert any(o.op == "tk.drw.table" and len(o.args["rows"]) == 9 for o in ops)
    assert any(o.op == "tk.drw.note" and o.args.get("leader_to") for o in ops)


def test_service_build_mock(svc):
    svc.change_begin("G")
    svc.build(PartSpec.model_validate(GEAR))
    r = svc.build(DrawingSpec.model_validate({"model": "GR1", "title_block": {"designer": "AI"}}))
    assert r["rules"]["passed"], r["rules"]


def test_creo_toolkit_part_uses_involute_polyline():
    from cadmcp.adapters.creo.compile import compile_part_tk
    s = PartSpec.model_validate(GEAR)
    ops = compile_part_tk(s, resolve_part(s))
    base = next(o for o in ops if o.op == "tk.feat.extrude" and o.args.get("feature") == "Base")
    assert len(base.args["entities"]) >= 40 * 10  # 每齿 ≥10 段


def test_blank_runout_table():
    from cadmcp.gear import blank_runout
    assert blank_runout(80, "8 GB/T 10095.1") == 0.018 and blank_runout(200, "7") == 0.022
    assert blank_runout(80, "6") == 0.011 and blank_runout(500, "9-8-8") == 0.050
    p = _p({**GEAR, "gear": {**GEAR["gear"], "blank_tolerances": False}})
    plan = plan_part_drawing(DrawingSpec(model="GR1"), p)
    assert not any(d.kind in ("gtol", "datum") for d in plan.dims)


GS = {"name": "GS1", "part_no": "GS-01", "title": "齿轮轴", "material": "40Cr",
      "base": {"type": "shaft", "chamfer": 1, "steps": [
          {"length": 30, "diameter": 25, "tolerance": "k6"}, {"length": 10, "diameter": 30},
          {"length": 36, "diameter": 44}, {"length": 10, "diameter": 30}, {"length": 20, "diameter": 25, "tolerance": "k6"},
          {"length": 40, "diameter": 20, "tolerance": "k6"}]},
      "features": [{"feature": "gear", "name": "G", "step": 2, "module": 2, "teeth": 20},
                   {"feature": "keyway", "name": "KW", "step": 5, "length": 28}]}


def test_gear_shaft_resolve_and_drawing():
    p = _p(GS)
    g = next(f for f in p.shaft_features if f["kind"] == "gear")
    assert (g["z0"], g["z1"], g["pd"], g["df"]) == (40, 76, 40, 35)
    plan = plan_part_drawing(DrawingSpec(model="GS1"), p)
    assert plan.layout == "shaft" and plan.sheet == "A3"
    texts = {d.ref: d.text for d in plan.dims}
    assert texts["step2_dia"] == "Φ44h11(0/-0.160)" and texts["ra:flank:G"] == "Ra 1.6"
    assert {r[0]: r[2] for r in plan.gear_table["rows"]}["公法线长度"] == "15.321"
    svg, out = render(plan, p)
    assert not out["crossings"] and not out["collisions"] and "gear_table" in out["dim_refs"]
    tx0 = plan.gear_table["origin"][0]
    main = plan.views[0]
    assert main.origin_on_sheet[0] + main.size_mm[0] / 2 < tx0  # 参数表不压主视图
    bad = {**GS, "base": {**GS["base"], "steps": [*GS["base"]["steps"][:2], {"length": 36, "diameter": 45},
                                                    *GS["base"]["steps"][3:]]}}
    with pytest.raises(Exception, match="tip diameter"):
        _p(bad)


def test_gear_shaft_native_ops():
    from cadmcp.adapters.creo.compile import compile_part_tk
    from cadmcp.adapters.solidworks.compile import compile_drawing, compile_part
    s = PartSpec.model_validate(GS)
    p = resolve_part(s)
    sw = compile_part(s, p)
    cut = next(o for o in sw if o.op == "keyway_cut" and o.args["feature"] == "G")
    assert cut.args["start_offset"] == 40 and cut.args["depth"] == 36
    assert any(o.op == "sketch_polyline_closed" and o.args["sketch"] == "SK_G" for o in sw)
    tk = next(o for o in compile_part_tk(s, p) if o.op == "tk.feat.extrude" and o.args.get("feature") == "G")
    assert tk.args["op"] == "cut" and tk.args["plane"] == {"ref": "TOP", "offset": 40}
    ops = compile_drawing(plan_part_drawing(DrawingSpec(model="GS1"), p), "GS1.SLDPRT")
    assert any(o.op == "table" and o.args["title"] == "齿轮参数表" for o in ops)


def test_shaft_fit_steps_without_geotol_warn():
    from cadmcp.rules import check_part
    p = _p(GS)
    g6 = [f for f in check_part(p) if f["rule"] == "G006"]
    assert g6 and g6[0]["severity"] == "warning" and "step:2" in g6[0]["target"]
    spec = {**GS, "datums": [{"name": "A", "target": "step:0"}, {"name": "B", "target": "step:4"}],
            "tolerances": [{"type": "circular_runout", "value": 0.012, "target": f"step:{i}", "datums": ["A", "B"]}
                           for i in (0, 2, 4, 5)]}
    assert not [f for f in check_part(_p(spec)) if f["rule"] == "G006"]


def test_crowded_gear_shaft_annotations_joint_layout():
    """拥挤轴段（基准 + 圆柱度 + Ra 同在 20 mm 段上）：框格联合排布、Ra 标在框格上方，无压字、不出图框。"""
    spec = {**GS, "datums": [{"name": "A", "target": "step:0"}, {"name": "B", "target": "step:4"}],
            "tolerances": [{"type": "circular_runout", "value": 0.012, "target": f"step:{i}", "datums": ["A", "B"]}
                           for i in (2, 5)] + [{"type": "cylindricity", "value": 0.005, "target": f"step:{i}"} for i in (0, 4)],
            "roughness": [{"target": f"step:{i}", "ra": 0.8} for i in (0, 4, 5)]}
    p = _p(spec)
    plan = plan_part_drawing(DrawingSpec(model="GS1"), p)
    svg, out = render(plan, p)
    assert not out["crossings"] and not out["collisions"]
    on_frame = [d for d in plan.dims if d.kind == "roughness" and (d.extra or {}).get("on_frame")]
    assert on_frame
    W, H = plan.sheet_size
    assert all(d.extra["sheet_at"][1] + 10 < H - 5 for d in on_frame)  # 不出图框
