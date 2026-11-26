"""轴肩端面几何公差（shoulder:<i>）：目标校验、自动补注轴承定位轴肩端面圆跳动、框格联合排布无重叠穿线、原生附着点。"""
import sys

import pytest

sys.path.insert(0, "tests")
from test_gear import GS  # noqa: E402

from cadmcp.adapters.creo.drawing import tk_detail_ops  # noqa: E402
from cadmcp.adapters.solidworks.compile import compile_drawing  # noqa: E402
from cadmcp.detailing import plan_part_drawing, shaft_gtol_layout  # noqa: E402
from cadmcp.errors import CadError  # noqa: E402
from cadmcp.ir import DrawingSpec, PartSpec  # noqa: E402
from cadmcp.ir.resolve import resolve_part  # noqa: E402
from cadmcp.precision import bearing_shoulder_runout  # noqa: E402
from cadmcp.render_svg import render  # noqa: E402

SH = {"name": "SH1", "part_no": "SH-01", "title": "轴", "material": "45#",
      "base": {"type": "shaft", "chamfer": 1, "steps": [
          {"length": 25, "diameter": 30, "tolerance": "k6"}, {"length": 40, "diameter": 40},
          {"length": 25, "diameter": 30, "tolerance": "k6"}]},
      "datums": [{"name": "A", "target": "step:0"}, {"name": "B", "target": "step:2"}],
      "tolerances": [{"type": "circular_runout", "value": 0.012, "target": "shoulder:0", "datums": ["A", "B"]},
                     {"type": "circular_runout", "value": 0.012, "target": "shoulder:1", "datums": ["A", "B"]}]}


def test_shoulder_target_drawing_and_native():
    p = resolve_part(PartSpec.model_validate(SH))
    plan = plan_part_drawing(DrawingSpec(model="SH1"), p)
    g = [d for d in plan.dims if d.kind == "gtol"]
    assert len(g) == 2
    v = plan.views[0]
    for d in g:
        e = d.extra["elbow"]
        assert d.extra["sheet_at"] == (e["x_face"], e["y"])
        assert v.origin_on_sheet[1] + 15 * v.scale < e["y"] < v.origin_on_sheet[1] + 20 * v.scale  # 端面中部
    e0, e1 = g[0].extra["elbow"], g[1].extra["elbow"]
    assert e0["dir"] == 1 and e0["x_leader"] < e0["x_face"]      # 小端在左：自左向右指
    assert e1["dir"] == -1 and e1["x_leader"] > e1["x_face"]
    svg, out = render(plan, p)
    assert not out["crossings"] and not out["collisions"]
    ops = compile_drawing(plan, "SH1.SLDPRT")
    sw = [o for o in ops if o.op == "gtol"]
    assert len(sw) == 2 and {tuple(o.args["at"]) for o in sw} == {(e0["x_face"], e0["y"]), (e1["x_face"], e1["y"])}
    assert sum(1 for o in tk_detail_ops(plan) if o.op == "tk.drw.gtol") == 2


@pytest.mark.parametrize("tol", [
    {"type": "circular_runout", "value": 0.01, "target": "shoulder:2", "datums": ["A"]},       # 没有第 3、4 段之间
    {"type": "circular_runout", "value": 0.01, "target": "shoulder:x", "datums": ["A"]},
])
def test_bad_shoulder_target(tol):
    with pytest.raises(CadError, match="invalid target"):
        resolve_part(PartSpec.model_validate({**SH, "tolerances": [tol]}))


def test_shoulder_not_datum_or_equal_steps():
    with pytest.raises(CadError, match="invalid target"):
        resolve_part(PartSpec.model_validate({**SH, "datums": [{"name": "A", "target": "shoulder:0"}], "tolerances": []}))
    same = {**SH, "base": {**SH["base"], "steps": [{"length": 20, "diameter": 30}, {"length": 20, "diameter": 30}]},
            "datums": [], "tolerances": [{"type": "circular_runout", "value": 0.01, "target": "shoulder:0"}]}
    with pytest.raises(CadError, match="invalid target"):
        resolve_part(PartSpec.model_validate(same))


def test_auto_bearing_shoulders():
    p = resolve_part(PartSpec.model_validate({**GS, "auto_precision": True}))
    sh = {g["target"]: g for g in p.geotols if g["target"].startswith("shoulder:")}
    assert set(sh) == {"shoulder:0", "shoulder:3"}  # Φ25 轴承位 → 相邻 Φ30 定位轴肩
    assert sh["shoulder:0"]["value"] == bearing_shoulder_runout(25) and sh["shoulder:0"]["datums"] == ["A", "B"]
    plan = plan_part_drawing(DrawingSpec(model="GS1"), p)
    svg, out = render(plan, p)
    assert not out["crossings"] and not out["collisions"]
    lay = shaft_gtol_layout(p, plan.scale)
    assert len(lay) == 6 and max(r for _, r in lay.values()) <= 2


EC = {"name": "EC1", "part_no": "EC-01", "title": "轴承端盖", "material": "HT200", "auto_precision": True,
      "base": {"type": "shaft", "chamfer": 1, "steps": [
          {"length": 10, "diameter": 110}, {"length": 18, "diameter": 72, "tolerance": "f9"}],
          "flange_holes": [{"step": 0, "count": 6, "pcd": 92, "diameter": 9}]}}


def test_auto_flange_cover():
    """盘盖类：止口为基准 A，法兰定位端面对 A 的垂直度；箭头落在端面实体上（避开剖到的法兰孔）。"""
    from cadmcp.precision import perpendicularity
    p = resolve_part(PartSpec.model_validate(EC))
    assert [(d["name"], d["target"]) for d in p.datums] == [("A", "step:1")]
    assert [(g["type"], g["target"], g["datums"], g["value"]) for g in p.geotols] == \
        [("perpendicularity", "shoulder:0", ["A"], perpendicularity(110, 8))]
    assert {r["target"] for r in p.roughness} == {"step:1"}
    plan = plan_part_drawing(DrawingSpec(model="EC1"), p)
    g = next(d for d in plan.dims if d.kind == "gtol")
    cy = plan.views[0].origin_on_sheet[1]
    r = (g.extra["sheet_at"][1] - cy) / plan.views[0].scale
    assert 36 < r < 46 - 4.5  # 止口 Φ72 与孔（PCD 92、Φ9）之间
    svg, out = render(plan, p)
    assert not out["crossings"] and not out["collisions"]
    # 有配合内孔时仍按套类规则（内孔为基准）
    q = resolve_part(PartSpec.model_validate({**EC, "base": {**EC["base"], "bores": [{"diameter": 40, "tolerance": "H7"}]}}))
    assert [d["target"] for d in q.datums] == ["bore:0"]


def test_cast_material_notes_without_casting_spec():
    p = resolve_part(PartSpec.model_validate(EC))
    notes = plan_part_drawing(DrawingSpec(model="EC1"), p).notes
    assert any("人工时效" in x for x in notes) and any("砂眼" in x for x in notes)
    q = resolve_part(PartSpec.model_validate({**EC, "material": "45#"}))
    assert not any("砂眼" in x for x in plan_part_drawing(DrawingSpec(model="EC1"), q).notes)


def test_keyway_symmetry():
    """键槽对称度：auto_precision 补基准 C + ⌯；框格在断面图槽宽尺寸线左侧；原生按断面标签找视图。"""
    from cadmcp.precision import keyway_symmetry
    p = resolve_part(PartSpec.model_validate({**GS, "auto_precision": True}))
    sym = next(g for g in p.geotols if g["type"] == "symmetry")
    assert sym["target"] == "keyway:KW" and sym["datums"] == ["C"] and sym["value"] == keyway_symmetry(6)
    plan = plan_part_drawing(DrawingSpec(model="GS1"), p)
    d = next(d for d in plan.dims if d.kind == "gtol" and d.view == "section")
    sec = next(v for v in plan.views if v.type == "section")
    assert d.extra["section"] == sec.label and d.extra["sheet_at"][0] < sec.origin_on_sheet[0]
    svg, out = render(plan, p)
    assert not out["crossings"] and not out["collisions"]
    sw = next(o for o in compile_drawing(plan, "GS1.SLDPRT") if o.op == "gtol" and o.args["type"] == "symmetry")
    assert sw.args["view"] == f"section:{sec.label}"
    assert any(o.op == "tk.drw.gtol" and o.args["type"] == "symmetry" for o in tk_detail_ops(plan))
    with pytest.raises(CadError, match="invalid target"):
        resolve_part(PartSpec.model_validate({**GS, "datums": [{"name": "A", "target": "keyway:KW"}]}))
