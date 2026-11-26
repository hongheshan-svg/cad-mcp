import json
from pathlib import Path

import pytest
from legacy import write_legacy

from cadmcp.errors import CadError


@pytest.fixture
def legacy(svc, tmp_path):
    src = write_legacy(tmp_path / "designs" / "OLD")
    svc.policy.read_allow.append((tmp_path / "designs").resolve())
    svc.change_begin("OLD", sources=[str(src)])
    return svc, src


def test_inspect(legacy):
    svc, _ = legacy
    r = svc.inspect_existing("Bracket.mprt")
    assert {d["name"] for d in r["dimensions"]} >= {"D1@Boss-Extrude1", "D3@Sketch2"}
    assert r["used_by"] == ["Bracket.mdrw", "Station.masm"] and r["mass_kg"] == 0.75


def test_edit_validation(legacy):
    svc, _ = legacy
    r = svc.edit_existing("Bracket.mprt", [{"op": "set_dimension", "name": "D1@Boss-Extrud1", "value": 12}])
    assert not r["applied"] and r["findings"][0]["rule"] == "E001" and "D1@Boss-Extrude1" in r["findings"][0]["hint"]
    r = svc.edit_existing("Bracket.mprt", [{"op": "set_dimension", "name": "D3@Sketch2", "value": 90}])
    assert r["findings"][0]["rule"] == "E003" and "set_variable" in r["findings"][0]["hint"]
    r = svc.edit_existing("Bracket.mprt", [{"op": "set_dimension", "name": "D1@Boss-Extrude1", "value": 100}],
                          dry_run=True)
    assert r["ok"] and not r["applied"] and r["findings"][0]["rule"] == "E002"  # 变化 ×10 警告
    with pytest.raises(CadError):
        svc.edit_existing("Nope.mprt", [])


def test_edit_rebuild_failure_keeps_file(legacy):
    svc, _ = legacy
    r = svc.edit_existing("Bracket.mprt", [{"op": "set_variable", "name": "pitch", "value": 130}])  # 孔距 > 宽度
    assert not r["ok"] and r["regen"]["status"] == "errors"
    assert svc.inspect_existing("Bracket.mprt")["variables"][0]["value"] == 80


def test_edit_commit_writeback(legacy, tmp_path):
    svc, src = legacy
    r = svc.edit_existing("Bracket.mprt", [{"op": "set_dimension", "name": "D1@Boss-Extrude1", "value": 12},
                                           {"op": "set_variable", "name": "pitch", "value": 90}])
    assert r["applied"] and r["rules"]["passed"]
    assert r["diff"]["mass_kg"]["after"] == pytest.approx(0.9)
    assert {"name": "D3@Sketch2", "before": 80, "after": 90.0} in r["diff"]["dimensions"]  # 方程式联动
    assert [x["file"] for x in r["refreshed"]] == ["Bracket.mdrw", "Station.masm"]
    assert r["refreshed"][0]["dangling"] == 0
    c = svc.commit("B")
    assert "D1@Boss-Extrude1 10→12" in c["revision_entry"]["description"]
    # 回写：默认不允许
    w = svc.writeback()
    assert set(f["status"] for f in w["files"]) == {"denied"} and len(w["files"]) == 3
    svc.policy.writeback_allow.append(src.resolve())
    w = svc.writeback()
    assert [f["status"] for f in w["files"]] == ["ready"] * 3 and not w["confirmed"]  # 零件 + 更新过的图纸、装配
    original = json.loads((src / "Bracket.mprt").read_text(encoding="utf-8"))
    assert original["native"]["mass_kg"] == 0.75  # 未确认前原文件不变
    w = svc.writeback(confirm=True)
    f = {x["file"]: x for x in w["files"]}
    assert f["Bracket.mprt"]["status"] == f["Bracket.mdrw"]["status"] == "written"
    assert json.loads((src / "Bracket.mprt").read_text(encoding="utf-8"))["native"]["mass_kg"] == pytest.approx(0.9)
    assert json.loads(open(f["Bracket.mprt"]["backup"], encoding="utf-8").read())["native"]["mass_kg"] == 0.75


def test_writeback_conflict(legacy):
    svc, src = legacy
    svc.edit_existing("Bracket.mprt", [{"op": "set_dimension", "name": "D1@Boss-Extrude1", "value": 12}])
    svc.commit("B")
    svc.policy.writeback_allow.append(src.resolve())
    (src / "Bracket.mprt").chmod(0o644)
    (src / "Bracket.mprt").write_text((src / "Bracket.mprt").read_text(encoding="utf-8") + " ", encoding="utf-8")  # 别人改了
    w = svc.writeback(confirm=True)
    f = {x["file"]: x["status"] for x in w["files"]}
    assert f == {"Bracket.mprt": "conflict", "Bracket.mdrw": "ready", "Station.masm": "ready"} and not w["ok"]
    assert w["blocked_by"] == ["Bracket.mprt"] and not w["written"]  # 一致性：工程图也不写


def test_suppress_dangling_blocks_commit(legacy):
    svc, _ = legacy
    r = svc.edit_existing("Bracket.mprt", [{"op": "suppress", "feature": "Fillet1"}])
    assert r["applied"] and any(f["rule"] == "E010" for f in r["findings"])
    with pytest.raises(CadError):
        svc.commit("B")
    svc.rollback(r["rollback_to"])


def test_replace_component_same_interface(legacy):
    svc, _ = legacy
    info = svc.inspect_existing("Station.masm")
    assert [c["instance"] for c in info["components"]] == ["CYL-SC32x50-1", "Bracket-1"]
    r = svc.edit_existing("Station.masm", [{"op": "replace_component", "instance": "CYL-SC32x50-1",
                                            "source": "lib:CYL-SC32x100"}])
    assert r["applied"] and r["rules"]["passed"], r
    assert not [f for f in r["findings"] if f["rule"] == "E012"]  # 安装接口相同
    assert any(f["rule"] == "E014" for f in r["findings"])          # 行程长了，外形变大 → 提醒干涉
    assert r["diff"]["components"] == [{"instance": "CYL-SC32x50-1", "before": "CYL-SC32x50", "after": "CYL-SC32x100"}]
    assert r["diff"]["mass_kg"]["delta"] == pytest.approx(0.16)
    assert "换为 CYL-SC32x100" in r["description"]


def test_replace_component_interface_change(legacy):
    svc, _ = legacy
    r = svc.edit_existing("Station.masm", [{"op": "replace_component", "instance": "CYL-SC32x50-1",
                                            "source": "lib:CYL-SC40x50"}], dry_run=True)
    e12 = next(f for f in r["findings"] if f["rule"] == "E012")
    assert "安装孔位" in e12["message"] and "定位止口 26 → 32" in e12["message"] and "Bracket-1" in e12["hint"]
    # 新件没有配合用的基准 → 配合会断开（error，拒绝执行）
    r = svc.edit_existing("Station.masm", [{"op": "replace_component", "instance": "CYL-SC32x50-1",
                                            "source": "lib:BRK-4040"}])
    assert not r["applied"] and any(f["rule"] == "E013" for f in r["findings"])
    r = svc.edit_existing("Station.masm", [{"op": "replace_component", "instance": "NoSuch", "source": "lib:CYL-SC40x50"}])
    assert r["findings"][0]["rule"] == "E001"


def test_replace_then_writeback_copies_new_part(legacy):
    svc, src = legacy
    svc.edit_existing("Station.masm", [{"op": "replace_component", "instance": "CYL-SC32x50-1",
                                        "source": "lib:CYL-SC32x100"}])
    svc.commit("B")
    svc.policy.writeback_allow.append(src.resolve())
    w = svc.writeback(confirm=True)
    assert w["ok"], w
    new = [f for f in w["files"] if f.get("new")]
    assert [f["file"] for f in new] == ["PH_CYL-SC32x100.mprt"] and (src / "PH_CYL-SC32x100.mprt").exists()
    asm = json.loads((src / "Station.masm").read_text(encoding="utf-8"))["native"]
    assert asm["components"][0]["file"] == "PH_CYL-SC32x100.mprt"


def test_where_used_outside_workspace(svc, tmp_path):
    """只复制进单个零件：共享盘上其他项目引用它的装配及装配图自动纳入并更新；同名的别的零件不受影响。"""
    from legacy import HDR
    designs = tmp_path / "designs"
    old = write_legacy(designs / "OLD")
    other = designs / "OTHER"
    other.mkdir()
    machine = {**HDR, "kind": "assembly", "name": "Machine", "native": {
        "references": [str((old / "Bracket.mprt").resolve())],
        "components": [{"instance": "Bracket-1", "file": "Bracket.mprt", "code": "Bracket", "mass_kg": 0.75}],
        "mates": [], "mass_kg": 0.75}}
    mdrw = {**HDR, "kind": "drawing", "name": "Machine_DRW", "native": {"references": ["Machine.masm"], "annotations": []}}
    (other / "Machine.masm").write_text(json.dumps(machine), encoding="utf-8")
    (other / "Machine.mdrw").write_text(json.dumps(mdrw), encoding="utf-8")
    decoy = designs / "DECOY"
    decoy.mkdir()
    (decoy / "Bracket.mprt").write_text((old / "Bracket.mprt").read_text(encoding="utf-8"), encoding="utf-8")
    user = {**HDR, "kind": "assembly", "name": "User", "native": {"references": ["Bracket.mprt"], "components": []}}
    (decoy / "User.masm").write_text(json.dumps(user), encoding="utf-8")

    svc.policy.read_allow.append(designs.resolve())
    svc.change_begin("OLD", sources=[str(old / "Bracket.mprt")])
    info = svc.inspect_existing("Bracket.mprt")
    assert info["used_by_outside_by_folder"] == {"OLD": 2, "OTHER": 2} and info["used_by_outside_count"] == 4
    outside = sorted(Path(x).name for x in info["used_by_outside"])
    assert outside == ["Bracket.mdrw", "Machine.masm", "Machine.mdrw", "Station.masm"]  # 不含 DECOY/User.masm
    r = svc.edit_existing("Bracket.mprt", [{"op": "set_dimension", "name": "D1@Boss-Extrude1", "value": 12}])
    assert sorted(r["imported_dependents"]) == ["Bracket.mdrw", "Station.masm", "_ext/designs/OTHER/Machine.masm",
                                                "_ext/designs/OTHER/Machine.mdrw"]
    assert [x["file"] for x in r["refreshed"]] == ["Bracket.mdrw", "Station.masm", "_ext/designs/OTHER/Machine.masm",
                                                   "_ext/designs/OTHER/Machine.mdrw"]
    svc.commit("B")
    svc.policy.writeback_allow.append(designs.resolve())
    w = svc.writeback(confirm=True)
    assert w["ok"] and {Path(f["to"]).name for f in w["files"]} == {"Bracket.mprt", "Bracket.mdrw", "Station.masm",
                                                                  "Machine.masm", "Machine.mdrw"}
    assert json.loads((other / "Machine.masm").read_text(encoding="utf-8"))["native"]["refreshed"]


def test_no_read_allow_warns(svc, tmp_path):
    src = write_legacy(tmp_path / "d")
    svc.policy.check_read = lambda p: None
    svc.change_begin("X", sources=[str(src / "Bracket.mprt")])
    r = svc.edit_existing("Bracket.mprt", [{"op": "set_dimension", "name": "D1@Boss-Extrude1", "value": 12}])
    assert any(f["rule"] == "E015" for f in r["findings"])


def test_many_dependents_scale(svc, tmp_path):
    import sys
    import time
    from legacy import HDR
    designs = tmp_path / "designs"
    old = write_legacy(designs / "OLD")
    for k in range(20):
        proj = designs / f"P{k:02d}"
        proj.mkdir()
        for j in range(100):  # 2000 个装配引用同一个共用件
            a = {**HDR, "kind": "assembly", "name": f"A{j}", "native": {
                "references": [str((old / "Bracket.mprt").resolve())], "components": [], "mates": []}}
            (proj / f"A{j}.masm").write_text(json.dumps(a), encoding="utf-8")
    svc.policy.read_allow.append(designs.resolve())
    svc.change_begin("OLD", sources=[str(old / "Bracket.mprt")])
    t0 = time.perf_counter()
    info = svc.inspect_existing("Bracket.mprt")
    assert info["used_by_outside_count"] == 2002 and info["used_by_outside_by_folder"]["P00"] == 100
    assert len(info["used_by_outside"]) == 50
    r = svc.edit_existing("Bracket.mprt", [{"op": "set_dimension", "name": "D1@Boss-Extrude1", "value": 12}])
    assert r["applied"] and r["imported_count"] == 2002 and len(r["refreshed"]) == 2002
    # 防止退回 O(n²)（旧实现本机 109 s）；Windows runner 复制 2002 个文件本身就要 30 s 左右
    assert time.perf_counter() - t0 < (90 if sys.platform == "win32" else 30)
    svc.policy.max_import_dependents = 1000  # 超过上限：拒绝
    r = svc.edit_existing("Bracket.mprt", [{"op": "set_dimension", "name": "D1@Boss-Extrude1", "value": 14}])
    assert not r["applied"] and "上限 1000" in r["error"]


def _two_projects(svc, tmp_path):
    from legacy import HDR
    designs = tmp_path / "designs"
    old = write_legacy(designs / "OLD")
    for proj in ("RELEASED", "WIP"):
        d = designs / proj
        d.mkdir()
        a = {**HDR, "kind": "assembly", "name": f"M_{proj}", "native": {
            "references": [str((old / "Bracket.mprt").resolve())], "components": [], "mates": []}}
        (d / f"M_{proj}.masm").write_text(json.dumps(a), encoding="utf-8")
    svc.policy.read_allow.append(designs.resolve())
    return designs, old


def test_dependents_mode_frozen_and_save_as(svc, tmp_path):
    designs, old = _two_projects(svc, tmp_path)
    svc.policy.dependents_rules = [((designs / "RELEASED").resolve(), "frozen"), ((designs / "WIP").resolve(), "report")]
    assert svc.policy.dependents_mode_for(designs / "RELEASED" / "x.masm") == "frozen"
    assert svc.policy.dependents_mode_for(designs / "OLD" / "x.masm") == "update"
    svc.change_begin("OLD", sources=[str(old / "Bracket.mprt")])
    ed = [{"op": "set_dimension", "name": "D1@Boss-Extrude1", "value": 12}]
    r = svc.edit_existing("Bracket.mprt", ed)
    assert not r["applied"] and {f["rule"] for f in r["findings"]} >= {"E017", "E018"}
    assert r["dependents"]["frozen"]["by_folder"] == {"RELEASED": 1}
    before = (old / "Bracket.mprt").read_text(encoding="utf-8")
    r = svc.edit_existing("Bracket.mprt", ed, save_as="Bracket-B.mprt")
    assert r["applied"] and r["saved_as"] == "Bracket-B.mprt" and not r.get("imported_count")
    assert (old / "Bracket.mprt").read_text(encoding="utf-8") == before
    with pytest.raises(CadError):
        svc.edit_existing("Bracket.mprt", ed, save_as="Bracket-B.mprt")  # 已存在


def test_dependents_mode_report_only(svc, tmp_path):
    designs, old = _two_projects(svc, tmp_path)
    svc.policy.dependents_rules = [((designs / "RELEASED").resolve(), "report")]
    svc.change_begin("OLD", sources=[str(old / "Bracket.mprt")])
    r = svc.edit_existing("Bracket.mprt", [{"op": "set_dimension", "name": "D1@Boss-Extrude1", "value": 12}])
    assert r["applied"] and any(f["rule"] == "E018" for f in r["findings"])
    files = [x["file"] for x in r["refreshed"]]
    assert any("WIP/M_WIP.masm" in f for f in files) and not any("RELEASED" in f for f in files)
    assert r["dependents"]["report"]["count"] == 1
