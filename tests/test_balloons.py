from cylinder_station import ASSEMBLY, BASE_PLATE, MOUNT_PLATE

from cadmcp import detailing
from cadmcp.adapters.solidworks.compile import compile_drawing
from cadmcp.ir import AssemblySpec, DrawingSpec, PartSpec


def _station(svc):
    svc.change_begin("A")
    for d in (BASE_PLATE, MOUNT_PLATE):
        svc.build(PartSpec.model_validate(d))
    asm = {**ASSEMBLY, "bom_extras": [{"source": "lib:SHCS-M8x16", "qty": 4, "note": "地脚"}]}
    svc.build(AssemblySpec.model_validate(asm))
    d = svc.build(DrawingSpec.model_validate({"model": ASSEMBLY["name"], "title_block": {"designer": "AI"}}))
    return d, svc._get(ASSEMBLY["name"] + "_DRW").resolved


def test_balloon_layout_clockwise(svc):
    d, plan = _station(svc)
    assert d["rules"]["passed"]
    v = plan.views[0]
    cx, cy = v.origin_on_sheet
    w, h = v.size_mm
    bs = plan.balloons
    assert [b["item"] for b in bs] == list(range(1, len(bs) + 1))
    assert plan.bom[-1]["part_no"].startswith("SHCS-M8x16") and plan.bom[-1]["item"] == len(bs) + 1  # 只进明细表的排最后
    for b in bs:  # 序号在视图外，引出点在视图内
        x, y = b["pos"]
        assert not (cx - w / 2 < x < cx + w / 2 and cy - h / 2 < y < cy + h / 2)
        ax, ay = b["anchor"]
        assert cx - w / 2 - 1 <= ax <= cx + w / 2 + 1 and cy - h / 2 - 1 <= ay <= cy + h / 2 + 1
    sides = [b["side"] for b in bs]
    assert sides == sorted(sides, key=["top", "right", "left"].index)  # 顺时针
    top = [b for b in bs if b["side"] == "top"]
    assert [b["pos"][0] for b in top] == sorted(b["pos"][0] for b in top)
    assert len({b["anchor"] for b in bs}) == len(bs)  # 引出点不重合


def test_association_check(svc):
    _, plan = _station(svc)
    good = {"bom_rows": [(r["item"], r["part_no"], r["qty"]) for r in plan.bom],
            "balloon_items": [b["item"] for b in plan.balloons]}
    assert not detailing.check_bom_association(plan, good)
    bad = {"bom_rows": [(r["item"], r["part_no"], r["qty"] + (1 if r["item"] == 2 else 0)) for r in plan.bom],
           "balloon_items": [b["item"] for b in plan.balloons][1:] + [1, 99]}
    rules = [(f["rule"], f["severity"]) for f in detailing.check_bom_association(plan, bad)]
    assert ("D011", "error") in rules and rules.count(("D012", "error")) == 1  # 99 无对应行
    bad["balloon_items"] = [b["item"] for b in plan.balloons][1:]
    assert any("没有标到图上" in f["message"] for f in detailing.check_bom_association(plan, bad))


def test_sw_ops_carry_rows_and_balloons(svc):
    _, plan = _station(svc)
    ops = compile_drawing(plan, "x.SLDASM")
    bt = next(o for o in ops if o.op == "bom_table")
    rows = {r["item"]: r for r in bt.args["rows"]}
    base = next(r for r in rows.values() if r["part_no"] == BASE_PLATE["part_no"])
    assert base["keys"] == [BASE_PLATE["part_no"].lower(), BASE_PLATE["name"].lower()]
    bb = next(o for o in ops if o.op == "bom_balloons")
    assert len(bb.args["balloons"]) == len(plan.balloons)
    assert all(0 < b["pos"][0] < 1.2 for b in bb.args["balloons"])  # 米
    assert not any(o.op == "auto_balloon" for o in ops)


def test_assembly_sheet_not_oversized(svc):
    """审图：视图只需避开右侧栏（标题栏/明细表），不该因此换大图幅而大片留白。"""
    d, plan = _station(svc)
    assert plan.sheet == "A3"


def test_assembly_install_dims():
    """装配图：底板成组通孔的孔距作为安装尺寸标在俯视图，孔规格进技术要求。"""
    import sys
    sys.path.insert(0, "examples")
    from cylinder_station import ASSEMBLY, BASE_PLATE, MOUNT_PLATE

    from cadmcp.gateway.service import CadService
    from cadmcp.ir import AssemblySpec, DrawingSpec, PartSpec
    import tempfile
    svc = CadService.from_config(None, None, tempfile.mkdtemp())
    svc.change_begin("T")
    for d in (BASE_PLATE, MOUNT_PLATE):
        svc.build(PartSpec.model_validate(d))
    svc.build(AssemblySpec.model_validate(ASSEMBLY))
    from cadmcp.detailing import plan_assembly_drawing
    a = svc._models()[ASSEMBLY["name"]].resolved
    plan = plan_assembly_drawing(DrawingSpec(model=ASSEMBLY["name"]), a)
    inst = {d.axis: d for d in plan.dims if d.ref.startswith("install_")}
    assert {round(d.value) for d in inst.values()} == {260, 160}
    assert all(d.view == "top" and len(d.extra["sheet_pts"]) == 2 for d in inst.values())
    assert any("安装孔 4×Φ9" in n for n in plan.notes)
    from cadmcp.render_svg import render
    svg, out = render(plan, a)
    assert "stroke-dasharray='3,1'" in svg  # 俯视图：安装板下的型材被遮挡，画细虚线
    assert out["crossings"] == []


def test_screw_simplified_drawing(svc):
    """装配图螺钉按 GB/T 4459.1 简化画法：头部 + 螺杆两段轮廓，螺纹小径（0.85d）细实线贯穿螺纹长度。"""
    import re

    from cadmcp.render_svg import render
    _station(svc)
    a = svc._get(ASSEMBLY["name"]).resolved
    plan = svc._get(ASSEMBLY["name"] + "_DRW").resolved
    assert not plan.asm_section  # 只有螺钉的回转轴线不触发全剖
    svg, _ = render(plan, a)
    L = 20 * plan.scale
    thin = [(float(x1), float(y1), float(x2), float(y2)) for x1, y1, x2, y2 in
            re.findall(r"<line x1='([\d.]+)' y1='([\d.]+)' x2='([\d.]+)' y2='([\d.]+)' stroke='#111' stroke-width='0.25'/>", svg)]
    pairs = [t for t in thin if abs(t[0] - t[2]) < 1e-6 and 0.5 < abs(t[1] - t[3]) <= L + 0.02]
    assert len(pairs) >= 4  # 主视图 2 个螺钉 × 两条小径线（拧入板内的一段被挡、省略）
    assert any(abs(abs(p[0] - q[0]) - 0.85 * 6 * plan.scale) < 0.02 for p in pairs for q in pairs)
    assert "stroke-dasharray='3,1'" in svg  # 螺杆在板内的一段画细虚线
