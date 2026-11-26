from weld_base import weld_base

from cadmcp import detailing
from cadmcp.adapters.solidworks.compile import compile_part
from cadmcp.ir import DrawingSpec, PartSpec
from cadmcp.ir.resolve import resolve_part


def _plan(svc, **kw):
    svc.change_begin("W")
    svc.build(PartSpec.model_validate({**weld_base(), **kw}))
    d = svc.build(DrawingSpec.model_validate({"model": "WB1", "title_block": {"designer": "AI"}}))
    return d, svc._get("WB1_DRW")


def test_weld_symbols_and_cut_list_layout(svc):
    d, m = _plan(svc)
    assert d["rules"]["passed"]
    plan = m.resolved
    welds = [x for x in plan.dims if x.kind == "weld"]
    kinds = {(x.extra["all_around"], x.extra["both_sides"]) for x in welds}
    assert kinds == {(True, False), (False, True)} and all(x.extra["process"] == "135" for x in welds)
    assert sum(x.extra["count"] for x in welds) == len(resolve_part(PartSpec.model_validate(weld_base())).welds)
    assert [r["item"] for r in plan.cut_list] == list(range(1, len(plan.cut_list) + 1))
    assert [b["item"] for b in plan.balloons] == [r["item"] for r in plan.cut_list]   # 顺时针编号 = 切割清单序号
    assert any("CO2 气体保护焊（135）" in n for n in plan.notes)
    r = m.extra["rendered"]
    assert r["weld_symbols"] == 2 and len(r["cut_list_rows"]) == len(plan.cut_list)


def test_weld_process_option(svc):
    base = weld_base()
    base["base"]["weld_process"] = "111"
    _, m = _plan(svc, base=base["base"])
    assert all(x.extra["process"] == "111" for x in m.resolved.dims if x.kind == "weld")
    assert any("焊条电弧焊（111）" in n for n in m.resolved.notes)


def test_weldment_readback_checks(svc):
    _, m = _plan(svc)
    plan = m.resolved
    ok = {"cut_list_rows": [[r["item"], f"L={r['length_mm']:g}" if r["kind"] == "member" else
                             "×".join(f"{v:g}" for v in r["size_mm"]), r["qty"]] for r in plan.cut_list],
          "weld_symbols": 2}
    assert not detailing.check_weldment_drawing(plan, ok)
    bad = {"cut_list_rows": ok["cut_list_rows"][1:], "weld_symbols": 1}
    assert {f["rule"] for f in detailing.check_weldment_drawing(plan, bad)} == {"D013", "D014"}
    bad = {"cut_list_rows": [[a, "", q] for a, _, q in ok["cut_list_rows"]]}  # 长度列空（未写 LENGTH 属性）
    assert detailing.check_weldment_drawing(plan, bad)[0]["rule"] == "D013"


def test_cut_list_properties_compiled():
    spec = PartSpec.model_validate(weld_base())
    p = resolve_part(spec)
    op = next(o for o in compile_part(spec, p) if o.op == "cut_list_properties")
    rows = op.args["rows"]
    assert len(rows) == len(p.cut_list)
    member = next(r for r in rows if "下料长度" in r["props"])
    assert member["props"]["LENGTH"] == member["props"]["下料长度"] and member["props"]["MATERIAL"] == "Q235"
    assert sorted(b for r in rows for b in r["bodies"]) == sorted(b["name"] for b in p.weld_bodies)


def test_weld_top_view_hides_frame_under_plate():
    """俯视图：顶板之下的管架被遮挡画细虚线；顶板轮廓与伸出板外的底脚仍为粗实线。"""
    from cadmcp.render_svg import _split_hidden, render
    p = resolve_part(PartSpec.model_validate(weld_base()))
    plan = detailing.plan_part_drawing(DrawingSpec.model_validate({"model": p.name, "views": [{"type": "top"}]}), p)
    svg, _ = render(plan, p)
    assert svg.count("stroke-dasharray='3,1'") >= 8
    assert _split_hidden((0, 5), (10, 5), [(2, 0, 4, 10)]) == [(0.0, 0.2, False), (0.2, 0.4, True), (0.4, 1.0, False)]
    assert _split_hidden((0, 0), (10, 0), [(2, 0, 4, 10)]) == [(0.0, 1.0, False)]  # 落在遮挡框边界上：可见
