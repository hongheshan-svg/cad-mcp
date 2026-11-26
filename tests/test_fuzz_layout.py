"""随机规格回归（fuzz 发现的排布冲突）：剖切符号字母与引线/尺寸文字、轴测图区不放文字。"""
import json

import pytest

from cadmcp.detailing import plan_part_drawing
from cadmcp.ir import DrawingSpec, PartSpec
from cadmcp.ir.resolve import resolve_part
from cadmcp.render_svg import render

SPECS = [
    '{"name": "F10", "part_no": "F-10", "title": "测试板", "material": "Q235", "base": {"type": "extrude", "profile": {"shape": "rect", "width": 100, "height": 200}, "thickness": 8}, "features": [{"feature": "hole", "name": "H0", "type": "tapped", "size": "M8", "positions": [[-25, 0], [25, 0]], "depth": 8}, {"feature": "hole", "name": "H1", "type": "counterbore", "size": "M4", "positions": [[-9, -23]]}]}',
    '{"name": "F90", "part_no": "F-90", "title": "测试板", "material": "Q235", "base": {"type": "extrude", "profile": {"shape": "rect", "width": 100, "height": 120}, "thickness": 30}, "features": [{"feature": "hole", "name": "H0", "type": "clearance", "size": "M10", "positions": [[-25, -35], [-25, 35], [25, -35], [25, 35]]}, {"feature": "hole", "name": "H1", "type": "counterbore", "size": "M4", "positions": [[13, 16]]}, {"feature": "hole", "name": "H2", "type": "tapped", "size": "M6", "positions": [[2, -4]], "depth": "through"}, {"feature": "hole", "name": "H3", "type": "simple", "size": 8, "positions": [[-25, 0], [25, 0]], "tolerance": "H7"}]}',
    '{"name": "F134", "part_no": "F-134", "title": "测试板", "material": "Q235", "base": {"type": "extrude", "profile": {"shape": "rect", "width": 300, "height": 60}, "thickness": 10}, "features": [{"feature": "hole", "name": "H0", "type": "countersink", "size": "M6", "positions": [[-135, 0], [135, 0]]}, {"feature": "hole", "name": "H1", "type": "countersink", "size": "M4", "positions": [[56, 12]]}, {"feature": "hole", "name": "H2", "type": "tapped", "size": "M8", "positions": [[-101, 2]], "depth": "through"}, {"feature": "pocket", "name": "P", "face": "top", "depth": 5, "profile": {"shape": "rect", "width": 75, "height": 15, "center": [0, 0], "corner_radius": 3}}]}',
    '{"name": "F217", "part_no": "F-217", "title": "测试板", "material": "Q235", "base": {"type": "extrude", "profile": {"shape": "rect", "width": 80, "height": 200}, "thickness": 15}, "features": [{"feature": "hole", "name": "H0", "type": "clearance", "size": "M10", "positions": [[-5, -70]]}, {"feature": "hole", "name": "H1", "type": "countersink", "size": "M4", "positions": [[7, 64]]}, {"feature": "hole", "name": "H2", "type": "simple", "size": 10, "positions": [[-20, 0], [20, 0]], "tolerance": "H7"}]}',
]


@pytest.mark.parametrize("js", SPECS)
def test_fuzz_layout_clean(js):
    p = resolve_part(PartSpec.model_validate(json.loads(js)))
    plan = plan_part_drawing(DrawingSpec(model=p.name), p)
    _, out = render(plan, p)
    assert out["crossings"] == [] and out["collisions"] == []
