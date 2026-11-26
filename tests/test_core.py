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
    # 外形尺寸法：100 + 20 − 2·(r+t) + BA（r=t=1）
    assert r.flat_pattern[0] == pytest.approx(100 + 20 - 2 * 2 + ba)
    assert r.bends[0]["flat_pos"] == pytest.approx(-50 + 2 - ba / 2, abs=1e-3)
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


L_BRACKET = {
    "name": "LB", "part_no": "LB-01", "title": "直角座", "material": "AL6061-T6", "surface_treatment": "阳极氧化",
    "parameters": {"A": 80, "B": 60, "t": 10, "W": 40},
    # 截面 L：底边沿 X（厚 t），立边沿 Y（厚 t），沿 Z 挤出宽 W
    "base": {"type": "extrude", "thickness": "W", "profile": {"shape": "polygon", "points": [
        [0, 0], ["A", 0], ["A", "t"], ["t", "t"], ["t", "B"], [0, "B"]]}},
    "features": [
        # 底边：从底面(front, y=0)向上穿 2×M6 腰型孔，可沿 X 调节 10
        {"feature": "hole", "name": "BaseSlot", "face": "front", "type": "slot", "size": "M6", "slot_length": 10,
         "positions": [[50, 10], [50, 30]]},
        # 立边：内侧面(right, x=t)上 2×M5 螺纹通孔
        {"feature": "hole", "name": "WallTap", "face": "right", "at": "t", "type": "tapped", "size": "M5",
         "positions": [[40, 12], [40, 28]]},
    ],
}


def test_l_bracket_slots_and_inner_face():
    import math as m
    r = resolve_part(PartSpec.model_validate(L_BRACKET))
    slot = r.holes[0]
    assert slot.depth == 10  # 只穿过底边料厚，而不是包围盒 60
    assert slot.callout == "2×腰型孔 宽6.6 中心距10 通"
    tap = r.holes[1]
    assert tap.depth == 10 and tap.instances[0].xyz == (10, 40, 12)
    area = 80 * 10 + 10 * 50
    vol = area * 40 - 2 * (10 * 6.6 + m.pi * 6.6 ** 2 / 4) * 10 - 2 * m.pi * 4.2 ** 2 / 4 * 10
    assert r.volume_mm3 == pytest.approx(vol)
    assert rules.summarize(rules.check_part(r))["passed"], rules.check_part(r)


def test_slot_breakout_detected():
    bad = dict(L_BRACKET, features=[{"feature": "hole", "name": "S", "face": "front", "type": "slot", "size": "M6",
                                     "slot_length": 30, "positions": [[65, 20]]}])
    found = {f["rule"] for f in rules.check_part(resolve_part(PartSpec.model_validate(bad)))}
    assert "R003" in found  # 腰型孔一端伸出 80 的底边


def test_drawing_layout_and_missing_view(tmp_path):
    from cadmcp.gateway.service import CadService
    from cadmcp.ir import DrawingSpec
    svc = CadService.from_config(None, None, str(tmp_path)); svc.change_begin("LB")
    svc.build(PartSpec.model_validate(L_BRACKET))
    r = svc.build(DrawingSpec.model_validate({"model": "LB", "title_block": {"designer": "AI"}}))
    assert (r["sheet"], r["scale"]) == ("A3", "1:1")  # 80×60×40 小件：A3 1:1，不放大到 A2
    assert not [f for f in r["rules"]["findings"] if f["severity"] != "info"]
    # 只给主视图：侧面螺纹孔缺对应视图 → D010
    r = svc.build(DrawingSpec.model_validate({"model": "LB", "name": "LB2", "views": [{"type": "front"}]}))
    assert any(f["rule"] == "D010" for f in r["rules"]["findings"])


SHAFT = {
    "name": "SH1", "part_no": "SH-01", "title": "传动轴", "material": "45#", "surface_treatment": "调质 HB220-250",
    "base": {"type": "shaft", "chamfer": 1, "steps": [
        {"length": 40, "diameter": 20, "tolerance": "k6"}, {"length": 12, "diameter": 30},
        {"length": 60, "diameter": 25, "tolerance": "k6"}, {"length": 18, "diameter": 20, "tolerance": "h7"}]},
    "features": [
        {"feature": "keyway", "name": "KW1", "step": 0, "length": 28, "offset": 4},
        {"feature": "keyway", "name": "KW2", "step": 2, "length": 45},
        {"feature": "groove", "name": "G1", "type": "circlip", "step": 3, "position": "end"},
    ],
    "datums": [{"name": "A", "target": "step:0"}, {"name": "B", "target": "step:3"}],
    "tolerances": [{"type": "circular_runout", "value": 0.015, "target": "step:2", "datums": ["A", "B"]},
                   {"type": "cylindricity", "value": 0.005, "target": "step:0"}],
    "roughness": [{"target": "step:0", "ra": 0.8}, {"target": "step:2", "ra": 0.8}, {"target": "step:3", "ra": 1.6}],
}


def test_shaft_features_and_drawing(tmp_path):
    from cadmcp.gateway.service import CadService
    from cadmcp.ir import DrawingSpec
    p = resolve_part(PartSpec.model_validate(SHAFT))
    kw1, kw2, g1 = p.shaft_features
    assert (kw1["width"], kw1["depth"]) == (6.0, 3.5)        # Φ20 → 6×3.5（GB/T 1095）
    assert (kw2["width"], kw2["depth"]) == (8.0, 4.0)        # Φ25 → 8×4
    assert kw2["z0"] == 52 + 7.5                             # 居中
    assert g1["d2"] == 19.0 and g1["width"] == 1.3           # GB/T 894-20
    assert not [f for f in rules.check_part(p) if f["severity"] == "error"]
    bad = dict(SHAFT, features=[{"feature": "keyway", "name": "K", "step": 1, "length": 30}])
    assert any(f["rule"] == "S002" for f in rules.check_part(resolve_part(PartSpec.model_validate(bad))))

    svc = CadService.from_config(None, None, str(tmp_path)); svc.change_begin("SH")
    svc.build(PartSpec.model_validate(SHAFT))
    r = svc.build(DrawingSpec.model_validate({"model": "SH1", "title_block": {"designer": "AI"}}))
    assert r["rules"]["passed"], r["rules"]
    texts = [d["text"] for d in svc.get_model("SH1_DRW")["dimensions"]]
    assert {"Φ20k6(+0.015/+0.002)", "Φ25k6(+0.015/+0.002)", "6N9(0/-0.030)", "16.5", "8N9(0/-0.036)", "21", "2×C1", "130"} <= set(texts)
    assert any("挡圈槽" in t for t in texts)


def test_precision_rules_and_drawing(tmp_path):
    from cadmcp.gateway.service import CadService
    from cadmcp.ir import DrawingSpec
    plate = dict(PLATE_PRECISION)
    p = resolve_part(PartSpec.model_validate(plate))
    assert not [f for f in rules.check_part(p) if f["severity"] == "error"]
    bad = dict(plate, tolerances=[{"type": "parallelism", "value": 0.02, "target": "face:top", "datums": ["C"]},
                                  {"type": "position", "value": 0.1, "target": "hole:Pin"}])
    codes = {f["rule"] for f in rules.check_part(resolve_part(PartSpec.model_validate(bad)))}
    assert {"G001", "G002"} <= codes
    with pytest.raises(CadError):
        resolve_part(PartSpec.model_validate(dict(plate, roughness=[{"target": "step:0", "ra": 1.6}])))
    # 轴：k6 段未标粗糙度 → G004
    s = dict(SHAFT, roughness=[])
    assert any(f["rule"] == "G004" for f in rules.check_part(resolve_part(PartSpec.model_validate(s))))

    svc = CadService.from_config(None, None, str(tmp_path)); svc.change_begin("P")
    svc.build(PartSpec.model_validate(plate))
    r = svc.build(DrawingSpec.model_validate({"model": "PP", "title_block": {"designer": "AI"}}))
    assert r["rules"]["passed"], r["rules"]
    texts = [d["text"] for d in svc.get_model("PP_DRW")["dimensions"]]
    assert "A" in texts and "∥ | 0.02 | A" in texts and "⌖ | Φ0.05 | A | B" in texts and "Ra 1.6" in texts


PLATE_PRECISION = {
    "name": "PP", "part_no": "PP-01", "title": "精密安装板", "material": "AL6061-T6", "surface_treatment": "阳极氧化",
    "base": {"type": "extrude", "profile": {"shape": "rect", "width": 120, "height": 80}, "thickness": 15},
    "features": [{"feature": "hole", "name": "Pin", "type": "simple", "size": 6, "tolerance": "H7",
                  "positions": [[-45, -25], [45, 25]]}],
    "datums": [{"name": "A", "target": "face:bottom"}, {"name": "B", "target": "face:left"}],
    "tolerances": [{"type": "parallelism", "value": 0.02, "target": "face:top", "datums": ["A"]},
                   {"type": "flatness", "value": 0.01, "target": "face:bottom"},
                   {"type": "position", "value": 0.05, "target": "hole:Pin", "datums": ["A", "B"]}],
    "roughness": [{"target": "face:top", "ra": 1.6}, {"target": "hole:Pin", "ra": 1.6}],
}


def test_shaft_section_letters_skip_datums():
    from cadmcp.detailing import plan_part_drawing
    from cadmcp.ir import DrawingSpec
    p = resolve_part(PartSpec.model_validate(SHAFT))
    plan = plan_part_drawing(DrawingSpec.model_validate({"model": "SH1"}), p)
    labels = [v.label for v in plan.views if v.type == "section"]
    assert labels == ["C-C", "D-D"]                        # 基准已占用 A、B
    # 基准 A 不放在键槽剖切线处
    da = next(d for d in plan.dims if d.ref == "datum:A")
    kw1 = p.shaft_features[0]
    assert abs(da.at[0] - (kw1["z0"] + kw1["z1"]) / 2) > 5


def test_sheet_metal_drawing(tmp_path):
    from cadmcp.gateway.service import CadService
    from cadmcp.ir import DrawingSpec
    sm = {"name": "SMB", "part_no": "SM-01", "title": "钣金支架", "material": "SPCC", "category": "sheet_metal",
          "surface_treatment": "喷粉", "base": {"type": "sheet_metal", "profile": {"shape": "rect", "width": 120, "height": 80},
                                               "thickness": 2, "flanges": [{"edge": "front", "length": 40},
                                                                            {"edge": "back", "length": 40}]}}
    svc = CadService.from_config(None, None, str(tmp_path)); svc.change_begin("S")
    svc.build(PartSpec.model_validate(sm))
    r = svc.build(DrawingSpec.model_validate({"model": "SMB", "title_block": {"designer": "AI"},
                                              "views": [{"type": "front"}, {"type": "top"}, {"type": "left"},
                                                        {"type": "flat_pattern"}]}))
    assert r["rules"]["passed"]
    texts = [d["text"] for d in svc.get_model("SMB_DRW")["dimensions"]]
    assert texts.count("上折 90° R2") == 2 and "120" in texts and "38.3" in texts
    notes = svc.get_model("SMB_DRW")["plan"]["notes"]
    assert any("板厚 t2" in n and "K=0.44" in n for n in notes)


def test_sheet_metal_flange_holes():
    sm = {"name": "SM2", "part_no": "SM-02", "title": "U 形支架", "material": "SPCC", "category": "sheet_metal",
          "surface_treatment": "镀锌", "base": {"type": "sheet_metal", "profile": {"shape": "rect", "width": 120, "height": 80},
                                               "thickness": 2, "flanges": [{"edge": "front", "length": 40}]},
          "features": [{"feature": "hole", "name": "F", "type": "clearance", "size": "M5", "face": "front",
                        "positions": [[-40, 25], [40, 25]]},
                       {"feature": "hole", "name": "Low", "type": "clearance", "size": "M5", "face": "front",
                        "positions": [[0, 8]]}]}
    p = resolve_part(PartSpec.model_validate(sm))
    f = next(h for h in p.holes if h.name == "F")
    assert f.depth == 2
    ba = math.pi / 2 * (2 + 0.44 * 2)
    # 展开：距底板外形边 d = 2·OSSB − BA − h = 8 − BA − 25
    assert f.flat_uv[0][1] == pytest.approx(-40 + 8 - ba - 25, abs=0.01)
    # 展开边界 fv0 = v0 − (L − 2·OSSB + BA)；孔在边界内
    assert p.flat_box[2] < f.flat_uv[0][1]
    assert any(x["rule"] == "R008" and "Low" in x["target"] for x in rules.check_part(p))
    with pytest.raises(CadError):
        resolve_part(PartSpec.model_validate(dict(sm, features=[dict(sm["features"][0], face="back")])))


def _weld(**kw):
    import sys
    sys.path.insert(0, "examples")
    from weld_base import weld_base
    spec = weld_base()
    spec["base"].update(kw)
    return spec


def test_weldment_resolve_cut_list_and_welds():
    p = resolve_part(PartSpec.model_validate(_weld()))
    cut = {(r["spec"], r.get("length_mm")): r["qty"] for r in p.cut_list}
    assert cut[("方管 40×40×3", 480.0)] == 4 and cut[("方管 40×40×3", 520.0)] == 4 and cut[("方管 40×40×3", 320.0)] == 4
    assert len(p.welds) == 28 and {w["size"] for w in p.welds} == {3.0}
    # 立柱端面焊在地脚板上：周围焊，长度 = 方管周长
    w = next(w for w in p.welds if {w["a"], w["b"]} == {"P1", "FOOT1"})
    assert w["type"] == "all_around" and w["length"] == 160
    # 质量：12 根方管 + 板
    tube = (40 * 40 - 34 * 34) * (4 * 480 + 4 * 520 + 4 * 320)
    plates = 4 * 80 * 80 * 10 + 600 * 400 * 10
    holes = 4 * math.pi / 4 * 14 ** 2 * 10 + 4 * math.pi / 4 * 6.8 ** 2 * 10
    assert p.mass_kg == pytest.approx((tube + plates - holes) * 7.85e-6, rel=0.01)
    assert not [f for f in rules.check_part(p) if f["severity"] == "error"]
    top = next(h for h in p.holes if h.name == "TOP_M8")
    assert top.depth == 10 and top.instances[0].xyz[2] == 500


def test_weldment_rules():
    spec = _weld()
    # 立柱穿进顶板 → W001；去掉地脚板 → 管端敞口 W003；浮空板 → W004；焊脚过大 → W002
    spec["base"]["members"][0]["end"][2] = 495
    spec["base"]["plates"] = spec["base"]["plates"][1:] + [
        {"name": "LOOSE", "size": [50, 50], "thickness": 5, "center": [0, 0, 250]}]
    spec["base"]["weld_size"] = 6
    rs = {f["rule"] for f in rules.check_part(resolve_part(PartSpec.model_validate(spec)))}
    assert {"W001", "W002", "W003", "W004"} <= rs
    with pytest.raises(CadError):
        resolve_part(PartSpec.model_validate(_weld(members=[{"name": "D", "section": "SHS40x3", "start": [0, 0, 0],
                                                             "end": [100, 0, 100]}])))
    with pytest.raises(CadError):
        resolve_part(PartSpec.model_validate(_weld(members=[{"name": "D", "section": "XYZ40", "start": [0, 0, 0],
                                                             "end": [100, 0, 0]}])))


def test_weldment_sections():
    from cadmcp.ir.weldment import section
    assert section("RHS60x40x3").area == pytest.approx(60 * 40 - 54 * 34)
    assert section("L50x5").area == pytest.approx(5 * 95)
    assert section("C10").area == pytest.approx(1275) and section("I14").h == 140
    assert section("CHS48x3").title == "圆管 Φ48×3" and not section("SHS45x3").standard


def test_weldment_drawing(tmp_path):
    from cadmcp.gateway.service import CadService
    from cadmcp.ir import DrawingSpec
    svc = CadService.from_config(None, None, str(tmp_path / "ws"))
    svc.change_begin("WB")
    svc.build(PartSpec.model_validate(_weld()))
    r = svc.build(DrawingSpec.model_validate({"model": "WB1", "title_block": {"designer": "AI"}}))
    assert r["rules"]["passed"] and not r["rules"]["findings"], r["rules"]
    assert (r["sheet"], r["scale"]) == ("A2", "1:4")
    plan = svc._get("WB1_DRW").resolved
    welds = [d for d in plan.dims if d.kind == "weld"]
    assert sum(d.extra["count"] for d in welds) == 28
    assert any(d.text == "4×Φ14 通" for d in plan.dims)
    plan = plan.to_dict()
    assert any("GB/T 985.1" in n for n in plan["notes"]) and any("焊后加工" in n for n in plan["notes"])
    assert len(plan["cut_list"]) == 5 and len(plan["balloons"]) == 5


def test_iso_draws_slots_and_through_exits():
    """轴测图：可见面上的腰型孔画孔口轮廓；内侧面上的通孔在外表面出口照画。"""
    from cadmcp.detailing import plan_part_drawing
    from cadmcp.ir import DrawingSpec
    from cadmcp.render_svg import _iso_top_features, _Svg, _iso_xy
    p = resolve_part(PartSpec.model_validate(L_BRACKET))
    s = _Svg(420, 297)
    _iso_top_features(s, _iso_xy(200, 150, 1.0, p.bbox), p, p.bbox.zmax)
    assert len(s.outlines) > 0
    rings = [x for x in s.parts if x.startswith("<poly")]
    assert len(rings) == 4  # 2 个腰型孔 + 2 个 M5 出口
    assert plan_part_drawing(DrawingSpec(model="LB"), p)
