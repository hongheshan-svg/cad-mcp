import pytest

from cadmcp import calc
from cadmcp.errors import CadError


def test_cylinder_vertical():
    r = calc.cylinder({"load_kg": 20, "orientation": "vertical", "stroke_mm": 50})
    # F=196.2N, D=√(4F/(π·0.5·0.5))=31.6 → Φ32
    assert r["selection"]["bore"] == 32
    assert r["result"]["required_bore_mm"] == pytest.approx(31.6, abs=0.1)


def test_cylinder_pull_needs_bigger_bore():
    r = calc.cylinder({"force_N": 380, "direction": "pull", "load_rate": 0.7})
    # 推需 D≥37.2→40；拉需 D²-d²≥37.2²，40²-16²=1344<1384 → 50
    assert r["selection"]["bore"] == 50


def test_screw_drive_selects_servo():
    r = calc.screw_drive({"load_kg": 30, "stroke_mm": 400, "speed_mm_s": 500, "accel_time_s": 0.15})
    assert r["steps"][0]["value"] == 10  # 500/10*60=3000rpm
    assert r["selection"]["servo_W"] in (100, 200, 400)
    assert r["selection"]["inertia_ratio"] <= 10
    rv = calc.screw_drive({"load_kg": 30, "stroke_mm": 400, "speed_mm_s": 500, "orientation": "vertical"})
    assert rv["selection"]["brake"] and rv["result"]["T_rms_Nm"] > r["result"]["T_rms_Nm"]


def test_belt_and_bolt():
    r = calc.belt_drive({"load_kg": 5, "stroke_mm": 800, "speed_mm_s": 1500})
    assert r["selection"].get("servo_W")
    b = calc.bolt({"size": "M6", "count": 4, "grade": "12.9", "shear_N": 2000})
    assert b["result"]["pass"]
    b2 = calc.bolt({"size": "M4", "count": 2, "shear_N": 3000})
    assert not b2["result"]["pass"] and b2["warnings"]
    with pytest.raises(CadError):
        calc.run("gear", {})


def test_calc_report_in_session(svc):
    svc.change_begin("C")
    r = svc.calc("cylinder", {"load_kg": 20, "orientation": "vertical", "stroke_mm": 50})
    assert r["library_matches"] == ["CYL-SC32x50"]
    md = open(r["report"], encoding="utf-8").read()
    assert "计算书" in md and "D = √(4F" in md


def test_calc_matches_library_servo(svc):
    r = svc.calc("screw_drive", {"load_kg": 25, "stroke_mm": 300, "speed_mm_s": 400})
    assert r["library_matches"] == ["SRV-60-200W"]
