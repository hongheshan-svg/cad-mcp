"""Creo TOOLKIT 装配：假 cadmcp_tk（按 docs/CREO_TOOLKIT.md §3.4 校验）+ 假 creopyson，经服务端到端建装配。"""
import sys

import pytest
from cylinder_station import ASSEMBLY, BASE_PLATE, MOUNT_PLATE
from fake_creopyson import install_fake_creopyson
from fake_toolkit import FakeToolkit

from cadmcp.adapters.creo.adapter import CreoAdapter
from cadmcp.adapters.creo.compile import compile_assembly_tk
from cadmcp.errors import CadError
from cadmcp.gateway.service import Target
from cadmcp.ir import AssemblySpec, PartSpec
from cadmcp.ir.assembly_resolve import resolve_assembly


@pytest.fixture
def creo():
    saved = sys.modules.get("creopyson")
    fake = install_fake_creopyson()
    yield fake
    if saved is None:
        sys.modules.pop("creopyson", None)
    else:
        sys.modules["creopyson"] = saved


@pytest.fixture
def tk():
    f = FakeToolkit()
    yield f
    f.close()


@pytest.fixture
def creo_svc(svc, creo, tk):
    ad = CreoAdapter("10.0", toolkit={"host": "127.0.0.1", "port": tk.port, "timeout": 5})
    svc.targets = {"creo-tk": Target("creo-tk", ad)}
    svc.active_target = "creo-tk"
    return svc


def _build_station(svc):
    svc.change_begin("CS")
    for d in (BASE_PLATE, MOUNT_PLATE):
        svc.build(PartSpec.model_validate(d))
    return svc.build(AssemblySpec.model_validate(ASSEMBLY))


def test_assembly_end_to_end(creo_svc, tk):
    r = _build_station(creo_svc)
    assert r["ok"], r
    adds = [a for c, a in tk.calls if c == "asm.add_component"]
    spec = AssemblySpec.model_validate(ASSEMBLY)
    names = [a["name"] for a in adds]
    assert len(adds) >= len(spec.components) and "base" in names and "cyl" in names
    assert all(len(a["transform"]) == 4 and all(len(row) == 4 for row in a["transform"]) for a in adds)
    assert all(a["file"].lower().endswith(".prt") for a in adds)
    # 库件没有原生模型 → 平台先经 TOOLKIT 建占位零件，再装入
    assert any("PH_" in a["file"].upper() or "ph_" in a["file"] for a in adds)
    by = {a["name"]: a for a in adds}
    assert by["postL"]["csys"] == {"ref": "CS_End0", "target": "base", "target_ref": "CS_PostL"}
    screws = [a for n, a in by.items() if n.startswith("screw")]
    assert screws and all(a["csys"] is None for a in screws)  # 有偏移：位姿已含在 transform，直接固定
    assert by["base"]["csys"] is None
    cmds = tk.cmds()
    assert cmds.index("asm.new") < cmds.index("asm.interference") < len(cmds) - 1 and cmds[-1] == "model.save"


def test_interference_readback_shape(creo, tk, tmp_path, svc):
    tk.interference = [{"components": ["mount", "cyl"], "volume_mm3": 12.345}]
    ad = CreoAdapter("10.0", toolkit={"host": "127.0.0.1", "port": tk.port, "timeout": 5})
    svc.targets = {"creo-tk": Target("creo-tk", ad)}
    svc.active_target = "creo-tk"
    _build_station(svc)
    # 与 SW 执行器回读同形：{"components": [...], "volume_mm3": v}
    m = svc._get(ASSEMBLY["name"])
    assert m is not None
    ops = compile_assembly_tk(AssemblySpec.model_validate(ASSEMBLY), resolve_assembly(
        AssemblySpec.model_validate(ASSEMBLY), svc._source_info), {c: "x.prt" for c in
                                                                   [c["id"] for c in ASSEMBLY["components"]]})
    assert ops[-2].op == "tk.asm.interference"
    from cadmcp.adapters.creo.toolkit import ToolkitClient, run_tk_ops
    res = run_tk_ops(ToolkitClient("127.0.0.1", tk.port, timeout=5), ops, tmp_path, "X")
    assert res["readback"]["interferences"] == [{"components": ["mount", "cyl"], "volume_mm3": 12.3}]


def test_failure_and_capabilities(creo, tk, tmp_path):
    ad = CreoAdapter("10.0", toolkit={"host": "127.0.0.1", "port": tk.port, "timeout": 5})
    assert ad.capabilities()["build_assembly"] == "partial"
    assert CreoAdapter("10.0").capabilities()["build_assembly"] == "none"
    with pytest.raises(CadError) as e:
        CreoAdapter("10.0").build_assembly(None, None, tmp_path, {})
    assert e.value.code == "CAPABILITY_UNSUPPORTED"


def test_add_component_failure_is_error(creo_svc, tk):
    tk.fail.add("asm.add_component")
    with pytest.raises(CadError) as e:
        _build_station(creo_svc)
    assert e.value.code == "NATIVE_ERROR" and "asm.add_component" in e.value.message
    assert "asm.interference" not in tk.cmds()


def test_missing_component_file_unsupported(svc):
    _build_station(svc)  # 模拟目标上解析装配
    spec = AssemblySpec.model_validate(ASSEMBLY)
    res = resolve_assembly(spec, svc._source_info)
    with pytest.raises(CadError) as e:
        compile_assembly_tk(spec, res, {})
    assert e.value.code == "CAPABILITY_UNSUPPORTED" and "library_root" in (e.value.hint or "")
    parts = {c.id: f"C:/w/{c.id}.prt" for c in spec.components if c.source.startswith("part:")}
    ops = compile_assembly_tk(spec, res, parts, library_root="C:/lib")
    assert any(o.args.get("file", "").endswith("PRF-4040.prt") for o in ops)
