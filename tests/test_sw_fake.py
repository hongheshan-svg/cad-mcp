"""用假 COM 对象跑通 SolidWorks 执行器全流程（任意平台）。"""
import json

import pytest
from fake_sw import FakeConstants, FakeSw, install_fake_win32
from test_core import L_BRACKET
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
