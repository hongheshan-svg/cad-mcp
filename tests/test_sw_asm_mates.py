"""SolidWorks 装配：偏移/旋转/阵列坐标系配合（经装配参考坐标系）与 regenerate。"""
import json
import random

import pytest
from cylinder_station import ASSEMBLY
from test_sw_fake import sw_svc  # noqa: F401  (fixture)
from test_workflow import build_station

from cadmcp.adapters.solidworks.compile import compile_assembly
from cadmcp.adapters.solidworks.executor import SwExecutor
from cadmcp.ir import AssemblySpec
from cadmcp.ir.assembly_resolve import euler_xyz, matmul, rotate_xyz, same_frame, translate


def test_euler_roundtrip():
    rnd = random.Random(7)
    for _ in range(200):
        a = [rnd.uniform(-180, 180), rnd.uniform(-89, 89), rnd.uniform(-180, 180)]
        m = matmul(translate(1, 2, 3), rotate_xyz(*a))
        back = matmul(translate(1, 2, 3), rotate_xyz(*euler_xyz(m)))
        assert same_frame(m, back, 1e-9)
    for a in ([0, 90, 0], [30, -90, 10], [0, 0, 180]):  # 万向锁与 180°
        m = rotate_xyz(*a)
        assert same_frame(m, rotate_xyz(*euler_xyz(m)), 1e-9)


def _asm_ops(svc):
    build_station(svc)
    m = svc._models()[ASSEMBLY["name"]]
    files = {i.component: f"{i.component}.SLDPRT" for i in m.resolved.instances}
    return m.resolved, compile_assembly(AssemblySpec.model_validate(ASSEMBLY), m.resolved, files)


def test_compile_frames_only_when_needed(svc):
    asm, ops = _asm_ops(svc)
    mates = {o.args["instance"]: o.args for o in ops if o.op == "mate_coordinate"}
    assert mates["postL"]["frame"] is None and mates["mount"]["frame"] is None  # 无偏移：直接坐标系重合
    s1, s2 = mates["screw#1"]["frame"], mates["screw#2"]["frame"]  # 偏移 [12,12,0] + 阵列步距 [-24,0,0]
    assert s1["name"] == "MC_screw_1" and s2["name"] == "MC_screw_2"
    inst = {i.id: i for i in asm.instances}
    for iid, f in (("screw#1", s1), ("screw#2", s2)):  # 帧 = 实例位姿 × align 坐标系
        ref = inst[iid].source.references["CS_Head"]
        W = matmul(inst[iid].transform, matmul(translate(*ref.origin), rotate_xyz(*ref.rotate)))
        assert f["origin"] == pytest.approx([W[i][3] for i in range(3)], abs=1e-6)
        assert same_frame(W, matmul(translate(*f["origin"]), rotate_xyz(*f["rotate_deg"])), 1e-6)
    assert s2["origin"][0] - s1["origin"][0] == pytest.approx(-24)


def test_sw_offset_mates_use_assembly_csys(sw_svc, tmp_path):  # noqa: F811
    svc, ad = sw_svc
    build_station(svc)
    log = next((tmp_path / "ws").rglob(f"{ASSEMBLY['name']}.oplog.json"))
    recs = [r for r in json.loads(log.read_text(encoding="utf-8")) if r["op"] == "mate_coordinate"]
    res = {r["args"]["instance"]: r["result"] for r in recs}
    assert all(v["mate"] == "coordinate" for v in res.values()), res
    assert res["screw#1"]["via"] == "MC_screw_1" and res["mount"]["via"] is None
    csys = [c[1] for c in ad.sw.calls if c[0] == "CreateCoordinateSystemUsingNumericalValues"]
    assert len(csys) >= 2


def test_sw_regenerate(sw_svc, tmp_path, monkeypatch):  # noqa: F811
    svc, ad = sw_svc
    p = tmp_path / "x.SLDPRT"
    p.write_text("fake")
    r = ad.regenerate(p)
    assert r["status"] == "ok" and r["errors"] == [] and r["warnings"] == []
    assert "ForceRebuild3" in [c[0] for c in ad.sw.calls]

    def bad(self):
        self.readback["feature_errors"] = [{"feature": "Cut1", "code": 41}]
        self.readback["feature_warnings"] = [{"feature": "Fillet1", "code": 7}]
        raise RuntimeError("feature errors")
    monkeypatch.setattr(SwExecutor, "op_collect_feature_errors", bad)
    r = ad.regenerate(p)
    assert r["status"] == "errors" and r["errors"] == ["Cut1: error 41"] and r["warnings"] == ["Fillet1: warning 7"]
