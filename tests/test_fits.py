"""GB/T 1800.2（ISO 286）极限偏差查表 + 图上叠写。"""
import pytest

from cadmcp.detailing import split_deviation, text_width, with_deviations
from cadmcp.standards import fit_text, limit_deviations


@pytest.mark.parametrize("d,code,up,lo", [
    (25, "H7", 0.021, 0), (60, "H7", 0.030, 0), (8, "H7", 0.015, 0),
    (25, "k6", 0.015, 0.002), (20, "k6", 0.015, 0.002), (30, "m6", 0.021, 0.008),
    (40, "n6", 0.033, 0.017), (50, "p6", 0.042, 0.026),
    (25, "g6", -0.007, -0.020), (25, "f7", -0.020, -0.041), (25, "h6", 0, -0.013),
    (25, "G7", 0.028, 0.007), (25, "F7", 0.041, 0.020),
    (25, "K7", 0.006, -0.015), (25, "M7", 0, -0.021), (25, "N7", -0.007, -0.028), (25, "P7", -0.014, -0.035),
    (25, "JS7", 0.010, -0.010), (25, "js6", 0.0065, -0.0065),
    (6, "N9", 0, -0.030), (8, "N9", 0, -0.036), (6, "P9", -0.012, -0.042), (6, "JS9", 0.015, -0.015),
    (14, "D10", 0.120, 0.050),
])
def test_limit_deviations(d, code, up, lo):
    assert limit_deviations(d, code) == pytest.approx((up, lo), abs=1e-9)


def test_out_of_table():
    assert limit_deviations(600, "H7") is None and limit_deviations(25, "H13") is None
    assert fit_text(25, "X7") == "Φ25X7"


def test_callout_text_and_stacked_width():
    t = with_deviations("Φ60H7 通")
    assert t == "Φ60H7(+0.030/0) 通"
    assert split_deviation(t) == ("Φ60H7", "+0.030", "0", " 通")
    assert with_deviations("4×M5-6H ↧8") == "4×M5-6H ↧8"  # 螺纹不动
    assert text_width(t, 3.5) < text_width("Φ60H7+0.030/0 通", 3.5)  # 叠写比单行短


def test_switch_off():
    from cadmcp.detailing import plan_part_drawing
    from cadmcp.ir import DrawingSpec, PartSpec
    from cadmcp.ir.resolve import resolve_part
    p = resolve_part(PartSpec.model_validate({
        "name": "F", "part_no": "F", "title": "板", "material": "45#",
        "base": {"type": "extrude", "profile": {"shape": "rect", "width": 100, "height": 60}, "thickness": 10},
        "features": [{"feature": "hole", "name": "P", "type": "simple", "size": 20, "tolerance": "H7",
                      "positions": [[0, 0]]}]}))
    on = plan_part_drawing(DrawingSpec(model="F"), p)
    off = plan_part_drawing(DrawingSpec(model="F", fit_deviations=False), p)
    assert next(d.text for d in on.dims if d.ref == "P") == "Φ20H7(+0.021/0) 通"
    assert next(d.text for d in off.dims if d.ref == "P") == "Φ20H7 通"
