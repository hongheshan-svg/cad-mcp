"""策略：哪些操作允许、提交前必须通过哪些检查、哪些路径可读可写。"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .errors import POLICY_DENIED, CadError


@dataclass
class Policy:
    workspace_root: Path
    release_root: Path
    read_allow: list[Path] = field(default_factory=list)
    allow_upgrade_save: bool = False
    commit_requires: list[str] = field(default_factory=lambda: ["regen_ok", "rules_no_error", "drawings_no_error",
                                                                 "edits_ok"])
    native_call_enabled: bool = False
    mass_tolerance: float = 0.03
    writeback_allow: list[Path] = field(default_factory=list)  # 允许把发布结果回写的原位置（共享盘/PDM 检出目录）

    @classmethod
    def load(cls, path: str | Path | None, workspace_root: str | Path | None = None) -> "Policy":
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) if path else {}
        ws = Path(workspace_root or data.get("workspace_root") or Path.home() / "cadmcp" / "workspaces")
        rel = Path(data.get("release_root") or ws.parent / "releases")
        return cls(
            workspace_root=ws.resolve(), release_root=rel.resolve(),
            read_allow=[Path(p).resolve() for p in data.get("read_allow", [])],
            allow_upgrade_save=bool(data.get("allow_upgrade_save", False)),
            commit_requires=list(data.get("commit_requires", cls.__dataclass_fields__["commit_requires"].default_factory())),
            native_call_enabled=bool(data.get("native_call_enabled", False)),
            mass_tolerance=float(data.get("mass_tolerance", 0.03)),
            writeback_allow=[Path(p).resolve() for p in data.get("writeback_allow", [])],
        )

    @staticmethod
    def _under(p: Path, roots: list[Path]) -> bool:
        p = p.resolve()
        return any(p == r or r in p.parents for r in roots)

    def check_read(self, p: Path) -> None:
        if not self._under(p, self.read_allow + [self.workspace_root, self.release_root]):
            raise CadError(POLICY_DENIED, f"reading {p} is not allowed",
                           hint="路径需在 policy.read_allow 中（设计库/PDM 检出目录）")

    def check_writeback(self, p: Path) -> None:
        if not self._under(p, self.writeback_allow):
            raise CadError(POLICY_DENIED, f"writing back to {p} is not allowed",
                           hint="管理员在 policy.writeback_allow 中加入该目录后才能回写；否则请人工把发布目录的文件放回原处")

    def check_write(self, p: Path) -> None:
        if not self._under(p, [self.workspace_root, self.release_root]):
            raise CadError(POLICY_DENIED, f"writing {p} is not allowed", hint="只能写入工作区或发布目录")
        if self._under(p, self.read_allow) and not self._under(p, [self.workspace_root, self.release_root]):
            raise CadError(POLICY_DENIED, f"{p} is in a protected source location")
