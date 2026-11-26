import json

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
    assert r["used_by"] == ["Bracket.mdrw"] and r["mass_kg"] == 0.75


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
    assert r["refreshed"][0]["file"] == "Bracket.mdrw" and r["refreshed"][0]["dangling"] == 0
    c = svc.commit("B")
    assert "D1@Boss-Extrude1 10→12" in c["revision_entry"]["description"]
    # 回写：默认不允许
    w = svc.writeback()
    assert {f["file"]: f["status"] for f in w["files"]} == {"Bracket.mdrw": "denied", "Bracket.mprt": "denied"}
    svc.policy.writeback_allow.append(src.resolve())
    w = svc.writeback()
    assert [f["status"] for f in w["files"]] == ["ready", "ready"] and not w["confirmed"]  # 零件 + 已更新的工程图
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
    assert f == {"Bracket.mprt": "conflict", "Bracket.mdrw": "ready"} and not w["ok"]
    assert w["blocked_by"] == ["Bracket.mprt"] and not w["written"]  # 一致性：工程图也不写


def test_suppress_dangling_blocks_commit(legacy):
    svc, _ = legacy
    r = svc.edit_existing("Bracket.mprt", [{"op": "suppress", "feature": "Fillet1"}])
    assert r["applied"] and any(f["rule"] == "E010" for f in r["findings"])
    with pytest.raises(CadError):
        svc.commit("B")
    svc.rollback(r["rollback_to"])
