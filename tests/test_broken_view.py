"""细长轴断开画法（GB/T 4458.1）：规划、预览、SW 原生操作、不支持的 CAD 退回全长。"""
from cadmcp.adapters.creo.adapter import CreoAdapter
from cadmcp.adapters.solidworks.compile import compile_drawing
from cadmcp.detailing import BREAK_GAP, plan_part_drawing, shaft_breaks, shaft_sx
from cadmcp.ir import DrawingSpec, PartSpec
from cadmcp.ir.resolve import resolve_part
from cadmcp.render_svg import render

LS = {"name": "LS1", "part_no": "LS-01", "title": "长轴", "material": "45#",
      "base": {"type": "shaft", "chamfer": 1, "steps": [
          {"length": 40, "diameter": 25, "tolerance": "k6"}, {"length": 12, "diameter": 32},
          {"length": 520, "diameter": 30}, {"length": 30, "diameter": 25, "tolerance": "k6"},
          {"length": 50, "diameter": 20, "tolerance": "k6"}]},
      "features": [{"feature": "keyway", "name": "KW", "step": 4, "length": 36},
                   {"feature": "keyway", "name": "KW2", "step": 0, "length": 28}],
      "datums": [{"name": "A", "target": "step:0"}, {"name": "B", "target": "step:3"}],
      "tolerances": [{"type": "circular_runout", "value": 0.012, "target": "step:4", "datums": ["A", "B"]}],
      "roughness": [{"target": "step:0", "ra": 0.8}, {"target": "step:3", "ra": 0.8}, {"target": "step:2", "ra": 3.2}]}


def _p():
    return resolve_part(PartSpec.model_validate(LS))


def test_breaks_only_long_plain_steps():
    p = _p()
    assert shaft_breaks(p) == [(82.0, 542.0)]  # 第 2 段 52–572，两端各留 30
    short = {**LS, "base": {**LS["base"], "steps": LS["base"]["steps"][:2] + [{"length": 60, "diameter": 30}]
                            + LS["base"]["steps"][3:]}}
    assert shaft_breaks(resolve_part(PartSpec.model_validate(short))) == []


def test_broken_plan_bigger_scale_and_true_length_dims():
    p = _p()
    full = plan_part_drawing(DrawingSpec(model="LS1", broken_view=False), p)
    brk = plan_part_drawing(DrawingSpec(model="LS1"), p)
    assert brk.scale > full.scale and brk.views[0].breaks
    assert any(d.text == "520" for d in brk.dims) and any(d.text == "652" for d in brk.dims)
    v = brk.views[0]
    L = 652
    assert abs(shaft_sx(v, L, 542) - shaft_sx(v, L, 82) - BREAK_GAP) < 1e-9
    assert abs(shaft_sx(v, None, 600) - shaft_sx(v, L, 600)) < 1e-9
    # 符号、直径尺寸线都不落在断开处
    xa, xb = shaft_sx(v, L, 82), shaft_sx(v, L, 542)
    for d in brk.dims:
        ex = d.extra or {}
        for x, _ in [ex.get("sheet_at", (0, 0))] + list(ex.get("sheet_pts") or []) if d.kind != "linear" else []:
            assert not xa - 0.5 < x < xb + 0.5, d.ref
    svg, out = render(brk, p)
    assert not out["crossings"] and not out["collisions"]
    assert "fill='white'" in svg  # 间隙遮盖


def test_sw_break_view_op_and_creo_falls_back():
    p = _p()
    plan = plan_part_drawing(DrawingSpec(model="LS1"), p)
    ops = compile_drawing(plan, "LS1.SLDPRT", model_features=p.shaft_features)
    names = [o.op for o in ops]
    i = names.index("break_view")
    assert names[i - 1] == "model_view" and ops[i].args["breaks"] == [[82.0, 542.0]]
    sec = [o for o in ops if o.op == "section_view"]
    v = plan.views[0]
    for o in sec:  # 剖切线在断开后的主视图上
        assert abs(o.args["points"][0][0] - shaft_sx(v, None, o.args["z"])) < 1e-3
    assert CreoAdapter.supports_broken_view is False
