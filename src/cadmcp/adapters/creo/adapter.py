"""Creo 适配器：Creopyson → CREOSON（J-Link）。

已实现（CREOSON 原生能力）：连接与版本设定、打开、参数读写、再生、质量属性、另存、导出。
从零建模：需要"起始件 + UDF 库 + CREOSON 扩展命令 cadmcp_udf_place"（见 compile.py）——
在 UDF 库就绪前，build_* 如实返回 CAPABILITY_UNSUPPORTED。
⚠ 尚未在实机验证，见 docs/WINDOWS_TEST.md。
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from ...errors import CAPABILITY_UNSUPPORTED, NATIVE_ERROR, TARGET_UNAVAILABLE, CadError
from ..base import CAPABILITIES, BuildResult, CadAdapter
from .compile import compile_part


def creo_kind(path: Path) -> str:
    """part.prt / part.prt.3（Creo 带版本号的文件名）→ part"""
    parts = path.name.lower().split(".")
    ext = parts[1] if len(parts) > 1 else ""
    return {"prt": "part", "asm": "assembly", "drw": "drawing"}.get(ext, "unknown")


class CreoAdapter(CadAdapter):
    cad = "creo"

    def __init__(self, version: str, host: str = "localhost", port: int = 9056, start_part: str = "",
                 udf_dir: str = "", library_root: str = "", home: str | None = None):
        try:
            import creopyson  # type: ignore[import-not-found]
        except ImportError:
            raise CadError(TARGET_UNAVAILABLE, "creopyson not installed", hint="uv sync --extra windows") from None
        self.version = version
        self.ext = {"part": ".prt", "assembly": ".asm", "drawing": ".drw"}
        self.start_part, self.udf_dir, self.library_root = start_part, udf_dir, library_root
        self.c = creopyson.Client(ip_adress=host, port=port)
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
        return {"cad": "creo", "version": self.version, "adapter": "creoson"}

    def capabilities(self) -> dict[str, str]:
        caps = {c: "none" for c in CAPABILITIES}
        caps.update({"open_existing": "full", "set_parameters_existing": "full", "mass_properties": "full",
                     "export.step": "partial", "export.pdf": "partial"})
        if self.start_part and self.udf_dir:
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

    def build_part(self, spec, resolved, out_dir: Path) -> BuildResult:
        plan = compile_part(spec, resolved)
        raise CadError(CAPABILITY_UNSUPPORTED,
                       f"Creo 从零建模需要 UDF 库与 CREOSON 扩展（计划已编译 {len(plan)} 步）",
                       hint="首期 Creo 侧先做：打开现有模型 → 改参数 → 再生 → 出 BOM；或确认是否有 TOOLKIT 授权")

    def build_assembly(self, spec, resolved, out_dir, component_files) -> BuildResult:
        raise CadError(CAPABILITY_UNSUPPORTED, "Creo assembly build: P2（file_assemble + Csys 约束）")

    def build_drawing(self, plan, model_file, out_dir, model) -> BuildResult:
        raise CadError(CAPABILITY_UNSUPPORTED, "Creo drawing build: P2（drawing_create + 模板 + 视图）")

    def export(self, path: Path, fmt: str, out: Path) -> Path:
        if fmt == "step":
            self.c.interface_export_file("STEP", file_=path.name, dirname=str(out.parent), filename=out.name)
            return out
        if fmt == "pdf":
            self.c.interface_export_pdf(file_=path.name, dirname=str(out.parent), filename=out.name)
            return out
        raise CadError(CAPABILITY_UNSUPPORTED, f"export {fmt} not supported for Creo")
