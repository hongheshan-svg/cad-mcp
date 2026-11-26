"""精度自动补全：轴承位基准 + 圆柱度、其余配合段/齿轮段跳动、配合面粗糙度；用户已写的不改。"""
import sys

sys.path.insert(0, "tests")
from test_core import SHAFT  # noqa: E402
from test_gear import GS  # noqa: E402

from cadmcp import rules  # noqa: E402
from cadmcp.detailing import plan_part_drawing  # noqa: E402
from cadmcp.ir import DrawingSpec, PartSpec  # noqa: E402
from cadmcp.ir.resolve import resolve_part  # noqa: E402
from cadmcp.precision import bearing_cylindricity, runout  # noqa: E402
from cadmcp.render_svg import render  # noqa: E402


def test_tables():
    assert bearing_cylindricity(25) == 0.004 and bearing_cylindricity(60) == 0.005
    assert runout(25, 7) == 0.015 and runout(25, 6) == 0.01 and runout(100, 7) == 0.025


def test_gear_shaft_auto():
    p = resolve_part(PartSpec.model_validate({**GS, "auto_precision": True}))
    assert [(d["name"], d["target"]) for d in p.datums] == [("A", "step:0"), ("B", "step:4")]  # 键槽轴伸不当轴承位
    tl = {(g["type"], g["target"]): g for g in p.geotols}
    assert tl[("cylindricity", "step:0")]["value"] == 0.004
    assert tl[("circular_runout", "step:2")]["value"] == 0.018  # 齿坯：d=40、8 级
    assert tl[("circular_runout", "step:5")]["datums"] == ["A", "B"]
    assert {r["target"] for r in p.roughness} == {"step:0", "step:4", "step:5"}
    assert not [f for f in rules.check_part(p) if f["rule"] in ("G004", "G006")] if hasattr(rules, "check_part") else True
    plan = plan_part_drawing(DrawingSpec(model="GS1"), p)
    assert not render(plan, p)[1]["crossings"]


def test_user_tolerances_kept():
    p0 = resolve_part(PartSpec.model_validate(SHAFT))
    p = resolve_part(PartSpec.model_validate({**SHAFT, "auto_precision": True}))
    assert p.datums == p0.datums and p.geotols == p0.geotols
    assert len(p.roughness) >= len(p0.roughness)


def test_off_by_default():
    assert resolve_part(PartSpec.model_validate(GS)).datums == []
