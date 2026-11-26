"""Creo 适配器：Creopyson → CREOSON（J-Link）。

已实现（CREOSON 原生能力）：连接与版本设定、打开、参数读写、再生、质量属性、另存、导出。
从零建模与出图细节：配置 toolkit: {host, port} 后走 cadmcp_tk（Creo TOOLKIT 异步程序，docs/CREO_TOOLKIT.md）；
未配置时 build_part 如实返回 CAPABILITY_UNSUPPORTED，出图细节退回 CREOSON 扩展 ext.cadmcp_*。
⚠ 尚未在实机验证，见 docs/WINDOWS_TEST.md。
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from ...errors import CAPABILITY_UNSUPPORTED, NATIVE_ERROR, TARGET_UNAVAILABLE, CadError
from ..base import CAPABILITIES, BuildResult, CadAdapter
from .compile import compile_part, compile_part_tk
from .edit import CreoEditor


def creo_kind(path: Path) -> str:
    """part.prt / part.prt.3（Creo 带版本号的文件名）→ part"""
    parts = path.name.lower().split(".")
    ext = parts[1] if len(parts) > 1 else ""
    return {"prt": "part", "asm": "assembly", "drw": "drawing"}.get(ext, "unknown")


class CreoAdapter(CadAdapter):
    cad = "creo"
    tk: Any = None  # ToolkitClient（配置 toolkit 时）

    def __init__(self, version: str, host: str = "localhost", port: int = 9056, start_part: str = "",
                 udf_dir: str = "", library_root: str = "", home: str | None = None,
                 templates: dict[str, str] | None = None, sheet_unit_scale: float = 1.0,
                 view_map: dict[str, str] | None = None, toolkit: dict[str, Any] | None = None):
        try:
            import creopyson  # type: ignore[import-not-found]
        except ImportError:
            raise CadError(TARGET_UNAVAILABLE, "creopyson not installed", hint="uv sync --extra windows") from None
        self.version = version
        self.ext = {"part": ".prt", "assembly": ".asm", "drawing": ".drw"}
        self.start_part, self.udf_dir, self.library_root = start_part, udf_dir, library_root
        self.templates, self.sheet_unit_scale, self.view_map = templates or {}, sheet_unit_scale, view_map or {}  # 出图用
        self.c = creopyson.Client(ip_adress=host, port=port)
        self.tk = None
        if toolkit:  # 首次使用时才连接；连不上报 TARGET_UNAVAILABLE 并给启动提示
            from .toolkit import ToolkitClient
            self.tk = ToolkitClient(**toolkit)
        self.editor = CreoEditor(self.c)
        try:
            self.c.connect()
        except Exception as e:  # noqa: BLE001
            raise CadError(TARGET_UNAVAILABLE, f"cannot reach CREOSON at {host}:{port}: {e}",
                           hint="先运行 CreosonSetup 启动 CREOSON 服务，并启动对应版本的 Creo") from None
        major = int(version.split(".")[0])
        if major <= 7:
            self.c.creo_set_creo_version(major)  # Creo 7 需显式设置，否则部分再生操作报错
        if not self.c.is_creo_running():
            raise CadError(TARGET_UNAVAILABLE, "Creo is not running for this CREOSON session",
                           hint="启动 Creo 后重试（或用 creoson 的 creo_start）")

    def info(self) -> dict[str, Any]:
        return {"cad": "creo", "version": self.version, "adapter": "creoson+toolkit" if self.tk else "creoson"}

    def capabilities(self) -> dict[str, str]:
        caps = {c: "none" for c in CAPABILITIES}
        caps.update({"open_existing": "full", "set_parameters_existing": "full", "mass_properties": "full",
                     "export.step": "partial", "export.pdf": "partial", "build_drawing": "partial"})
        if self.tk is not None:
            caps.update({"build_part.extrude": "partial", "build_part.shaft": "partial"})
        elif self.start_part and self.udf_dir:
            caps.update({"build_part.extrude": "partial", "build_part.shaft": "partial"})
        return caps

    def file_version(self, path: Path) -> tuple[str, str] | None:
        """从 Creo 文件头读取版本（格式需实机核对；探针脚本会打印文件头）。"""
        try:
            head = path.read_bytes()[:4096].decode("latin-1")
        except OSError:
            return None
        if not head.startswith("#UGC"):
            return None
        m = re.search(r"Creo(?: Parametric)?\s+(\d+)\.0", head)
        return ("creo", f"{m.group(1)}.0") if m else ("creo", self.version)

    def open(self, path: Path, readonly: bool) -> dict[str, Any]:
        self.c.creo_cd(str(path.parent))
        self.c.file_open(path.name, display=True, activate=True)
        params = self.c.parameter_list(file_=path.name)
        return {"kind": creo_kind(path), "name": path.name, "parameters": params}

    def get_parameters(self, path: Path) -> list[dict[str, Any]]:
        return self.c.parameter_list(file_=path.name)

    def set_parameters(self, path: Path, updates: dict[str, Any]) -> dict[str, Any]:
        for k, v in updates.items():
            self.c.parameter_set(k, v, file_=path.name, no_create=True)
        return self.regenerate(path)

    def regenerate(self, path: Path) -> dict[str, Any]:
        try:
            self.c.file_regenerate(file_=path.name)
        except Exception as e:  # noqa: BLE001
            return {"status": "errors", "errors": [str(e)], "warnings": []}
        return {"status": "ok", "errors": [], "warnings": []}

    def mass_properties(self, path: Path) -> dict[str, Any]:
        return self.c.file_massprops(file_=path.name)

    # ---- 修改现有模型（逻辑在 edit.py）----
    def inspect(self, path: Path) -> dict[str, Any]:
        return self.editor.inspect(path)

    def apply_edits(self, path: Path, edits: list[Any]) -> dict[str, Any]:
        return self.editor.apply_edits(path, edits)

    def references(self, path: Path) -> list[str] | None:
        return self.editor.references(path)

    def refresh(self, path: Path, preload: list[Path] | None = None) -> dict[str, Any]:
        return self.editor.refresh(path, preload)

    def retarget(self, path: Path, by_name: dict[str, Path]) -> int:
        """Creo 装配/工程图按"模型名 + 搜索路径"引用，文件里不存路径，回写后无需改写引用。"""
        return 0

    def close_all(self) -> None:
        self.c.file_erase()  # 不带 file_：清出会话中所有模型（回滚前释放）

    def build_part(self, spec, resolved, out_dir: Path) -> BuildResult:
        if self.tk is not None:
            return self._build_part_tk(spec, resolved, out_dir)
        plan = compile_part(spec, resolved)
        raise CadError(CAPABILITY_UNSUPPORTED,
                       f"Creo 从零建模需要 UDF 库与 CREOSON 扩展（计划已编译 {len(plan)} 步）",
                       hint="首期 Creo 侧先做：打开现有模型 → 改参数 → 再生 → 出 BOM；或确认是否有 TOOLKIT 授权")

    def _build_part_tk(self, spec, resolved, out_dir: Path) -> BuildResult:
        from .toolkit import run_tk_ops
        ops = compile_part_tk(spec, resolved, self.start_part)
        res = run_tk_ops(self.tk, ops, out_dir, spec.name)
        oplog = str(out_dir / f"{spec.name}.oplog.json")
        rb = res["readback"]
        regen = rb.get("regen") or {}
        if res["status"] == "errors":
            failed = [r for r in res["log"] if not r["ok"]]
            where = failed[-1]["op"] if failed else "model.regenerate"
            raise CadError(NATIVE_ERROR, f"Creo TOOLKIT build {spec.name} failed at {where}",
                           hint=f"查看 {oplog}；再生失败的特征：{regen.get('failed', [])}",
                           native_code=failed[-1]["error"] if failed else str(regen.get("failed")))
        path = Path(rb["path"]) if rb.get("path") else out_dir / f"{spec.name.lower()}.prt"
        return BuildResult(path, {"status": res["status"], "errors": [], "warnings": res["warnings"], "oplog": oplog},
                           {}, extra={"features": rb.get("features", [])})

    def build_assembly(self, spec, resolved, out_dir, component_files) -> BuildResult:
        raise CadError(CAPABILITY_UNSUPPORTED, "Creo assembly build: P2（file_assemble + Csys 约束）")

    def build_drawing(self, plan, model_file, out_dir, model) -> BuildResult:
        from .drawing import CreoDrawingExecutor, compile_drawing
        ops = compile_drawing(plan, str(model_file), self.templates, self.sheet_unit_scale, self.view_map,
                              toolkit=self.tk is not None)
        ex = CreoDrawingExecutor(self.c, self.tk)
        self.c.creo_cd(str(out_dir))  # 图纸保存到输出目录
        res = ex.run(ops, out_dir, plan.name)
        oplog = str(out_dir / f"{plan.name}.oplog.json")
        failed = [r for r in res["log"] if not r["ok"]]
        if res["status"] == "errors":
            raise CadError(NATIVE_ERROR, f"Creo drawing {plan.name} failed at op {failed[-1]['op']}",
                           hint=f"查看 {oplog}；模板/CREOSON 连接是否正常", native_code=failed[-1]["error"])
        regen = {"status": res["status"], "errors": [], "warnings": ex.warnings, "oplog": oplog}
        rendered = {k: ex.readback[k] for k in ("views",) if k in ex.readback}
        if "dims" in ex.readback:  # TOOLKIT 回读
            rendered["dim_count"] = ex.readback["dims"]
        drw = ex.drawing or plan.name
        return BuildResult(out_dir / (drw if drw.lower().endswith(".drw") else drw + ".drw"), regen, extra={"rendered": rendered})

    def export(self, path: Path, fmt: str, out: Path) -> Path:
        if fmt == "step":
            self.c.interface_export_file("STEP", file_=path.name, dirname=str(out.parent), filename=out.name)
            return out
        if fmt == "pdf":
            self.c.interface_export_pdf(file_=path.name, dirname=str(out.parent), filename=out.name)
            return out
        raise CadError(CAPABILITY_UNSUPPORTED, f"export {fmt} not supported for Creo")
