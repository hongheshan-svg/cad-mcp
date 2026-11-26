import pytest

from cadmcp.errors import CadError
from cadmcp.frame import FrameSpec, frame_assembly


def test_frame_cut_list_and_bom(svc):
    svc.change_begin("F")
    r = svc.build_frame({"name": "FR1", "part_no": "FR-01", "title": "设备机架", "width": 800, "depth": 600,
                         "height": 900, "levels": [20, 300, 880]})
    cut = {c["use"]: (c["length_mm"], c["qty"]) for c in r["cut_list"]}
    assert cut["立柱"] == (900, 4)
    assert cut["X 向横梁"] == (720, 6) and cut["Y 向横梁"] == (520, 6)
    assert r["total_profile_m"] == pytest.approx((4 * 900 + 6 * 720 + 6 * 520) / 1000)
    bom = {b["title"]: b["qty"] for b in r["bom"]}
    assert bom["型材角码 40"] == 24 and bom["T 型螺母 M8（40 系列）"] == 48
    # 装配包围盒 = 外形
    assert r["summary"]["bbox_mm"] == [800, 600, 900]
    # 横梁与立柱不重叠（只在端面接触）
    assert not [f for f in r["rules"]["findings"] if f["rule"] == "A001"]
    from cadmcp.ir import DrawingSpec
    d = svc.build(DrawingSpec.model_validate({"model": "FR1", "title_block": {"designer": "AI"}}))
    assert d["rules"]["passed"], d["rules"]


def test_frame_validation():
    with pytest.raises(CadError):
        frame_assembly(FrameSpec(name="x", part_no="x", width=100, depth=600, height=500))
    with pytest.raises(CadError):
        frame_assembly(FrameSpec(name="x", part_no="x", width=600, depth=600, height=500, levels=[600]))


def test_welded_frame(svc):
    from cadmcp.ir import DrawingSpec
    svc.change_begin("WF")
    r = svc.build_frame({"name": "WF1", "part_no": "WF-01", "title": "焊接机架", "width": 800, "depth": 600,
                         "height": 900, "construction": "welded", "levels": [150, 880]})
    assert r["rules"]["passed"] and not r["rules"]["findings"], r["rules"]
    cut = {(c["spec"], c.get("length_mm")): c["qty"] for c in r["cut_list"]}
    assert cut[("方管 40×40×3", 890.0)] == 4 and cut[("方管 40×40×3", 720.0)] == 4 and cut[("方管 40×40×3", 520.0)] == 4
    assert r["summary"]["bbox_mm"] == [800, 600, 900]  # 地脚板与外形平齐
    d = svc.build(DrawingSpec.model_validate({"model": "WF1", "title_block": {"designer": "AI"}}))
    assert d["rules"]["passed"], d["rules"]
