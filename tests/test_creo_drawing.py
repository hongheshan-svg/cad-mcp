"""Creo 出图：假 creopyson 上跑 DrawingPlan 编译与执行（真实行为见 docs/WINDOWS_TEST.md）。"""
import sys

import pytest
from fake_creopyson import install_fake_creopyson
from test_core import PLATE_PRECISION

from cadmcp.adapters.creo.adapter import CreoAdapter
from cadmcp.adapters.creo.drawing import compile_drawing
from cadmcp.detailing import check_drawing, plan_part_drawing
from cadmcp.errors import CadError
from cadmcp.ir import DrawingSpec, PartSpec
from cadmcp.ir.resolve import resolve_part

TEMPLATES = {f"drawing_{s}": f"{s.lower()}.drw" for s in ("A4", "A3", "A2", "A1", "A0")}


@pytest.fixture
def creo():
    saved = sys.modules.get("creopyson")
    fake = install_fake_creopyson()
    yield fake
    if saved is None:
        sys.modules.pop("creopyson", None)
    else:
        sys.modules["creopyson"] = saved


def _plan():
    r = resolve_part(PartSpec.model_validate(PLATE_PRECISION))
    plan = plan_part_drawing(DrawingSpec(model=r.name, title_block={"designer": "AI"}), r)
    plan.name = r.name + "_DRW"
    return plan


def _adapter():
    return CreoAdapter("10.0", templates=TEMPLATES)


def names(fake):
    return [c[0] for c in fake.calls]


def test_views_scale_pdf(creo, tmp_path):
    plan = _plan()
    br = _adapter().build_drawing(plan, tmp_path / "plate.prt", tmp_path, None)
    calls: dict[str, list] = {}
    for n, kw in creo.calls:
        calls.setdefault(n, []).append(kw)
    dc = calls["drawing_create"][0]  # 图纸：模板按图幅、比例 = plan.scale
    assert dc["template"] == TEMPLATES[f"drawing_{plan.sheet}"] and dc["scale"] == plan.scale
    assert dc["model"] == "plate.prt"
    gen = calls["drawing_create_gen_view"]  # 主视图一般视图，其余为投影视图
    assert gen[0]["model_view"] == "FRONT" and gen[0]["view"] == "V_FRONT"
    proj = calls.get("drawing_create_proj_view", [])
    assert all(p["parent_view"] == "V_FRONT" for p in proj) and proj
    assert len(gen) + len(proj) == len([v for v in plan.views if v.type != "section"])
    front = next(v for v in plan.views if v.type == "front")  # mm 原样写入图纸坐标
    assert gen[0]["point"] == {"x": round(front.origin_on_sheet[0], 4), "y": round(front.origin_on_sheet[1], 4), "z": 0.0}
    for g in gen:
        if g["view"] == "V_ISO":
            assert g["scale"] == plan.scale * 0.5 and g["model_view"] == "TRIMETRIC"
    ps = {kw["name"]: kw["value"] for kw in calls["parameter_set"]}  # 标题栏由模型参数驱动
    assert ps["PART_NO"] == plan.title_block["part_no"] and ps["MATERIAL"] == plan.title_block["material"]
    assert calls["file_save"][0]["file_"] == f"{plan.name}.drw"
    pdf = calls["interface_export_pdf"][0]
    assert pdf["filename"] == f"{plan.name}.pdf" and pdf["dirname"] == str(tmp_path)
    # 回读只放真正读到的视图名
    assert br.extra["rendered"] == {"views": creo.views}
    assert br.path.name == f"{plan.name}.drw" and (tmp_path / f"{plan.name}.oplog.json").exists()
    assert not [f for f in check_drawing(plan, br.extra["rendered"]) if f["rule"] in ("D004", "D005")]


def test_without_extension_warns(creo, tmp_path):
    creo.fail.add("ext")
    br = _adapter().build_drawing(_plan(), tmp_path / "plate.prt", tmp_path, None)
    w = "\n".join(br.regen["warnings"])
    assert br.regen["status"] == "warnings"
    assert "cadmcp_show_dims" in w and "ext.cadmcp_note" in w and "尺寸" in w
    assert any(n.startswith("ext:") for n in names(creo))  # 确实尝试过扩展
    assert "interface_export_pdf" in names(creo)  # 扩展缺失不影响保存/导出


def test_drawing_create_failure_is_error(creo, tmp_path):
    creo.fail.add("drawing_create")
    with pytest.raises(CadError) as e:
        _adapter().build_drawing(_plan(), tmp_path / "plate.prt", tmp_path, None)
    assert e.value.code == "NATIVE_ERROR"
    assert "file_save" not in names(creo)


def test_missing_sheet_template_warns_in_plan():
    ops = compile_drawing(_plan(), "plate.prt", {})
    assert any(o.op == "warn" and "模板" in o.args["message"] for o in ops)


def test_unit_scale_and_capability(creo):
    plan = _plan()
    ops = compile_drawing(plan, "plate.prt", TEMPLATES, sheet_unit_scale=1 / 25.4)
    gen = next(o for o in ops if o.op == "creoson.drawing_create_gen_view")
    front = next(v for v in plan.views if v.type == "front")
    assert gen.args["point"]["x"] == round(front.origin_on_sheet[0] / 25.4, 4)
    assert _adapter().capabilities()["build_drawing"] == "partial"
