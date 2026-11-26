"""适配器接口。Gateway 负责 IR 解析/规则/出图规划；适配器只负责"编译成原生模型并测量"。"""
from __future__ import annotations

from ..errors import CAPABILITY_UNSUPPORTED, CadError

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

    def has_library_item(self, code: str) -> bool:
        """本 CAD 是否有该标准件/外购件的原生模型；没有时平台会生成包络块占位零件。"""
        return True

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

    # ---- 修改现有模型（可选能力）----
    def inspect(self, path: Path) -> dict[str, Any]:
        """特征树、尺寸、方程式/全局变量、属性、材料、质量。"""
        raise CadError(CAPABILITY_UNSUPPORTED, f"{self.cad}: inspect existing models not supported")

    def apply_edits(self, path: Path, edits: list[Any]) -> dict[str, Any]:
        """执行修改并重建；返回 {regen, after}。重建出错时不保存。"""
        raise CadError(CAPABILITY_UNSUPPORTED, f"{self.cad}: editing existing models not supported")

    def where_used(self, path: Path, search_dir: Path) -> list[Path]:
        """search_dir 中引用 path 的装配/工程图。"""
        return []

    def refresh(self, path: Path) -> dict[str, Any]:
        """重建引用文档（装配/工程图）并保存；工程图返回悬空标注数。"""
        raise CadError(CAPABILITY_UNSUPPORTED, f"{self.cad}: refresh not supported")

    @abstractmethod
    def open(self, path: Path, readonly: bool) -> dict[str, Any]: ...

    @abstractmethod
    def regenerate(self, path: Path) -> dict[str, Any]: ...

    @abstractmethod
    def export(self, path: Path, fmt: str, out: Path) -> Path: ...

    def close_all(self) -> None:
        """回滚前关闭所有文档（释放文件锁）。"""
