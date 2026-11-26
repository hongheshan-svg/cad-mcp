import copy
import csv
import os

import pytest

from cadmcp.errors import CadError
from cadmcp.ir import AssemblySpec, DrawingSpec, PartSpec
from cylinder_station import ASSEMBLY, BASE_PLATE, MOUNT_PLATE


def build_station(svc):
    svc.change_begin("CS01")
    a = svc.build(PartSpec.model_validate(BASE_PLATE))
    b = svc.build(PartSpec.model_validate(MOUNT_PLATE))
    asm = svc.build(AssemblySpec.model_validate(ASSEMBLY))
    for m in (BASE_PLATE["name"], MOUNT_PLATE["name"], ASSEMBLY["name"]):
        svc.build(DrawingSpec.model_validate({"model": m, "title_block": {"designer": "AI"}}))
    return a, b, asm


def test_end_to_end(svc):
    a, b, asm = build_station(svc)
    assert a["rules"]["passed"], a["rules"]
    assert b["rules"]["passed"], b["rules"]
    # 装配位姿：安装板底面 = 底板厚 15 + 立柱 250
    inst = {i.id: i for i in svc._models()[ASSEMBLY["name"]].resolved.instances}
    assert inst["mount"].world_bbox.zmin == pytest.approx(265)
    assert inst["cyl"].world_bbox.zmin == pytest.approx(277)
    bom = {r["title"]: r["qty"] for r in asm["bom"]}
    assert bom["铝型材 40×40"] == 2 and bom["内六角圆柱头螺钉 M6×20"] == 2
    status = svc.change_status()
    assert status["gates"]["every_part_has_drawing"]
    drw = svc.get_model(f"{BASE_PLATE['name']}_DRW")
    texts = [d["text"] for d in drw["dimensions"]]
    assert "4×Φ9 通 ⌴Φ14↧8.6" in texts and "2×M8-6H 通" in texts
    assert os.path.exists(drw["extra"]["preview_svg"] if "extra" in drw else svc._models()[f"{BASE_PLATE['name']}_DRW"].extra["preview_svg"])

    # 关联更新：改底板孔距 → 装配、底板图纸、装配图纸都重建
    r = svc.set_parameters(BASE_PLATE["name"], {"pitch": 180})
    assert set(r["rebuilt_dependents"]) >= {ASSEMBLY["name"], f"{BASE_PLATE['name']}_DRW", f"{ASSEMBLY['name']}_DRW"}

    bom_file = svc.export_bom(ASSEMBLY["name"])["file"]
    rows = list(csv.reader(open(bom_file, encoding="utf-8-sig")))
    assert rows[0][0] == "序号" and len(rows) == 1 + len(asm["bom"])

    rel = svc.commit("A")
    assert os.path.exists(os.path.join(rel["release"], "change_report.json"))
    with pytest.raises(CadError):
        svc.commit("A")


def test_commit_blocked_by_rule_errors(svc):
    svc.change_begin("BAD")
    bad = copy.deepcopy(BASE_PLATE)
    bad["features"].append({"feature": "hole", "name": "Oops", "type": "clearance", "size": "M6", "positions": [[149, 99]]})
    r = svc.build(PartSpec.model_validate(bad))
    assert not r["rules"]["passed"]
    with pytest.raises(CadError) as e:
        svc.commit("A")
    assert e.value.code == "CHECKS_NOT_PASSED"


def test_rollback_restores_models(svc):
    svc.change_begin("RB")
    svc.build(PartSpec.model_validate(BASE_PLATE))
    svc.checkpoint("plate done")
    svc.build(PartSpec.model_validate(MOUNT_PLATE))
    assert len(svc._models()) == 2
    svc.rollback(1)
    assert list(svc._models()) == [BASE_PLATE["name"]]
    svc.rollback(0)
    assert svc._models() == {}


def test_missing_param_hint(svc):
    svc.change_begin("P")
    svc.build(PartSpec.model_validate(BASE_PLATE))
    with pytest.raises(CadError) as e:
        svc.set_parameters(BASE_PLATE["name"], {"Length": 1})
    assert e.value.code == "PARAM_NOT_FOUND" and "pitch" in e.value.hint


def test_write_outside_workspace_denied(svc, tmp_path):
    src = tmp_path / "design" / "x.mprt"
    src.parent.mkdir()
    src.write_text("{}")
    with pytest.raises(CadError) as e:
        svc.change_begin("X", sources=[str(src)])
    assert e.value.code == "POLICY_DENIED"


def test_sw_compile_plan():
    from cadmcp.adapters.solidworks.compile import compile_part
    from cadmcp.ir.resolve import resolve_part
    spec = PartSpec.model_validate(BASE_PLATE)
    ops = compile_part(spec, resolve_part(spec))
    names = [o.op for o in ops]
    assert names[0] == "new_document" and names[-1] == "save_as"
    assert names.count("extrude_cut") == 3  # 沉孔 2 次切除 + 螺纹孔
    assert "cosmetic_thread" in names
    wiz = compile_part(spec, resolve_part(spec), holes_via="wizard")
    assert [o.op for o in wiz].count("hole_wizard") == 2
    gv = [o.args["equation"] for o in ops if o.op == "add_global_variable"]
    assert '"pitch" = 160' in gv
    ext = next(o for o in ops if o.op == "extrude_boss")
    assert ext.args["depth_equation"] == '"T"'
