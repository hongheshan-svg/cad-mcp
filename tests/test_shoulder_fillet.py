"""轴肩圆角：每个轴肩小端根部倒圆（紧靠环槽处不倒），技术要求注"未注轴肩圆角"，SW/Creo 原生倒圆。"""
import pytest

from cadmcp.adapters.creo.compile import compile_part_tk
from cadmcp.adapters.solidworks.compile import compile_part
from cadmcp.detailing import plan_part_drawing
from cadmcp.errors import CadError
from cadmcp.ir import DrawingSpec, PartSpec
from cadmcp.ir.resolve import resolve_part

SF = {"name": "SF1", "part_no": "SF-01", "title": "轴", "material": "45#",
      "base": {"type": "shaft", "chamfer": 1, "shoulder_fillet": 1, "steps": [
          {"length": 30, "diameter": 25, "tolerance": "k6"}, {"length": 50, "diameter": 34},
          {"length": 40, "diameter": 30}, {"length": 40, "diameter": 30}, {"length": 30, "diameter": 25}]},
      "features": [{"feature": "groove", "name": "G1", "type": "grinding", "step": 0}]}


def test_fillets():
    sp = PartSpec.model_validate(SF)
    p = resolve_part(sp)
    # 轴肩 z=30 有越程槽 → 不倒；z=80（Φ34→Φ30，小端 2）、z=160（Φ30→Φ25，小端 4）；z=120 等径不是轴肩
    assert [(f["z"], f["r"], f["step"]) for f in p.shoulder_fillets] == [(80, 15, 2), (160, 12.5, 4)]
    plan = plan_part_drawing(DrawingSpec(model="SF1"), p)
    assert any(n.endswith("未注轴肩圆角 R1。") for n in plan.notes)
    sw = compile_part(sp, p)
    sel = next(o for o in sw if o.op == "select_edges_at")
    assert sel.args["points"] == [[15, 0, 80], [12.5, 0, 160]]
    assert any(o.op == "fillet" and o.args["size"] == 1 for o in sw)
    rd = next(o for o in compile_part_tk(sp, p) if o.op == "tk.feat.round")
    assert rd.args["edges"] == [[15, 0, 80], [12.5, 0, 160]] and rd.args["radius"] == 1


def test_too_big():
    with pytest.raises(CadError, match="太大"):
        resolve_part(PartSpec.model_validate({**SF, "base": {**SF["base"], "shoulder_fillet": 2}}))
