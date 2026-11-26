import pytest

from cadmcp.ir import AssemblySpec, PartSpec
from cadmcp.ir.resolve import resolve_part

MOTOR_PLATE = {
    "name": "MP", "part_no": "MP-01", "title": "电机安装板", "material": "AL6061-T6", "surface_treatment": "阳极氧化",
    "base": {"type": "extrude", "profile": {"shape": "rect", "width": 120, "height": 100}, "thickness": 12},
    "features": [
        {"feature": "mount", "name": "Motor", "item": "SRV-60-400W", "center": [10, 0]},
        {"feature": "mount", "name": "Rail", "item": "LGR-15?L=400", "face": "bottom", "center": [-50, 30],
         "rotate": 0, "screw_hole": "tapped", "depth": 10, "thread_depth": 8},
    ],
}


def test_mount_expands_holes_and_cs():
    p = resolve_part(PartSpec.model_validate(dict(MOTOR_PLATE, features=MOTOR_PLATE["features"][:1])))
    names = {h.name: h for h in p.holes}
    assert names["Motor_Screw"].callout == "4×Φ5.5 通 均布"
    assert names["Motor_Pilot"].callout.startswith("Φ50H7")
    assert names["Motor_Screw"].instances[0].uv == pytest.approx((10 + 35 * 0.7071068, 35 * 0.7071068), abs=1e-4)
    cs = p.references["CS_Motor"]
    assert cs.origin == (10.0, 0.0, 12.0)


def test_rail_linear_count():
    from cadmcp.ir.part import Mount
    from cadmcp.ir.resolve import expand_mount
    holes, info = expand_mount(Mount.model_validate(MOTOR_PLATE["features"][1]), lambda v: float(v))
    assert len(holes[0].positions) == 7  # floor((400-40)/60)+1
    assert info["frame"] == "CS_Start"


def test_interface_check(svc):
    svc.change_begin("M")
    svc.build(PartSpec.model_validate(dict(MOTOR_PLATE, features=MOTOR_PLATE["features"][:1])))
    good = {"name": "ASM", "part_no": "A-1", "title": "电机组件", "components": [
        {"id": "plate", "source": "part:MP"},
        {"id": "motor", "source": "lib:SRV-60-400W", "place": {"align": "CS_Flange", "to": "plate.CS_Motor"}}]}
    r = svc.build(AssemblySpec.model_validate(good))
    assert not [f for f in r["rules"]["findings"] if f["rule"] in ("A003", "A004")]
    wrong = dict(good, name="ASM2", components=[good["components"][0],
                 {"id": "cyl", "source": "lib:CYL-SC32x50", "place": {"align": "CS_Mount", "to": "plate.CS_Motor"}}])
    r = svc.build(AssemblySpec.model_validate(wrong))
    assert any(f["rule"] == "A003" and f["severity"] == "error" for f in r["rules"]["findings"])
    bad_align = dict(good, name="ASM3", components=[good["components"][0],
                     {"id": "motor", "source": "lib:SRV-60-400W", "place": {"align": "CS_Origin", "to": "plate.CS_Motor"}}])
    r = svc.build(AssemblySpec.model_validate(bad_align))
    assert any("CS_Flange" in f["message"] for f in r["rules"]["findings"])
