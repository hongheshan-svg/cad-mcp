"""SW 钣金缺口：外形草图完全定义（dimension_profile）与边线法兰（InsertSheetMetalEdgeFlange2）。"""
import json

import pytest
from fake_sw import ARITY
from test_core import L_BRACKET
from test_sw_fake import sw_svc  # noqa: F401  (fixture)

from cadmcp.adapters.solidworks.compile import compile_part
from cadmcp.ir import PartSpec
from cadmcp.ir.resolve import resolve_part
from cadmcp.sketch_plan import dof_check, profile_dimension_plan

SM = {"name": "SMU", "part_no": "SM-U", "title": "U 形支架", "material": "SPCC", "category": "sheet_metal",
      "surface_treatment": "镀锌", "parameters": {"W": 100},
      "base": {"type": "sheet_metal", "profile": {"shape": "rect", "width": "W", "height": 60, "center": [50, 30]},
               "thickness": 2, "flanges": [{"edge": "front", "length": 30},
                                           {"edge": "back", "length": 25, "direction": "down"}]}}


def _profile(prof):
    spec = PartSpec.model_validate({"name": "X", "part_no": "X", "title": "X", "material": "Q235",
                                    "base": {"type": "extrude", "thickness": 5, "profile": prof}})
    return resolve_part(spec).profile


@pytest.mark.parametrize("prof", [
    {"shape": "rect", "width": 100, "height": 60},
    {"shape": "rect", "width": 100, "height": 60, "center": [20, 0]},
    {"shape": "rect", "width": 100, "height": 60, "center": [20, -5], "corner_radius": 5},
    {"shape": "circle", "diameter": 40},
    {"shape": "circle", "diameter": 40, "center": [10, 15]},
    {"shape": "slot", "length": 60, "width": 12, "angle": 30},
    {"shape": "polygon", "points": [[0, 0], [80, 0], [80, 10], [10, 10], [10, 60], [0, 60]]},
    {"shape": "polygon", "points": [[0, 0], [50, 0], [30, 40]]},
], ids=["rect", "rect-off", "rect-r", "circle", "circle-off", "slot", "L", "triangle"])
def test_profile_plan_fully_defined(prof):
    pr = _profile(prof)
    plan = profile_dimension_plan(pr, {})
    assert dof_check(pr, plan) == 0
    assert all(d["value"] > 0 for d in plan["dims"])  # 0 值一律用关系代替尺寸
    kinds = {r["kind"] for r in plan["relations"]}
    assert kinds <= {"coincident", "same_x", "same_y", "horizontal", "vertical"}


def test_profile_plan_links_global_variables():
    spec = PartSpec.model_validate(L_BRACKET)
    ops = compile_part(spec, resolve_part(spec))
    dp = next(o for o in ops if o.op == "dimension_profile")
    links = {d["name"]: d["link"] for d in dp.args["dims"]}
    assert links["p1_x"] == '"A"' and links["p4_y"] == '"B"' and links["p2_y"] == '"t"'
    assert dof_check(resolve_part(spec).profile, dp.args) == 0


def test_flange_edge_points_and_extent():
    r = resolve_part(PartSpec.model_validate(SM))
    front, back = r.bends
    assert front["edge_point"] == (50.0, 0.0, 2.0) and front["expect_z"] == (0.0, 30.0)
    assert back["edge_point"] == (50.0, 60.0, 0.0) and back["expect_z"] == (-23.0, 2.0)
    assert (r.bbox.zmin, r.bbox.zmax) == (-23.0, 30.0)


def test_sw_sheet_metal_build(sw_svc, tmp_path):  # noqa: F811
    svc, ad = sw_svc
    svc.change_begin("SMU")
    svc.build(PartSpec.model_validate(SM))
    calls = ad.sw.calls
    ef = [a for n, a in calls if n == "InsertSheetMetalEdgeFlange2"]
    assert len(ef) == 2 and all(len(a) == ARITY["InsertSheetMetalEdgeFlange2"] for a in ef)
    assert abs(ef[0][6] - 0.030) < 1e-12 and abs(ef[1][6] - 0.025) < 1e-12  # 长度（米）
    edge_sel = [a for n, a in calls if n == "SelectByID2" and a[1] == "EDGE"]
    assert [tuple(round(v, 6) for v in a[2:5]) for a in edge_sel] == [(0.05, 0.0, 0.002), (0.05, 0.06, 0.0)]
    assert len([a for n, a in calls if n == "AddDimension2"]) >= 4  # 宽、高、中心 x/y
    lg = next((tmp_path / "ws").rglob("SMU.oplog.json"))
    log = {r["op"]: r for r in json.loads(lg.read_text(encoding="utf-8"))}
    assert log["dimension_profile"]["ok"], log["dimension_profile"]
    assert log["sheet_metal_edge_flange"]["ok"]
    # 宽度链接全局变量 W：退出草图后写方程式 "<尺寸名>@SK_Base" = "W"
    eqs = [a[1] for n, a in calls if n == "Add2" and len(a) == 3 and isinstance(a[1], str)]
    assert any(e.endswith('@SK_Base" = "W"') for e in eqs), eqs
    # 假实体包围盒（0..10 mm）≠ 规划（-23..30）：核对项只记警告，不中断
    assert not log["check_body_extent"]["ok"] and "不符" in log["check_body_extent"]["error"]
