"""精度符号图纸定位（CAD 原生符号用）：锚点在视图轮廓上，放置点沿外法向。"""
from test_core import PLATE_PRECISION, SHAFT

from cadmcp.ir import DrawingSpec, PartSpec


def _plan(svc, spec, name):
    svc.change_begin(name)
    svc.build(PartSpec.model_validate(spec))
    svc.build(DrawingSpec.model_validate({"model": spec["name"], "title_block": {"designer": "AI"}}))
    return svc._get(spec["name"] + "_DRW").resolved


def _check(plan):
    vmap = {v.type: v for v in plan.views}
    gdt = [d for d in plan.dims if d.kind in ("datum", "gtol", "roughness")]
    assert gdt
    for d in gdt:
        ax, ay = d.extra["sheet_at"]
        v = vmap[d.view]
        cx, cy = v.origin_on_sheet
        w, h = v.size_mm
        assert cx - w / 2 - 0.5 <= ax <= cx + w / 2 + 0.5 and cy - h / 2 - 0.5 <= ay <= cy + h / 2 + 0.5, d
        if d.extra.get("attach") == "callout":
            continue
        nx, ny = d.extra["normal"]
        px, py = d.extra["sheet_pos"]
        assert (px - ax) * nx + (py - ay) * ny >= 0, d                      # 在外侧
        assert abs((px - ax) * ny - (py - ay) * nx) < 1e-6, d                 # 沿法向


def test_plate_precision_sheet_positions(svc):
    _check(_plan(svc, PLATE_PRECISION, "PP"))


def test_shaft_precision_sheet_positions(svc):
    _check(_plan(svc, SHAFT, "SH"))
