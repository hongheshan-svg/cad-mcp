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

def test_sw_native_shaft_dimensions(sw_svc):
    """轴类尺寸原生标注：选两条边线（SelectByID2 9 参）+ AddDimension2（3 参），坐标在图纸内。"""
    svc, ad = sw_svc
    svc.change_begin("SHD")
    svc.build(PartSpec.model_validate(SHAFT))
    svc.build(DrawingSpec.model_validate({"model": "SH1", "title_block": {"designer": "AI"}}))
    calls = ad.sw.calls
    adds = [c[1] for c in calls if c[0] == "AddDimension2"]
    assert len(adds) >= 8 and all(len(a) == 3 for a in adds)
    edge_sel = [c[1] for c in calls if c[0] == "SelectByID2" and c[1][1] == "EDGE"]
    assert len(edge_sel) >= 2 * len(adds) and all(len(a) == 9 for a in edge_sel)
    texts = [c[1][1] for c in calls if c[0] == "SetText" and len(c[1]) == 2]
    assert "<MOD-DIAM><DIM>" in texts   # 直径符号文字覆盖
    fits = [c[1] for c in calls if c[0] == "SetFitValues"]
    assert ("", "k6") in [tuple(f) for f in fits]   # 配合公差交给 SW 自带 ISO 286 表（代号 + 叠写偏差）
    for a in adds + [(e[2], e[3]) for e in edge_sel]:
        assert 0 < a[0] < 0.5 and 0 < a[1] < 0.5


def test_sw_overall_dimension_native(sw_svc):
    svc, ad = sw_svc
    svc.change_begin("PL")
    svc.build(PartSpec.model_validate(PLATE_PRECISION))
    svc.build(DrawingSpec.model_validate({"model": "PP", "title_block": {"designer": "AI"}}))
    adds = [c[1] for c in ad.sw.calls if c[0] == "AddDimension2"]
    assert len(adds) >= 2 and all(len(a) == 3 for a in adds)

def _cosmetic_spec():
    return PartSpec.model_validate({
        "name": "CT1", "part_no": "CT-01", "title": "装饰螺纹板", "material": "AL6061-T6",
        "base": {"type": "extrude", "thickness": 10, "profile": {"shape": "rect", "width": 100, "height": 60}},
        "features": [
            {"feature": "hole", "name": "Tap", "face": "top", "type": "tapped", "size": "M6", "positions": [[10, 10], [30, 10]]},
            {"feature": "fillet", "name": "R2", "edges": "outer_vertical", "size": 2},
            {"feature": "chamfer", "name": "C1", "edges": "top_outer", "size": 1},
        ],
        "references": [
            {"name": "MP_Mid", "face": "top"},
            {"name": "AX_Tap", "hole": "Tap", "index": 0},
            {"name": "PT_Center", "origin": [50, 30, 5]},
        ],
    })


def _part_log(tmp_path, name):
    lg = next((tmp_path / "ws").rglob(f"{name}.oplog.json"))
    return {r["op"]: r for r in json.loads(lg.read_text(encoding="utf-8"))}


def test_sw_cosmetic_thread_and_named_refs(sw_svc, tmp_path):
    svc, ad = sw_svc
    svc.change_begin("CT1")
    svc.build(_cosmetic_spec())
    names = [c[0] for c in ad.sw.calls]
    # 螺纹孔 2 个实例 -> 2 次 InsertCosmeticThread3，实参个数由 ARITY 核对
    assert names.count("InsertCosmeticThread3") == 2
    edge_sel = [c[1] for c in ad.sw.calls if c[0] == "SelectByID2" and c[1][1] == "EDGE"]
    assert len(edge_sel) == 2
    # 命名参考：面/轴/点都创建并改名
    assert "InsertRefPlane" in names and "InsertAxis2" in names and "InsertReferencePoint" in names
    renamed = [v for k, v in ad.sw.sets if k.endswith(".Name")]
    for n in ("MP_Mid", "AX_Tap", "PT_Center"):
        assert n in renamed, renamed
    log = _part_log(tmp_path, "CT1")
    assert log["cosmetic_thread"]["ok"] and log["ref_plane_coincident"]["ok"]


def test_sw_select_edges_rules_and_finish(sw_svc, tmp_path):
    svc, ad = sw_svc
    svc.change_begin("CT1")
    svc.build(_cosmetic_spec())
    sel_calls = [c[1] for c in ad.sw.calls if c[0] == "EdgeSelect4"]
    # outer_vertical：4 条竖边（孔内环不算）；top_outer：顶面外环 4 条边
    verts = [c for c in sel_calls if c[0][0][2] != c[0][1][2]]
    tops = [c for c in sel_calls if c[0][0][2] == c[0][1][2] == 0.01]
    assert len(verts) == 4 and len(tops) == 4
    assert all(c[0][0] != (0.05, 0.03, 0.01) for c in sel_calls)  # 孔内环未被选
    names = [c[0] for c in ad.sw.calls]
    assert "FeatureFillet3" in names and "InsertFeatureChamfer" in names


def _oplog(tmp_path, name):
    return json.loads(next((tmp_path / "ws").rglob(f"{name}.oplog.json")).read_text(encoding="utf-8"))


def test_sw_shaft_removed_sections_and_dims(sw_svc, tmp_path):
    """移出断面图：主视图竖直剖切线 → CreateSectionViewAt5（7 参）→ 解除对齐移到规划位置；
    断面图上槽宽（N9 后缀覆盖）与 d−t（圆弧条件"最大"）走原生 AddDimension2。"""
    svc, ad = sw_svc
    svc.change_begin("SHS")
    svc.build(PartSpec.model_validate(SHAFT))
    svc.build(DrawingSpec.model_validate({"model": "SH1", "title_block": {"designer": "AI"}}))
    calls = ad.sw.calls
    secs = [c[1] for c in calls if c[0] == "CreateSectionViewAt5"]
    assert len(secs) == 2 and len({s[3] for s in secs}) == 2 and all(len(s[3]) == 1 for s in secs)
    assert sum(1 for c in calls if c[0] == "RemoveAlignment") == 2
    log = _oplog(tmp_path, "SH1_DRW")
    sv = [r for r in log if r["op"] == "section_view"]
    assert len(sv) == 2 and all(r["ok"] for r in sv)
    for r in sv:  # 剖切线竖直、跨过主视图
        (ax, ay), (bx, by) = r["args"]["points"]
        assert ax == bx and ay > by
    sec_dims = [r for r in log if r["op"] == "shaft_dimension" and r["args"]["view"].startswith("section:")]
    assert len(sec_dims) == 4 and all(r["ok"] for r in sec_dims)
    assert any(c[0] == "SetFitValues" and tuple(c[1]) == ("N9", "") for c in calls)  # 槽宽 N9 走 SW 配合公差
    assert sum(1 for c in calls if c[0] == "SetArcEndCondition") == 2
    # 倒角/挡圈槽引出标注不再退回视图中心：有规划位置
    notes = [r for r in log if r["op"] == "leader_note" and r["args"]["ref"] in ("chamfer", "G1")]
    assert len(notes) == 2 and all(r["args"].get("text_pos") and r["args"].get("anchor") for r in notes)


def test_sw_flat_pattern_dims(sw_svc, tmp_path):
    """展开图总宽/总高走原生 AddDimension2（规划层给出外轮廓边上的点），折弯说明放到规划位置。
    （零件建模的边线法兰另行实现，这里直接对规划出图。）"""
    from cadmcp.detailing import plan_part_drawing
    from cadmcp.ir.resolve import resolve_part
    _, ad = sw_svc
    sm = {"name": "SMB", "part_no": "SM-01", "title": "钣金支架", "material": "SPCC", "category": "sheet_metal",
          "base": {"type": "sheet_metal", "profile": {"shape": "rect", "width": 120, "height": 80}, "thickness": 2,
                   "flanges": [{"edge": "front", "length": 40}, {"edge": "back", "length": 40}]}}
    r = resolve_part(PartSpec.model_validate(sm))
    plan = plan_part_drawing(DrawingSpec.model_validate({"model": "SMB", "views": [
        {"type": "front"}, {"type": "top"}, {"type": "flat_pattern"}]}), r)
    plan.name = "SMB_DRW"
    out = tmp_path / "ws"
    out.mkdir(exist_ok=True)
    ad.build_drawing(plan, tmp_path / "smb.sldprt", out, r)
    log = _oplog(tmp_path, "SMB_DRW")
    flat = [r for r in log if r["op"] == "overall_dimension" and r["args"]["view"] == "flat_pattern"]
    assert len(flat) == 4 and all(r["ok"] and r["args"]["pts"] for r in flat)
    bends = [r for r in log if r["op"] == "leader_note" and r["args"]["ref"].startswith("bend")]
    assert len(bends) == 2 and all(r["args"].get("text_pos") for r in bends)


def test_sw_gear_part_and_drawing(sw_svc):
    """齿轮：渐开线齿廓折线草图拉伸；出图端面视图 + 全剖主视图 + 参数表，规则检查通过。"""
    from test_gear import GEAR
    svc, ad = sw_svc
    svc.change_begin("GR")
    svc.build(PartSpec.model_validate(GEAR))
    r = svc.build(DrawingSpec.model_validate({"model": "GR1", "title_block": {"designer": "AI"}}))
    assert r["rules"]["passed"], r["rules"]
    calls = [c[0] for c in ad.sw.calls]
    assert "CreateSectionViewAt5" in calls and "InsertTableAnnotation2" in calls


def test_sw_broken_shaft_view(sw_svc):
    """细长轴：主视图插入断裂线并 BreakView，规则检查通过。"""
    from test_broken_view import LS
    svc, ad = sw_svc
    svc.change_begin("LS")
    svc.build(PartSpec.model_validate(LS))
    r = svc.build(DrawingSpec.model_validate({"model": "LS1", "title_block": {"designer": "AI"}}))
    assert r["rules"]["passed"], r["rules"]
    calls = [c[0] for c in ad.sw.calls]
    assert "InsertBreak3" in calls and "BreakView" in calls


def test_sw_sleeve_full_section(sw_svc):
    """套类：内孔切除 + 主视图原位全剖（CreateBreakOutSection），规则检查通过。"""
    from test_shaft_bores import SL
    svc, ad = sw_svc
    svc.change_begin("SL")
    svc.build(PartSpec.model_validate(SL))
    r = svc.build(DrawingSpec.model_validate({"model": "SL1", "title_block": {"designer": "AI"}}))
    assert r["rules"]["passed"], r["rules"]
    assert "CreateBreakOutSection" in [c[0] for c in ad.sw.calls]


def test_sw_sleeve_bore_datum(sw_svc):
    """套类精度：内孔基准 A、外圆跳动、内孔粗糙度走原生符号，规则检查无 G004。"""
    from test_shaft_bores import SL
    svc, ad = sw_svc
    svc.change_begin("SL")
    svc.build(PartSpec.model_validate({**SL, "auto_precision": True}))
    r = svc.build(DrawingSpec.model_validate({"model": "SL1", "title_block": {"designer": "AI"}}))
    assert r["rules"]["passed"], r["rules"]
    calls = [c[0] for c in ad.sw.calls]
    assert "InsertDatumTag2" in calls or any("Datum" in c for c in calls)


def test_sw_shaft_end_holes(sw_svc):
    """轴端螺孔：底孔切除 + 内螺纹装饰线 + 引线标注，规则检查通过。"""
    from test_end_holes import SP
    svc, ad = sw_svc
    svc.change_begin("EH")
    svc.build(PartSpec.model_validate(SP))
    r = svc.build(DrawingSpec.model_validate({"model": "EH1", "title_block": {"designer": "AI"}}))
    assert r["rules"]["passed"], r["rules"]
    assert "InsertCosmeticThread3" in [c[0] for c in ad.sw.calls]


def test_sw_sleeve_hub_keyway(sw_svc):
    """套类内键槽：键槽切除、左视图（*Back 转 180°）+ 槽宽/d+t2 + 孔径引线，规则检查通过。"""
    from test_shaft_bores import SL
    svc, ad = sw_svc
    spec = {**SL, "base": {**SL["base"], "bores": [{"diameter": 30, "tolerance": "H7", "keyway": True}]}}
    svc.change_begin("SLK")
    svc.build(PartSpec.model_validate(spec))
    r = svc.build(DrawingSpec.model_validate({"model": "SL1", "title_block": {"designer": "AI"}}))
    assert r["rules"]["passed"], r["rules"]


def test_sw_grinding_undercut_detail(sw_svc):
    """越程槽：环槽切除 + 局部放大图（CreateDetailViewAt4）+ 放大图内尺寸，规则检查通过。"""
    from test_grinding_undercut import GR
    svc, ad = sw_svc
    svc.change_begin("GR")
    svc.build(PartSpec.model_validate(GR))
    r = svc.build(DrawingSpec.model_validate({"model": "GR1", "title_block": {"designer": "AI"}}))
    assert r["rules"]["passed"], r["rules"]
    assert [c for c in ad.sw.calls if c[0] == "CreateDetailViewAt4"]


def test_sw_heat_treatment_chain_line(sw_svc):
    """局部淬火：视图草图粗点画线（SetLineStyle/SetLineWidth），规则检查通过。"""
    from test_heat_treatment import HT
    svc, ad = sw_svc
    svc.change_begin("HT")
    svc.build(PartSpec.model_validate(HT))
    r = svc.build(DrawingSpec.model_validate({"model": "HT1", "title_block": {"designer": "AI"}}))
    assert r["rules"]["passed"], r["rules"]
    assert len([c for c in ad.sw.calls if c[0] == "SetLineStyle"]) == 2


def test_sw_hollow_shaft_keyway(sw_svc):
    """空心轴键槽：键槽切除（Top Plane 翻转）+ 原位全剖 + 断面图，规则检查通过。"""
    from test_shaft_bores import SL
    svc, ad = sw_svc
    spec = {**SL, "features": [{"feature": "keyway", "name": "KW", "step": 1, "length": 28}]}
    svc.change_begin("HK")
    svc.build(PartSpec.model_validate(spec))
    r = svc.build(DrawingSpec.model_validate({"model": "SL1", "title_block": {"designer": "AI"}}))
    assert r["rules"]["passed"], r["rules"]


def test_sw_flange_holes(sw_svc):
    """端盖均布孔：分布圆切孔 + 左视图引线标注，规则检查通过。"""
    from test_flange_holes import EC
    svc, ad = sw_svc
    svc.change_begin("EC")
    svc.build(PartSpec.model_validate(EC))
    r = svc.build(DrawingSpec.model_validate({"model": "EC1", "title_block": {"designer": "AI"}}))
    assert r["rules"]["passed"], r["rules"]


def test_sw_shoulder_fillets(sw_svc):
    from test_shoulder_fillet import SF
    svc, ad = sw_svc
    svc.change_begin("SF")
    svc.build(PartSpec.model_validate(SF))
    assert [c for c in ad.sw.calls if c[0] == "FeatureFillet3"]


def test_sw_executor_cross_holes(sw_svc):
    from test_cross_holes import XS
    svc, ad = sw_svc
    svc.change_begin("XS")
    svc.build(PartSpec.model_validate(XS))
    svc.build(DrawingSpec.model_validate({"model": "XS1", "title_block": {"designer": "AI"}}))
    cuts = [c[1] for c in ad.sw.calls if c[0] == "FeatureCut4"]
    assert len(cuts) == 4  # 键槽 + 通孔两半 + 盲螺孔
