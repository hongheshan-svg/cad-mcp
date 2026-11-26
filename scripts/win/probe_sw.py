"""SolidWorks 实机探针：逐级验证，生成 probe_report_sw.json 发回开发组。

用法（在仓库根目录、SolidWorks 2024 已安装并授权）：
    uv run python scripts/win/probe_sw.py --version 2024 [--node-config D:/cadmcp/node.yaml]
"""
from __future__ import annotations

import argparse
import json
import platform
import sys
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "examples"))

report: dict = {"time": time.strftime("%Y-%m-%d %H:%M:%S"), "python": sys.version.split()[0],
                "os": platform.platform(), "steps": []}


def step(name):
    def deco(fn):
        def run(*a, **kw):
            t0 = time.perf_counter()
            rec = {"step": name}
            try:
                rec["result"] = fn(*a, **kw)
                rec["ok"] = True
            except Exception as e:  # noqa: BLE001
                rec.update(ok=False, error=f"{type(e).__name__}: {e}", trace=traceback.format_exc(limit=6))
            rec["s"] = round(time.perf_counter() - t0, 2)
            report["steps"].append(rec)
            print(("✅" if rec["ok"] else "❌"), name, "" if rec["ok"] else rec["error"])
            return rec
        return run
    return deco


@step("1. 连接 SolidWorks（带版本 ProgID）并核对版本")
def connect(version):
    from win32com.client import gencache

    from cadmcp.versioning import SW_PROGID, sw_version_from_revision
    sw = gencache.EnsureDispatch(SW_PROGID[version])
    sw.Visible = True
    rev = sw.RevisionNumber()
    return {"progid": SW_PROGID[version], "revision": rev, "detected": sw_version_from_revision(rev),
            "language": sw.GetCurrentLanguage(), "default_part_template": sw.GetUserPreferenceStringValue(8)}


@step("2. 读取 SolidWorks 常量类型库")
def constants():
    from cadmcp.adapters.solidworks.executor import load_constants
    c = load_constants()
    names = ["swEndCondBlind", "swEndCondThroughAll", "swSelFACES", "swAutodimSchemeOrdinate", "swMateCOORDINATE",
             "swDefaultTemplatePart", "swDefaultTemplateDrawing", "swSaveAsOptions_Silent", "swCustomInfoText"]
    return {n: getattr(c, n, "MISSING") for n in names}


@step("3. 新建零件：基准面名称、模型视图名称")
def planes_and_views(version):
    from win32com.client import gencache

    from cadmcp.versioning import SW_PROGID
    sw = gencache.EnsureDispatch(SW_PROGID[version])
    m = sw.NewDocument(sw.GetUserPreferenceStringValue(8), 0, 0, 0)
    planes, f = [], m.FirstFeature()
    while f is not None:
        if f.GetTypeName2() == "RefPlane":
            planes.append(f.Name)
        f = f.GetNextFeature()
    views = list(m.GetModelViewNames() or [])
    try:
        from cadmcp.adapters.solidworks.executor import load_constants
        c = load_constants()
        units = {"unit_system": m.Extension.GetUserPreferenceInteger(c.swUnitSystem, 0), "MMGS": c.swUnitSystem_MMGS}
    except Exception as e:  # noqa: BLE001
        units = f"unavailable: {e}"
    sw.CloseDoc(m.GetTitle())
    return {"ref_planes": planes, "model_view_names": views, "unit_system_pref": units}


@step("4. 端到端：按 spec 建底板 → 规则检查 → 出图（PDF）")
def e2e(node_config):
    from cylinder_station import BASE_PLATE, MOUNT_PLATE

    from cadmcp.gateway.service import CadService
    from cadmcp.ir import DrawingSpec, PartSpec
    svc = CadService.from_config(node_config) if node_config else CadService.from_config(None)
    if node_config is None:
        from cadmcp.adapters.solidworks.adapter import SolidWorksAdapter
        from cadmcp.gateway.service import Target
        svc.targets = {"sw2024": Target("sw2024", SolidWorksAdapter("2024"))}
    tid = next(t for t, v in svc.targets.items() if v.adapter.cad == "solidworks" and v.adapter.info()["adapter"] == "com")
    out = {"target": tid, "unavailable": svc.target_errors}
    svc.change_begin("PROBE", target=tid)
    for spec in (BASE_PLATE, MOUNT_PLATE):
        r = svc.build(PartSpec.model_validate(spec))
        out[spec["name"]] = {k: r.get(k) for k in ("ok", "file", "regen", "measured", "summary")}
        d = svc.build(DrawingSpec.model_validate({"model": spec["name"], "title_block": {"designer": "AI-probe"}}))
        out[spec["name"] + "_DRW"] = {k: d.get(k) for k in ("ok", "file", "regen", "sheet", "scale", "rules")}
    out["workspace"] = str(svc._session().root)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--version", default="2024")
    ap.add_argument("--node-config")
    a = ap.parse_args()
    r1 = connect(a.version)
    if r1["ok"]:
        constants()
        planes_and_views(a.version)
        e2e(a.node_config)
    out = ROOT / "probe_report_sw.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    print(f"\n报告：{out}\n请把该文件，以及工作区中的 *.oplog.json / *.PDF 一并发回。")


if __name__ == "__main__":
    main()
