"""变更会话：隔离工作区、快照、回滚、提交为新版本（绝不覆盖原文件）。

目录：<workspace_root>/<change_id>/{src,work,snapshots/<n>,out}，操作日志 changeset.jsonl
"""
from __future__ import annotations

import hashlib
import json
import shutil
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from .errors import CHECKS_NOT_PASSED, POLICY_DENIED, SPEC_INVALID, CadError
from .policy import Policy


def _sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()[:16]


@dataclass
class ChangeSession:
    id: str
    project: str
    target_id: str
    root: Path
    policy: Policy
    checkpoints: list[str] = field(default_factory=list)
    source_hashes: dict[str, str] = field(default_factory=dict)
    source_paths: dict[str, str] = field(default_factory=dict)  # 相对名 → 原始绝对路径（回写用）
    edits: list[dict] = field(default_factory=list)  # 对现有文件的修改记录
    committed: str | None = None

    @property
    def src(self) -> Path:
        return self.root / "src"

    @property
    def work(self) -> Path:
        return self.root / "work"

    @property
    def out(self) -> Path:
        return self.root / "out"

    # ---------- lifecycle ----------
    @classmethod
    def begin(cls, policy: Policy, project: str, target_id: str, sources: list[str] | None = None) -> "ChangeSession":
        if not project or any(c in project for c in '\\/:*?"<>|'):
            raise CadError(SPEC_INVALID, f"bad project name {project!r}")
        cid = f"c-{datetime.now():%Y%m%d-%H%M%S}-{hashlib.sha1(str(time.time_ns()).encode()).hexdigest()[:4]}"
        root = policy.workspace_root / cid
        s = cls(cid, project, target_id, root, policy)
        for d in (s.src, s.work, s.out):
            d.mkdir(parents=True, exist_ok=True)
        for src in sources or []:
            p = Path(src)
            policy.check_read(p)
            items = [p] if p.is_file() else [q for q in p.rglob("*") if q.is_file()]
            for q in items:
                rel = q.name if p.is_file() else q.relative_to(p)
                for dest_root in (s.src, s.work):
                    dest = dest_root / rel
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(q, dest)
                s.source_hashes[str(rel)] = _sha(q)
                s.source_paths[str(rel)] = str(q.resolve())
        for f in s.src.rglob("*"):
            if f.is_file():
                f.chmod(0o444)
        s.checkpoint("begin")
        s.log("begin", {"project": project, "target": target_id, "sources": sources or []})
        return s

    def add_source(self, q: Path, rel: str) -> Path:
        """会话中途纳入新的源文件（如自动找到的引用文件）：同 begin，复制到 src（只读）与 work。"""
        self.policy.check_read(q)
        for dest_root in (self.src, self.work):
            dest = dest_root / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(q, dest)
        (self.src / rel).chmod(0o444)
        self.source_hashes[rel] = _sha(q)
        self.source_paths[rel] = str(q.resolve())
        return self.work / rel

    def log(self, op: str, args: dict[str, Any], result: dict[str, Any] | None = None) -> None:
        rec = {"t": datetime.now().isoformat(timespec="seconds"), "op": op, "args": args}
        if result is not None:
            rec["result"] = result
        with (self.root / "changeset.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")

    def history(self) -> list[dict[str, Any]]:
        p = self.root / "changeset.jsonl"
        return [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines()] if p.exists() else []

    def checkpoint(self, label: str = "") -> int:
        n = len(self.checkpoints)
        dest = self.root / "snapshots" / str(n)
        shutil.copytree(self.work, dest)
        self.checkpoints.append(label or f"#{n}")
        return n

    def rollback(self, to: int = 0) -> None:
        if not 0 <= to < len(self.checkpoints):
            raise CadError(SPEC_INVALID, f"checkpoint {to} does not exist", hint=f"0..{len(self.checkpoints) - 1}")
        shutil.rmtree(self.work)
        shutil.copytree(self.root / "snapshots" / str(to), self.work)
        self.checkpoints = self.checkpoints[: to + 1]
        self.log("rollback", {"to": to})

    def commit(self, revision: str, gates: dict[str, bool], report: dict[str, Any]) -> Path:
        if self.committed:
            raise CadError(POLICY_DENIED, f"session already committed to {self.committed}")
        failed = [g for g in self.policy.commit_requires if not gates.get(g, False)]
        if failed:
            raise CadError(CHECKS_NOT_PASSED, f"commit gates not passed: {failed}",
                           hint="运行 cad_validate / 修复错误后再提交；gates=" + json.dumps(gates))
        if not revision or any(c in revision for c in '\\/:*?"<>|'):
            raise CadError(SPEC_INVALID, f"bad revision {revision!r}")
        dest = self.policy.release_root / self.project / revision
        self.policy.check_write(dest)
        if dest.exists():
            raise CadError(POLICY_DENIED, f"release {dest} already exists — revisions are immutable",
                           hint="使用新的修订号")
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(self.work, dest)
        (dest / "change_report.json").write_text(json.dumps(
            {"change_id": self.id, "project": self.project, "revision": revision, "target": self.target_id,
             "gates": gates, "source_hashes": self.source_hashes, "history": self.history(), **report},
            ensure_ascii=False, indent=1, default=str), encoding="utf-8")
        # 原始文件未被改动的证明
        for rel, h in self.source_hashes.items():
            if _sha(self.src / rel) != h:
                raise CadError(POLICY_DENIED, f"source copy {rel} changed unexpectedly")
        self.committed = str(dest)
        self.log("commit", {"revision": revision, "dest": str(dest)})
        return dest
