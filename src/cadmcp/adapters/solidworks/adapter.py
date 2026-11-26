"""SolidWorks 适配器（Windows + pywin32）。

连接：用带版本号的 ProgID（SldWorks.Application.32 = 2024）并校验 RevisionNumber，
避免多版本共存时连到错误的 SolidWorks。建模/出图：执行 compile.py 生成的 NativeOp 计划。
⚠ 执行器尚未在实机验证，见 docs/WINDOWS_TEST.md。
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from ...errors import CAPABILITY_UNSUPPORTED, NATIVE_ERROR, TARGET_UNAVAILABLE, CadError
from ...versioning import SW_PROGID, sw_version_from_revision
from ..base import CAPABILITIES, BuildResult, CadAdapter
from .compile import compile_assembly, compile_drawing, compile_part

# 平台材料名 → SW 材料库（库名, 材料名）。按公司材料库调整。
DEFAULT_MATERIAL_MAP = {
    "Q235": ("SOLIDWORKS Materials", "AISI 1020"),
    "45#": ("SOLIDWORKS Materials", "AISI 1045 Steel, cold drawn"),
    "SUS304": ("SOLIDWORKS Materials", "AISI 304"),
    "AL6061-T6": ("SOLIDWORKS Materials", "6061-T6 (SS)"),
    "AL6063-T5": ("SOLIDWORKS Materials", "6063-T5"),
    "AL7075-T6": ("SOLIDWORKS Materials", "7075-T6 (SN)"),
    "SPCC": ("SOLIDWORKS Materials", "Plain Carbon Steel"),
    "40Cr": ("SOLIDWORKS Materials", "AISI 4140 Steel, normalized"),
}


class SolidWorksAdapter(CadAdapter):
    cad = "solidworks"

    def __init__(self, version: str, visible: bool = True, templates: dict[str, str] | None = None,
                 material_map: dict[str, list[str]] | None = None, library_root: str = "",
                 holes_via: str = "cut", view_map: dict[str, list] | None = None):
        if sys.platform != "win32":
            raise CadError(TARGET_UNAVAILABLE, "SolidWorks adapter requires Windows", hint="在 macOS/Linux 上使用 mock 目标")
        from win32com.client import gencache  # type: ignore[import-not-found]

        from .executor import load_constants

        self.version = version
        self.ext = {"part": ".SLDPRT", "assembly": ".SLDASM", "drawing": ".SLDDRW"}
        progid = SW_PROGID[version]
        try:
            self.sw = gencache.EnsureDispatch(progid)  # 早绑定：支持 out 参数
        except Exception as e:  # noqa: BLE001
            raise CadError(TARGET_UNAVAILABLE, f"cannot start {progid}: {e}", hint="确认 SolidWorks 已安装并授权") from None
        self.sw.Visible = visible
        rev = str(self.sw.RevisionNumber())
        actual = sw_version_from_revision(rev)
        if actual != version:
            raise CadError(TARGET_UNAVAILABLE, f"{progid} connected to SolidWorks {actual} (rev {rev}), expected {version}",
                           hint="检查 COM 注册；建议一台机器只装一个 SW 大版本")
        self.revision = rev
        self.constants = load_constants()
        self.templates = templates or {}
        self.material_map = {k: tuple(v) for k, v in (material_map or {}).items()} or DEFAULT_MATERIAL_MAP
        self.library_root = library_root
        self.holes_via = holes_via
        if view_map:
            from . import compile as c
            c.SW_VIEW.update({k: tuple(v) for k, v in view_map.items()})

    def info(self) -> dict[str, Any]:
        return {"cad": "solidworks", "version": self.version, "revision": self.revision, "adapter": "com"}

    def has_library_item(self, code: str) -> bool:
        return bool(self.library_root) and (Path(self.library_root) / f"{code}.SLDPRT").exists()

    def capabilities(self) -> dict[str, str]:
        caps = {c: "partial" for c in CAPABILITIES}  # 由契约测试结果覆盖
        caps.update({"set_parameters_existing": "partial", "replace_component": "none"})
        return caps

    def _executor(self):
        from .executor import SwExecutor
        return SwExecutor(self.sw, self.constants, self.material_map, self.templates)

    def _run(self, ops, out_dir: Path, name: str) -> tuple[Any, dict[str, Any]]:
        ex = self._executor()
        res = ex.run(ops, out_dir, name)
        failed = [r for r in res["log"] if not r["ok"]]
        regen = {"status": res["status"],
                 "errors": [f"{r['op']}: {r['error']}" for r in failed if res["status"] == "errors" and r is failed[-1]],
                 "warnings": [f"{r['op']}: {r['error']}" for r in failed if not (res["status"] == "errors" and r is failed[-1])],
                 "oplog": str(out_dir / f"{name}.oplog.json")}
        if res["status"] == "errors" or ex.saved_path is None:
            raise CadError(NATIVE_ERROR, f"SolidWorks build of {name} failed at op {failed[-1]['op'] if failed else '?'}",
                           hint=f"查看 {regen['oplog']}；把该文件发回开发组",
                           native_code=failed[-1]["error"] if failed else None)
        return ex, regen

    def file_version(self, path: Path) -> tuple[str, str] | None:
        hist = self.sw.VersionHistory(str(path))
        if not hist:
            return None
        last = str(hist[-1])
        year = next((y for y in SW_PROGID if y in last), None)
        return ("solidworks", year) if year else None

    def build_part(self, spec, resolved, out_dir: Path) -> BuildResult:
        ops = compile_part(spec, resolved, self.templates.get("part", ""), self.holes_via)
        ex, regen = self._run(ops, out_dir, spec.name)
        self.sw.CloseDoc(ex.model.GetTitle())
        return BuildResult(ex.saved_path, regen, {"mass_kg": ex.readback.get("mass_kg"),
                                                  "bbox_mm": ex.readback.get("bbox_mm"), "source": "cad"})

    def build_assembly(self, spec, resolved, out_dir, component_files) -> BuildResult:
        ops = compile_assembly(spec, resolved, {k: str(v) for k, v in component_files.items()}, self.library_root)
        ex, regen = self._run(ops, out_dir, spec.name)
        rb = ex.readback
        return BuildResult(ex.saved_path, regen, {"source": "cad"}, {"interferences": rb.get("interferences", [])})

    def build_drawing(self, plan, model_file, out_dir, model) -> BuildResult:
        ops = compile_drawing(plan, str(model_file), {k[8:]: v for k, v in self.templates.items() if k.startswith("drawing_")},
                              getattr(model, "shaft_features", None))
        ex, regen = self._run(ops, out_dir, plan.name)
        return BuildResult(ex.saved_path, regen, extra={"rendered": {"dim_count": ex.readback.get("dim_count", 0),
                                                                     "notes": ex.readback.get("notes", [])},
                                                        "exports": ex.readback.get("exports", [])})

    # ---- 修改现有模型 ----
    def _edit_run(self, path: Path, ops, tag: str):
        from ..native import NativeOp
        ex = self._executor()
        res = ex.run([NativeOp("open_document", {"path": str(path)}), *ops], path.parent, f"{path.stem}.{tag}")
        return ex, res

    def inspect(self, path: Path) -> dict[str, Any]:
        from ..native import NativeOp
        ex, res = self._edit_run(path, [NativeOp("inspect", {})], "inspect")
        if res["status"] == "errors":
            raise CadError(NATIVE_ERROR, f"inspect {path.name} failed", hint=f"查看 {path.stem}.inspect.oplog.json")
        return ex.readback["inspect"][0]

    def apply_edits(self, path: Path, edits: list[Any]) -> dict[str, Any]:
        from ..native import NativeOp
        ops = []
        units = {d["name"]: d.get("unit", "mm") for d in self.inspect(path).get("dimensions", [])}
        for e in edits:
            if e.op == "set_dimension":
                ops.append(NativeOp("set_dimension", {"name": e.name, "value": e.value, "unit": units.get(e.name, "mm")}))
            elif e.op == "set_variable":
                ops.append(NativeOp("set_variable", {"name": e.name, "value": e.value}))
            elif e.op == "set_property":
                ops.append(NativeOp("set_custom_properties", {"properties": {e.name: e.value}}))
            elif e.op == "suppress":
                ops.append(NativeOp("suppress", {"feature": e.feature, "suppressed": e.suppressed}))
            elif e.op == "set_material":
                ops.append(NativeOp("set_material", {"database": "", "name": e.material}))
            elif e.op == "replace_component":
                ops.append(NativeOp("replace_component", {"instance": e.instance, "file": e.file,
                                                          "all_instances": e.all_instances}))
        ops += [NativeOp("rebuild", {}), NativeOp("collect_feature_errors", {}), NativeOp("inspect", {})]
        ex, res = self._edit_run(path, ops, "edit")
        regen = {"status": res["status"], "oplog": str(path.parent / f"{path.stem}.edit.oplog.json"),
                 "errors": [f"{r['op']}: {r['error']}" for r in res["log"] if not r["ok"]]}
        if res["status"] != "errors":
            ex.op_save_document()
        ex.op_close_document()  # 出错时不保存直接关闭：工作副本保持原样
        return {"regen": regen, "after": (ex.readback.get("inspect") or [None])[-1]}

    def library_path(self, code: str) -> Path | None:
        f = Path(self.library_root) / f"{code}.SLDPRT" if self.library_root else None
        return f if f is not None and f.exists() else None

    def references(self, path: Path) -> list[str] | None:
        if path.suffix.upper() not in (".SLDASM", ".SLDDRW"):
            return None
        deps = self.sw.GetDocumentDependencies2(str(path), False, True, False) or []
        return [str(d) for d in deps[1::2]]  # [名称, 路径, 名称, 路径, …]

    def refresh(self, path: Path, preload: list[Path] | None = None) -> dict[str, Any]:
        from ..native import NativeOp
        # 先打开已修改的文件：SW 打开装配/工程图时优先使用内存中同名文档，避免按旧路径读到共享盘上的旧版本
        pre = [self.sw.OpenDoc6(str(q), self.constants.swDocPART if q.suffix.upper() == ".SLDPRT"
                                else self.constants.swDocASSEMBLY, self.constants.swOpenDocOptions_Silent, "", 0, 0)
               for q in (preload or [])]
        ops = [NativeOp("rebuild", {}), NativeOp("count_dangling", {}), NativeOp("save_document", {})]
        if path.suffix.upper() == ".SLDDRW":
            ops.append(NativeOp("export", {"format": "pdf"}))
        ex, res = self._edit_run(path, ops, "refresh")
        ex.op_close_document()
        for d in pre:
            if d is not None:
                self.sw.CloseDoc(d.GetTitle())
        return {"status": res["status"], "dangling": ex.readback.get("dangling", 0),
                "exports": ex.readback.get("exports", [])}

    def retarget(self, path: Path, by_name: dict[str, Path]) -> int:
        """ISldWorks.ReplaceReferencedDocument：把引用改为原位置的同名文件（文件须未打开）。"""
        if path.suffix.upper() not in (".SLDASM", ".SLDDRW"):
            return 0
        n = 0
        for old in self.references(path) or []:
            new = by_name.get(Path(old.replace("\\", "/")).name.lower())
            if new is not None and str(new).lower() != old.lower() and self.sw.ReplaceReferencedDocument(str(path), old, str(new)):
                n += 1
        return n

    def open(self, path: Path, readonly: bool) -> dict[str, Any]:
        c = self.constants
        ext = path.suffix.upper()
        dt = {".SLDPRT": c.swDocPART, ".SLDASM": c.swDocASSEMBLY, ".SLDDRW": c.swDocDRAWING}[ext]
        opts = c.swOpenDocOptions_Silent | (c.swOpenDocOptions_ReadOnly if readonly else 0)
        doc = self.sw.OpenDoc6(str(path), dt, opts, "", 0, 0)
        if doc is None:
            raise CadError(NATIVE_ERROR, f"OpenDoc6 failed for {path}")
        return {"kind": {".SLDPRT": "part", ".SLDASM": "assembly", ".SLDDRW": "drawing"}[ext], "name": doc.GetTitle()}

    def regenerate(self, path: Path) -> dict[str, Any]:
        raise CadError(CAPABILITY_UNSUPPORTED, "P1: ForceRebuild3 on opened document")

    def export(self, path: Path, fmt: str, out: Path) -> Path:
        doc = self.sw.OpenDoc6(str(path), self.constants.swDocDRAWING if path.suffix.upper() == ".SLDDRW"
                               else self.constants.swDocPART, self.constants.swOpenDocOptions_Silent, "", 0, 0)
        err = doc.SaveAs3(str(out), 0, self.constants.swSaveAsOptions_Silent | self.constants.swSaveAsOptions_Copy)
        if err != 0:
            raise CadError(NATIVE_ERROR, f"export {fmt} failed ({err})")
        return out

    def close_all(self) -> None:
        self.sw.CloseAllDocuments(True)
