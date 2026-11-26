import math

import pytest

from cadmcp.errors import CadError
from cadmcp.expr import evaluate, resolve_parameters
from cadmcp.ir import PartSpec
from cadmcp.ir.assembly_resolve import apply, rotate_xyz
from cadmcp.ir.resolve import resolve_part
from cadmcp import rules
from cadmcp.versioning import decide_open, sw_version_from_revision


def test_expressions():
    p = resolve_parameters({"L": 200, "W": "L/2", "e": "max(10, W*0.1)"})
    assert p == {"L": 200, "W": 100, "e": 10}
    assert evaluate("sqrt(9) + cos(0)", {}) == 4
    with pytest.raises(CadError):
        resolve_parameters({"a": "b", "b": "a"})
    with pytest.raises(CadError):
        evaluate("__import__('os')", {})
    with pytest.raises(CadError):
        evaluate("L.real", {"L": 1})


def plate(**kw):
    d = {"name": "P", "part_no": "P-1", "title": "板", "material": "AL6061-T6", "surface_treatment": "阳极氧化",
         "base": {"type": "extrude", "profile": {"shape": "rect", "width": 100, "height": 50}, "thickness": 10}}
    d.update(kw)
    return PartSpec.model_validate(d)


def test_plate_mass_and_holes():
    r = resolve_part(plate(features=[{"feature": "hole", "name": "H", "type": "simple", "size": 10,
                                      "positions": [[0, 0]]}]))
    vol = 100 * 50 * 10 - math.pi * 25 * 10
    assert r.volume_mm3 == pytest.approx(vol)
    assert r.mass_kg == pytest.approx(vol * 2700e-9)
    assert r.holes[0].callout == "Φ10 通"


def test_tapped_callout_and_standard_sizes():
    r = resolve_part(plate(features=[{"feature": "hole", "name": "T", "type": "tapped", "size": "M6", "depth": 8,
                                      "thread_depth": 6, "positions": [[0, 0], [20, 0]]}]))
    h = r.holes[0]
    assert h.drill == 5.0 and h.callout == "2×M6-6H ↧6 底孔Φ5↧8"


def test_rules_edge_distance_and_breakout():
    r = resolve_part(plate(features=[
        {"feature": "hole", "name": "Edge", "type": "clearance", "size": "M6", "positions": [[46, 0]]},
        {"feature": "hole", "name": "Out", "type": "clearance", "size": "M6", "positions": [[49, 22]]},
        {"feature": "hole", "name": "Blind", "type": "tapped", "size": "M5", "depth": 10, "positions": [[0, 0]]},
    ]))
    found = {(f["rule"], f["target"].split("[")[0]) for f in rules.check_part(r)}
    assert ("R003", "Out") in found  # 破边
    assert ("R004", "Blind") in found  # 盲孔=料厚


def test_sheet_metal_flat_pattern():
    r = resolve_part(PartSpec.model_validate({
        "name": "S", "part_no": "S", "title": "罩", "material": "SPCC", "category": "sheet_metal", "surface_treatment": "喷粉",
        "base": {"type": "sheet_metal", "profile": {"shape": "rect", "width": 100, "height": 100}, "thickness": 1,
                 "flanges": [{"edge": "left", "length": 20}]}}))
    ba = math.pi / 2 * (1 + 0.44)
    assert r.flat_pattern[0] == pytest.approx(100 + 20 - 1 - 1 + ba)
    assert r.bbox.zmax == 20


def test_rotation():
    m = rotate_xyz(0, 0, 90)
    assert apply(m, (1, 0, 0)) == pytest.approx((0, 1, 0))
    m = rotate_xyz(0, 90, 0)
    assert apply(m, (1, 0, 0)) == pytest.approx((0, 0, -1))


def test_version_guard():
    assert sw_version_from_revision("32.3.0") == "2024"
    d = decide_open("solidworks", "2022", "solidworks", "2026", want_write=False, allow_upgrade_save=False)
    assert d.readonly
    with pytest.raises(CadError) as e:
        decide_open("solidworks", "2022", "solidworks", "2026", want_write=True, allow_upgrade_save=False)
    assert e.value.code == "VERSION_UPGRADE_BLOCKED"
    with pytest.raises(CadError) as e:
        decide_open("creo", "10.0", "creo", "7.0", want_write=False, allow_upgrade_save=False)
    assert e.value.code == "VERSION_DOWNGRADE_RISK"


def test_drawing_notes_dedupe():
    from cadmcp.detailing import plan_part_drawing
    from cadmcp.ir import DrawingSpec
    r = resolve_part(plate())
    plan = plan_part_drawing(DrawingSpec(model="P", notes=["表面处理：阳极氧化", "未注倒角C0.5，锐边去毛刺", "锐边去毛刺", "调质 HRC28-32"]), r)
    body = "\n".join(plan.notes)
    assert body.count("表面处理") == 1 and "未注倒角C0.5" in body and "调质" in body
    assert sum("去毛刺" in n for n in plan.notes) == 2  # 自动一条 + 含倒角要求的一条
