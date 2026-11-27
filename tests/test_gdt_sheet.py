"""精度符号图纸定位（CAD 原生符号用）：锚点在视图轮廓上，放置点沿外法向。"""
import pytest
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
        if d.extra.get("on_dim"):  # Ra 接在伸出轮廓的直径尺寸线延长线上（文字之后），本就在视图框外
            continue
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


def test_shaft_dim_sheet_coords(svc):
    """轴类尺寸图纸坐标与 SVG 投影一致：P(z, r) = (cx - Ltot*sc/2 + z*sc, cy + r*sc)。"""
    plan = _plan(svc, SHAFT, "SH")
    v = plan.views[0]
    cx, cy = v.origin_on_sheet
    xl = cx - v.size_mm[0] / 2
    got = 0
    for d in plan.dims:
        ex = d.extra or {}
        if d.view != "front" or "sheet_pts" not in ex:
            continue
        got += 1
        (xa, ya), (xb, yb) = ex["sheet_pts"]
        assert cy - v.size_mm[1] / 2 - 0.5 <= min(ya, yb) and max(ya, yb) <= cy + v.size_mm[1] / 2 + 0.5, d
        if d.kind == "linear":
            assert xa == pytest.approx(xl + d.at[0] * v.scale, abs=0.01)
            assert xb == pytest.approx(xl + d.at[1] * v.scale, abs=0.01)
            assert ex["orient"] == "h"
        else:
            assert xa == xb and ya + yb == pytest.approx(2 * cy, abs=0.02)
            assert yb - ya == pytest.approx(d.at[1] * v.scale, abs=0.02)
    assert got >= 8


def test_plate_overall_dim_sheet_coords(svc):
    plan = _plan(svc, PLATE_PRECISION, "PP")
    ov = [d for d in plan.dims if d.kind == "linear" and d.at is None]
    assert ov
    vmap = {v.type: v for v in plan.views}
    for d in ov:
        (xa, ya), (xb, yb) = d.extra["sheet_pts"]
        cx, cy = vmap[d.view].origin_on_sheet
        w, h = vmap[d.view].size_mm
        assert cx - w / 2 - 0.5 <= xa <= cx + w / 2 + 0.5 and cy - h / 2 - 0.5 <= ya <= cy + h / 2 + 0.5
        span = abs(xb - xa) if d.extra["orient"] == "h" else abs(yb - ya)
        assert span == pytest.approx(d.value * vmap[d.view].scale, abs=0.02)
