"""用假 COM 对象跑通 SolidWorks 执行器全流程（任意平台）。"""
import json

import pytest
from fake_sw import FakeConstants, FakeSw, install_fake_win32
from test_core import L_BRACKET, PLATE_PRECISION, SHAFT
from test_workflow import build_station

from cadmcp.adapters.solidworks.adapter import DEFAULT_MATERIAL_MAP, SolidWorksAdapter
from cadmcp.gateway.service import CadService, Target
from cadmcp.ir import DrawingSpec, PartSpec


@pytest.fixture
def sw_svc(tmp_path, monkeypatch):
    install_fake_win32()
    ad = SolidWorksAdapter.__new__(SolidWorksAdapter)
    ad.version, ad.revision = "2024", "32.1.0"
    ad.ext = {"part": ".SLDPRT", "assembly": ".SLDASM", "drawing": ".SLDDRW"}
    ad.sw, ad.constants = FakeSw(), FakeConstants()
    ad.templates, ad.material_map = {}, DEFAULT_MATERIAL_MAP
    ad.library_root, ad.holes_via = str(tmp_path / "lib"), "cut"
    (tmp_path / "lib").mkdir()
    monkeypatch.setattr(SolidWorksAdapter, "file_version", lambda self, p: ("solidworks", "2024"))
    svc = CadService.from_config(None, None, str(tmp_path / "ws"))
    svc.targets["fake-sw"] = Target("fake-sw", ad)
    svc.select_target("fake-sw")
    return svc, ad


def _oplogs(svc):
    work = next(svc.session.root.glob("work")) if hasattr(svc.session, "root") else None
    return work


def test_sw_executor_station(sw_svc, tmp_path):
    svc, ad = sw_svc
    build_station(svc)
    logs = list((tmp_path / "ws").rglob("*.oplog.json"))
    assert len(logs) >= 6  # 2 零件 + 1 装配 + 3 图纸
    bad = []
    for lg in logs:
        for r in json.loads(lg.read_text(encoding="utf-8")):
            # 允许已知占位（P1 实现）与假对象无法模拟的库文件；其余失败都是执行器 bug
            if not r["ok"] and "P1" not in r["error"] and "skipped" not in r["error"] \
                    and "hole_wizard" not in r["error"] and "not implemented" not in r["error"] :
                bad.append((lg.name, r["op"], r["error"]))
    assert not bad, bad
    names = [c[0] for c in ad.sw.calls]
    assert "FeatureExtrusion3" in names and "FeatureCut4" in names and "AutoDimension" in names


def test_sw_executor_slots_inner_face(sw_svc, tmp_path):
    svc, ad = sw_svc
    svc.change_begin("LB")
    svc.build(PartSpec.model_validate(L_BRACKET))
    svc.build(DrawingSpec.model_validate({"model": "LB", "title_block": {"designer": "AI"}}))
    names = [c[0] for c in ad.sw.calls]
    assert names.count("CreateSketchSlot") == 2


def test_missing_library_gets_placeholder(sw_svc):
    svc, ad = sw_svc
    build_station(svc)
    asm = svc.get_model("CS01-000_CylStation")
    a002 = [f for f in asm["findings"] if f["rule"] == "A002"]
    assert {f["target"] for f in a002} >= {"PRF-4040-L250", "CYL-SC32x50"}


def test_sw_executor_shaft(sw_svc):
    svc, ad = sw_svc
    svc.change_begin("SH")
    svc.build(PartSpec.model_validate(SHAFT))
    svc.build(DrawingSpec.model_validate({"model": "SH1", "title_block": {"designer": "AI"}}))
    names = [c[0] for c in ad.sw.calls]
    assert names.count("FeatureRevolve2") == 2           # 轴体 + 挡圈槽
    cuts = [c[1] for c in ad.sw.calls if c[0] == "FeatureCut4"]
    assert len(cuts) == 2 and cuts[0][24] == pytest.approx((10 - 3.5) / 1000)  # 起始偏移 d/2-t1（m）


def test_sw_executor_precision(sw_svc):
    svc, ad = sw_svc
    svc.change_begin("PP")
    svc.build(PartSpec.model_validate(PLATE_PRECISION))
    svc.build(DrawingSpec.model_validate({"model": "PP", "title_block": {"designer": "AI"}}))
    notes = [c[1][0] for c in ad.sw.calls if c[0] == "InsertNote"]
    assert any("其余" in n for n in notes) and not any("Φ0.05 A B" in n for n in notes)   # 公差走原生框格
    names = [c[0] for c in ad.sw.calls]
    assert names.count("InsertDatumTag2") == 2 and ("SetLabel", ("A",)) in ad.sw.calls
    vals = [c[1] for c in ad.sw.calls if c[0] == "SetFrameValues2"]
    assert (1, "0.05", "", "A", "B", "") in vals
    sym = [c[1] for c in ad.sw.calls if c[0] == "SetFrameSymbols2"]
    assert any(s[2] is True for s in sym)                                           # 位置度 Φ 公差带
    assert "InsertSurfaceFinishSymbol3" in names
    sf = next(c[1] for c in ad.sw.calls if c[0] == "InsertSurfaceFinishSymbol3")
    assert sf[11].startswith("Ra ")


def test_sw_gdt_falls_back_without_constants(sw_svc):
    svc, ad = sw_svc

    class NoGcs(FakeConstants):
        def __getattr__(self, name):
            if name.startswith(("swGcs", "swSF")):
                raise AttributeError(name)
            return super().__getattr__(name)

    ad.constants = NoGcs()
    svc.change_begin("PP")
    svc.build(PartSpec.model_validate(PLATE_PRECISION))
    d = svc.build(DrawingSpec.model_validate({"model": "PP", "title_block": {"designer": "AI"}}))
    notes = [c[1][0] for c in ad.sw.calls if c[0] == "InsertNote"]
    assert any("Φ0.05 A B" in n for n in notes)                                     # 退回注释
    assert "InsertGtol" not in [c[0] for c in ad.sw.calls]
    assert any(f["rule"] == "D019" for f in d["rules"]["findings"])


def test_sw_executor_weldment(sw_svc):
    import sys
    sys.path.insert(0, "examples")
    from weld_base import weld_base
    svc, ad = sw_svc
    svc.change_begin("WB")
    r = svc.build(PartSpec.model_validate(weld_base()))
    assert r["regen"]["status"] != "errors", r["regen"]  # 装饰螺纹等非关键项为警告
    svc.build(DrawingSpec.model_validate({"model": "WB1", "title_block": {"designer": "AI"}}))
    names = [c[0] for c in ad.sw.calls]
    ext = [c[1] for c in ad.sw.calls if c[0] == "FeatureExtrusion3"]
    assert names.count("InsertWeldmentFeature") == 1
    assert len(ext) == 4 + 8 + 5 and all(a[17] is False for a in ext)  # 每个实体独立（Merge=False）
    post = ext[0]  # P1：立柱从 z=10 起拉伸 480
    assert post[21] == pytest.approx(0.010) and post[5] == pytest.approx(0.480)
    cuts = [c[1] for c in ad.sw.calls if c[0] == "FeatureCut4"]
    assert cuts and all(a[3] == FakeConstants().swEndCondBlind for a in cuts)  # 板孔不打穿到杆件
    assert names.count("InsertWeldSymbol3") == 2                       # 两组焊缝：周围焊 + 双面焊
    texts = [c[1] for c in ad.sw.calls if c[0] == "SetText"]
    assert [a[:3] for a in texts].count((True, "3", "<WELD-FILLET>")) == 2
    assert (False, "3", "<WELD-FILLET>", "4处", "", 0) in texts             # 双面焊另一侧
    assert ("SetPeripheral", (True,)) in ad.sw.calls and ("SetProcess", (True, "135")) in ad.sw.calls
    assert names.count("InsertWeldmentTable") == 1


def test_sw_edit_existing(sw_svc, tmp_path):
    svc, ad = sw_svc
    src = tmp_path / "designs"
    src.mkdir()
    (src / "Old.SLDPRT").write_bytes(b"sw-binary")
    svc.policy.read_allow.append(src.resolve())
    svc.change_begin("OLD", sources=[str(src / "Old.SLDPRT")])
    info = svc.inspect_existing("Old.SLDPRT")
    assert {"name": "D1@F2", "value": 10.0, "unit": "mm", "feature": "F2", "driven": False} in info["dimensions"]
    r = svc.edit_existing("Old.SLDPRT", [{"op": "set_dimension", "name": "D1@F2", "value": 12},
                                         {"op": "suppress", "feature": "F3"}])
    assert r["applied"], r
    sets = [c[1] for c in ad.sw.calls if c[0] == "SetSystemValue3"]
    assert sets and sets[0][0] == pytest.approx(0.012)
    names = [c[0] for c in ad.sw.calls]
    assert "EditSuppress2" in names and "Save3" in names and "ForceRebuild3" in names


def test_sw_replace_component_op():
    from cadmcp.adapters.solidworks.executor import SwExecutor
    install_fake_win32()
    sw = FakeSw()
    ex = SwExecutor(sw, FakeConstants())
    ex.model = sw.OpenDoc6("Station.SLDASM", 1, 1, "", 0, 0)
    ex.op_replace_component("CYL-1", r"D:\lib\CYL-SC40x50.SLDPRT", False)
    call = next(c for c in sw.calls if c[0] == "ReplaceComponents2")
    assert call[1] == (r"D:\lib\CYL-SC40x50.SLDPRT", "", False, 0, True)  # 重新附加配合


def test_sw_hole_table_and_local_section(sw_svc):
    from test_hole_table import GRID
    from test_section import BAR
    svc, ad = sw_svc
    svc.change_begin("G")
    svc.build(PartSpec.model_validate(GRID))
    svc.build(DrawingSpec.model_validate({"model": GRID["name"], "title_block": {"designer": "AI"}}))
    tbl = [c[1] for c in ad.sw.calls if c[0] == "InsertTableAnnotation2"]
    assert len(tbl) == 1 and tbl[0][5:] == (22, 4)                    # 表头 + 21 行，4 列
    notes = [c[1][0] for c in ad.sw.calls if c[0] == "InsertNote"]
    assert "A1" in notes and "X0 Y0" in notes and "孔表" in notes
    svc.change_begin("B")
    svc.build(PartSpec.model_validate(BAR))
    d = svc.build(DrawingSpec.model_validate({"model": BAR["name"], "title_block": {"designer": "AI"}}))
    bo = [c[1] for c in ad.sw.calls if c[0] == "CreateBreakOutSection"]
    assert bo and bo[0][0] == pytest.approx(0.020)
    assert not [f for f in d["rules"]["findings"] if f["rule"] == "D016"]


def test_sw_revision_table_and_leader_attach(sw_svc):
    from test_section import BAR
    svc, ad = sw_svc
    svc.change_begin("R")
    svc.build(PartSpec.model_validate(BAR))
    revs = [{"rev": "A", "date": "2026-01-01", "description": "初版", "by": "AI"},
            {"rev": "B", "date": "2026-02-01", "description": "改孔径", "by": "AI"}]
    svc.build(DrawingSpec.model_validate({"model": BAR["name"], "title_block": {"designer": "AI"}, "revisions": revs}))
    calls = ad.sw.calls
    rt = [c[1] for c in calls if c[0] == "InsertRevisionTable2"]
    assert len(rt) == 1 and len(rt[0]) == 5
    assert [c[1][0] for c in calls if c[0] == "AddRevision"] == ["B", "A"]           # 最新在上
    assert not any(c[0] == "InsertNote" and str(c[1][0]).startswith("更改记录") for c in calls)
    # 有 anchor 的引出标注：先选边线（SelectByID2 九个参数）再插入注释
    sel = [c[1] for c in calls if c[0] == "SelectByID2" and c[1][1] == "EDGE"]
    assert sel and all(len(a) == 9 for a in sel)


def test_sw_revision_table_falls_back_to_note():
    from cadmcp.adapters.solidworks.executor import SwExecutor as Executor
    ex = Executor.__new__(Executor)
    ex.readback = {}

    class Ann:
        def SetPosition2(self, *a):
            pass

    class Note:
        def GetAnnotation(self):
            return Ann()

    class M:
        def InsertRevisionTable2(self, *a):
            return None

        def InsertNote(self, t):
            ex.readback["note"] = t
            return Note()
    ex.model = M()
    ex.c = object()
    r = ex.op_revision_table((10, 5), ["版本"], [14], [["A"]], "更改记录：\nA")
    assert r == {"fallback": True} and ex.readback["note"].startswith("更改记录")
    assert "revision_rows" not in ex.readback


def test_d020_revision_rows_mismatch(sw_svc):
    from cadmcp.detailing import check_drawing
    from test_section import BAR
    svc, ad = sw_svc
    svc.change_begin("D")
    svc.build(PartSpec.model_validate(BAR))
    revs = [{"rev": "A", "date": "2026-01-01", "description": "初版", "by": "AI"}]
    svc.build(DrawingSpec.model_validate({"model": BAR["name"], "title_block": {"designer": "AI"}, "revisions": revs}))
    plan = svc._get(BAR["name"] + "_DRW").resolved
    assert not [f for f in check_drawing(plan, {"revision_rows": 1}) if f["rule"] == "D020"]
    bad = [f for f in check_drawing(plan, {"revision_rows": 0}) if f["rule"] == "D020"]
    assert bad and bad[0]["severity"] == "error"
    assert not [f for f in check_drawing(plan, {}) if f["rule"] == "D020"]
