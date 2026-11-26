"""轴外螺纹（GB/T 193/196/4459.1）：螺纹代号代替Φ标在大径上、螺纹长度尺寸、预览小径细线、SW/Creo 装饰螺纹。"""
import pytest

from cadmcp.adapters.creo.compile import compile_part_tk
from cadmcp.adapters.solidworks.compile import compile_part
from cadmcp.detailing import plan_part_drawing
from cadmcp.ir import DrawingSpec, PartSpec
from cadmcp.ir.resolve import resolve_part
from cadmcp.render_svg import render

SH2 = {"name": "SH2", "part_no": "SH-02", "title": "螺纹轴", "material": "45#",
       "base": {"type": "shaft", "chamfer": 1, "steps": [
           {"length": 20, "diameter": 16}, {"length": 50, "diameter": 25, "tolerance": "k6"},
           {"length": 10, "diameter": 30}, {"length": 30, "diameter": 20}]},
       "features": [{"feature": "thread", "name": "T1", "step": 0, "size": "M16x1.5"},
                    {"feature": "thread", "name": "T2", "step": 3, "size": "M20", "length": 16},
                    {"feature": "groove", "name": "U1", "type": "undercut", "step": 3, "position": 12,
                     "width": 2.5, "depth": 1.5},
                    {"feature": "keyway", "name": "KW", "step": 1, "length": 36}]}


def _p(spec=SH2):
    s = PartSpec.model_validate(spec)
    return s, resolve_part(s)


def test_resolve_thread_geometry():
    _, p = _p()
    th = {f["name"]: f for f in p.shaft_features if f["kind"] == "thread"}
    assert th["T1"]["callout"] == "M16×1.5-6g" and th["T1"]["full"] and th["T1"]["side"] == "start"
    assert th["T1"]["d1"] == pytest.approx(16 - 1.0825 * 1.5, abs=0.01)
    assert th["T2"]["callout"] == "M20-6g" and th["T2"]["pitch"] == 2.5 and th["T2"]["side"] == "end"
    assert th["T2"]["z1"] == pytest.approx(110) and th["T2"]["z0"] == pytest.approx(94)


def test_thread_diameter_must_match_step():
    bad = {**SH2, "features": [{"feature": "thread", "name": "T", "step": 1, "size": "M20"}]}
    with pytest.raises(Exception):
        _p(bad)


def test_drawing_uses_thread_code_and_length_dim():
    _, p = _p()
    plan = plan_part_drawing(DrawingSpec(model="SH2"), p)
    texts = [d.text for d in plan.dims if d.kind == "diameter"]
    assert "M16×1.5-6g" in texts and "M20-6g" in texts
    assert not any(t.startswith("Φ16") or t.startswith("Φ20") for t in texts)
    t2 = next(d for d in plan.dims if d.ref == "T2_len")
    assert t2.text == "16"
    assert not any(d.ref == "T1_len" for d in plan.dims)  # 全长螺纹不另注长度
    svg, out = render(plan, p)
    assert not out["crossings"]  # 短轴段上的长螺纹代号移到轮廓之外


def test_sw_and_creo_cosmetic_threads():
    s, p = _p()
    sw = [o for o in compile_part(s, p) if o.op == "cosmetic_thread"]
    assert {o.args["feature"] for o in sw} == {"T1", "T2"}
    assert all(o.args["external"] and o.args["tol"] == "6g" for o in sw)
    tk = [o for o in compile_part_tk(s, p) if o.op == "tk.feat.cosmetic_thread"]
    assert {o.args["feature"] for o in tk} == {"T1", "T2"}
    t2 = next(o for o in tk if o.args["feature"] == "T2")
    assert t2.args["major_diameter"] == 20 and t2.args["length"] == 16
    assert "edge_point" in t2.args and "start_edge_point" in t2.args
