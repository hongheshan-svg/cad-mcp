"""装配图配合尺寸：孔 H7 与轴 k6 同轴重叠 → Φ20H7/k6。"""
import tempfile

from cadmcp.detailing import plan_assembly_drawing
from cadmcp.gateway.service import CadService
from cadmcp.ir import AssemblySpec, DrawingSpec, PartSpec
from cadmcp.render_svg import render

PLATE = {"name": "FP", "part_no": "FP-01", "title": "座板", "material": "45#", "surface_treatment": "发黑",
         "base": {"type": "extrude", "profile": {"shape": "rect", "width": 100, "height": 60}, "thickness": 20},
         "features": [{"feature": "hole", "name": "Pin", "type": "simple", "size": 20, "tolerance": "H7",
                       "positions": [[0, 0]]}]}
PIN = {"name": "FPIN", "part_no": "FP-02", "title": "轴销", "material": "45#", "surface_treatment": "发黑",
       "base": {"type": "shaft", "chamfer": 1, "steps": [{"length": 40, "diameter": 20, "tolerance": "k6"},
                                                       {"length": 8, "diameter": 28}]}}
ASM = {"name": "FA", "part_no": "FA-00", "title": "销座组件",
       "components": [{"id": "plate", "source": "part:FP"},
                      {"id": "pin", "source": "part:FPIN", "place": {"offset": [0, 0, -20]}}]}


def _asm():
    svc = CadService.from_config(None, None, tempfile.mkdtemp())
    svc.change_begin("F")
    for d in (PLATE, PIN):
        svc.build(PartSpec.model_validate(d))
    svc.build(AssemblySpec.model_validate(ASM))
    return svc._models()["FA"].resolved


def test_fit_found_and_drawn():
    from cadmcp.ir.assembly_resolve import assembly_fits
    a = _asm()
    fits = assembly_fits(a)
    assert [(f["text"], f["inst_hole"], f["inst_shaft"]) for f in fits] == [("Φ20H7/k6", "plate", "pin")]
    assert abs(fits[0]["p0"][2] - fits[0]["p1"][2]) == 20  # 重叠段 = 板厚
    plan = plan_assembly_drawing(DrawingSpec(model="FA"), a)
    d = next(d for d in plan.dims if d.ref.startswith("fit:"))
    assert d.view == "front" and d.extra["orient"] == "h" and d.extra["suffix"] == "H7/k6"
    svg, out = render(plan, a)
    assert "Φ20H7/k6" in svg and out["crossings"] == [] and out["collisions"] == []
    from cadmcp.adapters.solidworks.compile import compile_drawing
    op = next(o for o in compile_drawing(plan, "FA.SLDASM") if o.args.get("ref", "").startswith("fit:"))
    assert op.args["prefix"] == "Φ" and op.args["suffix"] == "H7/k6"


def test_no_fit_when_offset():
    a_spec = {**ASM, "name": "FB", "components": [ASM["components"][0],
                                                {"id": "pin", "source": "part:FPIN", "place": {"offset": [10, 0, -20]}}]}
    svc = CadService.from_config(None, None, tempfile.mkdtemp())
    svc.change_begin("F")
    for d in (PLATE, PIN):
        svc.build(PartSpec.model_validate(d))
    svc.build(AssemblySpec.model_validate(a_spec))
    from cadmcp.ir.assembly_resolve import assembly_fits
    assert assembly_fits(svc._models()["FB"].resolved) == []


def test_fit_gap_rule():
    """销孔没写公差带：A005 警告，配合尺寸不标。"""
    from cadmcp.ir.assembly_resolve import assembly_fits, fit_gaps
    plate = {**PLATE, "name": "FP2", "features": [{**PLATE["features"][0], "tolerance": None}]}
    a_spec = {**ASM, "name": "FC", "components": [{"id": "plate", "source": "part:FP2"}, ASM["components"][1]]}
    svc = CadService.from_config(None, None, tempfile.mkdtemp())
    svc.change_begin("F")
    for d in (plate, PIN):
        svc.build(PartSpec.model_validate(d))
    r = svc.build(AssemblySpec.model_validate(a_spec))
    a = svc._models()["FC"].resolved
    assert assembly_fits(a) == []
    g = fit_gaps(a)
    assert len(g) == 1 and "plate.Pin[0]（孔）" in g[0]["message"]
    assert any(f["rule"] == "A005" for f in r["rules"]["findings"])


def test_bearing_fits_one_side():
    """滚动轴承：轴颈注 Φ25k6、座孔注 Φ52H7（轴承一侧不注）；座孔没写公差带 → A005。"""
    from cadmcp.ir.assembly_resolve import assembly_fits, fit_gaps
    shaft = {"name": "BS", "part_no": "BS-01", "title": "轴", "material": "45#", "surface_treatment": "发黑",
             "base": {"type": "shaft", "chamfer": 1, "steps": [{"length": 30, "diameter": 25, "tolerance": "k6"},
                                                             {"length": 10, "diameter": 32}]}}
    housing = {"name": "BH", "part_no": "BH-01", "title": "轴承座板", "material": "HT200", "surface_treatment": "喷漆",
               "base": {"type": "extrude", "profile": {"shape": "rect", "width": 120, "height": 100}, "thickness": 15},
               "features": [{"feature": "hole", "name": "Seat", "type": "simple", "size": 52, "tolerance": "H7",
                             "positions": [[0, 0]]}]}
    asm = {"name": "BA", "part_no": "BA-00", "title": "轴承组件",
           "components": [{"id": "shaft", "source": "part:BS"},
                          {"id": "brg", "source": "lib:BRG-6205", "place": {"offset": [0, 0, 5]}},
                          {"id": "seat", "source": "part:BH", "place": {"offset": [0, 0, 5]}}]}
    svc = CadService.from_config(None, None, tempfile.mkdtemp())
    svc.change_begin("B")
    for d in (shaft, housing):
        svc.build(PartSpec.model_validate(d))
    svc.build(AssemblySpec.model_validate(asm))
    a = svc._models()["BA"].resolved
    assert sorted(f["text"] for f in assembly_fits(a)) == ["Φ25k6", "Φ52H7"]
    assert fit_gaps(a) == []
    plan = plan_assembly_drawing(DrawingSpec(model="BA"), a)
    assert sorted(d.text for d in plan.dims if d.ref.startswith("fit:")) == ["Φ25k6", "Φ52H7"]
    _, out = render(plan, a)
    assert out["crossings"] == [] and out["collisions"] == []  # 两个配合挤在同一段：上下错开，序号让开
    # 座孔没写公差带
    h2 = {**housing, "name": "BH2", "features": [{**housing["features"][0], "tolerance": None}]}
    svc.build(PartSpec.model_validate(h2))
    svc.build(AssemblySpec.model_validate({**asm, "name": "BA2", "components": asm["components"][:2] + [
        {"id": "seat", "source": "part:BH2", "place": {"offset": [0, 0, 5]}}]}))
    g = fit_gaps(svc._models()["BA2"].resolved)
    assert len(g) == 1 and "seat.Seat[0]（孔）" in g[0]["message"]


def test_asm_view_draws_revolve_steps():
    """装配视图：轴按段画阶梯轮廓（Φ25 段与 Φ32 轴肩各一个矩形），端视为同心圆，被更近的大圆盖住的不画。"""
    from cadmcp.ir.assembly_resolve import solid_boxes
    a = _asm()
    pin = next(i for i in a.instances if i.id == "pin")
    assert [round(b.xmax - b.xmin) for b, _ in solid_boxes(pin)] == [20, 28]
    plan = plan_assembly_drawing(DrawingSpec(model="FA"), a)
    svg, _ = render(plan, a)
    assert svg.count("<circle") >= 2  # 俯视：销头 Φ28 外圆（Φ20 被挡）+ 序号圈


def test_asm_full_section():
    """装配主视图沿轴线全剖：销不剖（exclude），座板剖面区挖去销孔；剖切面过对称面不标注；SW 剖视替代主视图。"""
    from cadmcp.detailing import asm_section_regions
    a = _asm()
    plan = plan_assembly_drawing(DrawingSpec(model="FA"), a)
    sec = plan.asm_section
    assert sec["exclude"] == ["pin"] and list(sec["hatch"]) == ["plate"] and sec["label"] == ""
    regs = {r["id"]: r for r in asm_section_regions(a, plan.views[0], sec)}
    assert regs["pin"]["state"] == "excluded" and regs["plate"]["state"] == "cut"
    xs = sorted({c[0] for c in regs["plate"]["cells"]} | {c[2] for c in regs["plate"]["cells"]})
    assert len(regs["plate"]["cells"]) == 2 and round(xs[2] - xs[1], 3) == round(20 * plan.scale, 3)  # 中间挖去 Φ20
    from cadmcp.adapters.solidworks.compile import compile_drawing
    ops = compile_drawing(plan, "FA.SLDASM")
    cut = next(o for o in ops if o.op == "section_cut")
    assert cut.args["exclude_components"] == ["pin"]
    assert not any(o.op == "model_view" and o.args["view"] == "front" for o in ops)
    from cadmcp.adapters.creo.drawing import tk_detail_ops
    assert any(o.op == "tk.drw.section" and o.args.get("exclude_components") == ["pin"] for o in tk_detail_ops(plan))
