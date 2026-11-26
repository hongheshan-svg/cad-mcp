"""适配器接口。Gateway 负责 IR 解析/规则/出图规划；适配器只负责"编译成原生模型并测量"。"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..detailing import DrawingPlan
from ..ir.assembly import AssemblySpec
from ..ir.assembly_resolve import ResolvedAssembly
from ..ir.part import PartSpec
from ..ir.resolve import ResolvedPart

# 能力级别：full / partial / none
CAPABILITIES = [
    "build_part.extrude", "build_part.shaft", "build_part.sheet_metal", "build_assembly", "build_drawing",
    "open_existing", "set_parameters_existing", "replace_component", "interference.exact", "mass_properties",
    "export.step", "export.pdf", "export.dxf", "screenshot",
]


@dataclass
class BuildResult:
    path: Path
    regen: dict[str, Any]  # {"status": ok|warnings|errors, "errors": [...], "warnings": [...]}
    measured: dict[str, Any] = field(default_factory=dict)  # CAD 实测：mass_kg, bbox_mm
    extra: dict[str, Any] = field(default_factory=dict)


class CadAdapter(ABC):
    cad: str
    version: str
    ext: dict[str, str]  # kind → 文件扩展名

    @abstractmethod
    def info(self) -> dict[str, Any]: ...

    @abstractmethod
    def capabilities(self) -> dict[str, str]: ...

    @abstractmethod
    def file_version(self, path: Path) -> tuple[str, str] | None:
        """返回 (cad, version)；不是本 CAD 的文件返回 None。"""

    @abstractmethod
    def build_part(self, spec: PartSpec, resolved: ResolvedPart, out_dir: Path) -> BuildResult: ...

    @abstractmethod
    def build_assembly(self, spec: AssemblySpec, resolved: ResolvedAssembly, out_dir: Path,
                       component_files: dict[str, Path]) -> BuildResult: ...

    @abstractmethod
    def build_drawing(self, plan: DrawingPlan, model_file: Path, out_dir: Path,
                      model: ResolvedPart | ResolvedAssembly) -> BuildResult: ...

    @abstractmethod
    def open(self, path: Path, readonly: bool) -> dict[str, Any]: ...

    @abstractmethod
    def regenerate(self, path: Path) -> dict[str, Any]: ...

    @abstractmethod
    def export(self, path: Path, fmt: str, out: Path) -> Path: ...

    def close_all(self) -> None:
        """回滚前关闭所有文档（释放文件锁）。"""
