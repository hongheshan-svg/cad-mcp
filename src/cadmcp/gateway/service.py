"""Gateway 核心服务（与 MCP 传输无关，便于测试）。

模型注册表以工作区中的 <name>.spec.json 为准（spec 是唯一事实来源）：
回滚 = 恢复工作区快照 + 从 spec 文件重建注册表。
"""
from __future__ import annotations

import csv
import json
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .. import rules
from ..adapters.base import CadAdapter
from ..adapters.mock import MockAdapter
from ..detailing import check_drawing, plan_assembly_drawing, plan_part_drawing
from ..errors import (CAPABILITY_UNSUPPORTED, DOC_NOT_FOUND, NO_CHANGE_SESSION, PARAM_NOT_FOUND, SPEC_INVALID,
                      TARGET_UNAVAILABLE, CadError)
from ..ir import AssemblySpec, DrawingSpec, PartSpec
from ..ir.assembly_resolve import ResolvedAssembly, SourceInfo, aabb_interference, resolve_assembly
from ..ir.resolve import ResolvedPart, ResolvedRef, resolve_part
from ..library import Library
from ..policy import Policy
from ..changeset import ChangeSession
from ..versioning import decide_open

SPEC_TYPES = {"part": PartSpec, "assembly": AssemblySpec, "drawing": DrawingSpec}


@dataclass
class Target:
    id: str
    adapter: CadAdapter
    node: str = "local"
    lock: threading.Lock = field(default_factory=threading.Lock)

    def describe(self) -> dict[str, Any]:
        return {"id": self.id, "node": self.node, **self.adapter.info()}


@dataclass
class Model:
    name: str
    kind: str  # part | assembly | drawing
    spec: Any
    resolved: Any
    file: Path
    deps: set[str]
    regen: dict[str, Any]
    findings: list[dict[str, Any]]
    measured: dict[str, Any] = field(default_factory=dict)
    extra: dict[str, Any] = field(default_factory=dict)

    def brief(self) -> dict[str, Any]:
        s = rules.summarize(self.findings)
        return {"name": self.name, "kind": self.kind, "file": str(self.file), "regen": self.regen["status"],
                "errors": s["errors"], "warnings": s["warnings"], "deps": sorted(self.deps)}


def make_adapter(cfg: dict[str, Any]) -> CadAdapter:
    kind = cfg.get("adapter", "mock")
    if kind == "mock":
        return MockAdapter(cfg["cad"], str(cfg["version"]))
    if kind == "solidworks":
        from ..adapters.solidworks.adapter import SolidWorksAdapter
        keys = ("visible", "templates", "material_map", "library_root", "holes_via", "view_map")
        return SolidWorksAdapter(str(cfg["version"]), **{k: cfg[k] for k in keys if k in cfg})
    if kind == "creo":
        from ..adapters.creo.adapter import CreoAdapter
        keys = ("start_part", "udf_dir", "library_root")
        return CreoAdapter(str(cfg["version"]), **cfg.get("creoson", {}), **{k: cfg[k] for k in keys if k in cfg})
    raise CadError(TARGET_UNAVAILABLE, f"unknown adapter {kind!r}")


class CadService:
    def __init__(self, policy: Policy, targets: list[Target] | None = None, library: Library | None = None):
        self.policy = policy
        self.targets: dict[str, Target] = {t.id: t for t in (targets or [])}
        self.target_errors: dict[str, str] = {}
        self.active_target: str | None = next(iter(self.targets), None)
        self.library = library or Library.default()
        self.sessions: dict[str, ChangeSession] = {}
        self.current: str | None = None
        self.models: dict[str, dict[str, Model]] = {}  # change_id → name → Model

    @classmethod
    def from_config(cls, node_config: str | None, policy_file: str | None = None,
                    workspace_root: str | None = None) -> "CadService":
        cfg = yaml.safe_load(Path(node_config).read_text(encoding="utf-8")) if node_config else {}
        policy = Policy.load(policy_file or cfg.get("policy"), workspace_root or cfg.get("workspace_root"))
        workers = cfg.get("workers") or [
            {"id": "mock-sw2024", "adapter": "mock", "cad": "solidworks", "version": "2024"},
            {"id": "mock-creo10", "adapter": "mock", "cad": "creo", "version": "10.0"},
        ]
        svc = cls(policy)
        for w in workers:
            try:
                svc.targets[w["id"]] = Target(w["id"], make_adapter(w), cfg.get("node_id", "local"))
            except CadError as e:
                svc.target_errors[w["id"]] = e.message
        svc.active_target = next(iter(svc.targets), None)
        return svc

    # ---------------- targets ----------------
    def list_targets(self) -> dict[str, Any]:
        return {"ok": True, "active": self.active_target, "targets": [t.describe() for t in self.targets.values()],
                "unavailable": self.target_errors}

    def _target(self, tid: str | None = None) -> Target:
        tid = tid or (self.sessions[self.current].target_id if self.current else self.active_target)
        if tid not in self.targets:
            raise CadError(TARGET_UNAVAILABLE, f"target {tid!r} not available", hint=f"available: {list(self.targets)}")
        return self.targets[tid]

    def select_target(self, target: str) -> dict[str, Any]:
        self._target(target)
        self.active_target = target
        return {"ok": True, "active": target}

    def capabilities(self, target: str | None = None) -> dict[str, Any]:
        t = self._target(target)
        return {"ok": True, "target": t.describe(), "capabilities": t.adapter.capabilities()}

    # ---------------- sessions ----------------
    def _session(self) -> ChangeSession:
        if not self.current:
            raise CadError(NO_CHANGE_SESSION, "no active change session", hint="先调用 cad_change_begin")
        return self.sessions[self.current]

    def _models(self) -> dict[str, Model]:
        return self.models[self._session().id]

    def change_begin(self, project: str, sources: list[str] | None = None, target: str | None = None) -> dict[str, Any]:
        t = self._target(target)
        s = ChangeSession.begin(self.policy, project, t.id, sources)
        self.sessions[s.id] = s
        self.models[s.id] = {}
        self.current = s.id
        return {"ok": True, "change_id": s.id, "target": t.describe(), "workspace": str(s.root),
                "copied_sources": sorted(s.source_hashes)}

    def change_status(self) -> dict[str, Any]:
        s = self._session()
        return {"ok": True, "change_id": s.id, "project": s.project, "target": s.target_id,
                "checkpoints": list(enumerate(s.checkpoints)), "models": [m.brief() for m in self._models().values()],
                "gates": self._gates(), "committed": s.committed}

    def checkpoint(self, label: str = "") -> dict[str, Any]:
        s = self._session()
        n = s.checkpoint(label)
        s.log("checkpoint", {"n": n, "label": label})
        return {"ok": True, "checkpoint": n}

    def rollback(self, to: int = 0) -> dict[str, Any]:
        s = self._session()
        self._target(s.target_id).adapter.close_all()
        s.rollback(to)
        self._reload_models()
        return {"ok": True, "rolled_back_to": to, "models": [m.brief() for m in self._models().values()]}

    def _reload_models(self) -> None:
        s = self._session()
        self.models[s.id] = {}
        specs = []
        for f in sorted(s.work.glob("*.spec.json")):
            d = json.loads(f.read_text(encoding="utf-8"))
            specs.append(SPEC_TYPES[d["kind"]].model_validate(d))
        order = {"part": 0, "assembly": 1, "drawing": 2}
        pending = sorted(specs, key=lambda sp: order[sp.kind])
        # 装配可能依赖其他装配：多轮直到收敛
        for _ in range(len(pending) + 1):
            rest = []
            for sp in pending:
                try:
                    self._build(sp, cascade=False)
                except CadError as e:
                    if e.code == DOC_NOT_FOUND:
                        rest.append(sp)
                    else:
                        raise
            if not rest:
                return
            pending = rest
        raise CadError(SPEC_INVALID, f"could not rebuild after rollback: {[p.name for p in pending]}")

    # ---------------- build ----------------
    def build(self, spec: PartSpec | AssemblySpec | DrawingSpec) -> dict[str, Any]:
        s = self._session()
        result = self._build(spec, cascade=True)
        s.log(f"build_{spec.kind}", {"name": self._name_of(spec)}, {k: result[k] for k in ("regen", "rules") if k in result})
        return result

    @staticmethod
    def _name_of(spec) -> str:
        return spec.name if spec.kind != "drawing" else (spec.name or f"{spec.model}_DRW")

    def _build(self, spec, cascade: bool) -> dict[str, Any]:
        s = self._session()
        t = self._target(s.target_id)
        models = self._models()
        name = self._name_of(spec)
        existing = models.get(name)
        if existing and existing.kind != spec.kind:
            raise CadError(SPEC_INVALID, f"name {name!r} already used by a {existing.kind}")
        with t.lock:
            if spec.kind == "part":
                res = resolve_part(spec)
                findings = rules.check_part(res)
                br = t.adapter.build_part(spec, res, s.work)
                deps: set[str] = set()
                mass_check = self._mass_check(res.mass_kg, br.measured)
                if mass_check:
                    findings.append(mass_check)
                out = {"summary": res.summary()}
            elif spec.kind == "assembly":
                res = resolve_assembly(spec, self._source_info)
                deps = {c.source.split(":", 1)[1] for c in spec.components if c.source.startswith(("part:", "asm:"))}
                files = {c.id: models[c.source.split(":", 1)[1]].file for c in spec.components
                         if c.source.startswith(("part:", "asm:"))}
                br = t.adapter.build_assembly(spec, res, s.work, files)
                interf = aabb_interference(res)
                findings = [{"rule": "A001", "severity": "warning", "target": f"{i['a']}~{i['b']}",
                             "message": f"包围盒重叠 {i['overlap_mm']} mm（需精确干涉检查确认）"} for i in interf]
                out = {"summary": res.summary(), "bom": res.bom()}
            else:
                if spec.model not in models:
                    raise CadError(DOC_NOT_FOUND, f"model {spec.model!r} not built in this session",
                                   hint=f"models: {sorted(models)}")
                mm = models[spec.model]
                if mm.kind == "part":
                    plan = plan_part_drawing(spec, mm.resolved)
                elif mm.kind == "assembly":
                    plan = plan_assembly_drawing(spec, mm.resolved)
                else:
                    raise CadError(SPEC_INVALID, "cannot make a drawing of a drawing")
                plan.name = name
                br = t.adapter.build_drawing(plan, mm.file, s.work, mm.resolved)
                res = plan
                deps = {spec.model}
                findings = [{"target": name, **f} for f in check_drawing(plan, br.extra.get("rendered"))]
                out = {"sheet": plan.sheet, "scale": plan.to_dict()["scale_label"], "views": [v.type for v in plan.views],
                       "dimensions": len(plan.dims), "preview": br.extra.get("preview_svg")}
        (s.work / f"{name}.spec.json").write_text(spec.model_dump_json(indent=1), encoding="utf-8")
        models[name] = Model(name, spec.kind, spec, res, br.path, deps, br.regen, findings, br.measured, br.extra)
        rebuilt = self._cascade(name) if cascade and existing else []
        summ = rules.summarize(findings)
        return {"ok": True, "name": name, "kind": spec.kind, "file": str(br.path), "regen": br.regen,
                "measured": br.measured, "rules": summ, **out, **({"rebuilt_dependents": rebuilt} if rebuilt else {})}

    def _mass_check(self, estimate: float, measured: dict[str, Any]) -> dict[str, Any] | None:
        m = measured.get("mass_kg")
        if m is None or measured.get("source") == "estimate" or estimate <= 0:
            return None
        dev = abs(m - estimate) / estimate
        if dev > self.policy.mass_tolerance:
            return {"rule": "M001", "severity": "warning", "target": "mass",
                    "message": f"CAD 实测质量 {m:.4f} kg 与 spec 估算 {estimate:.4f} kg 偏差 {dev:.1%}，"
                               "检查特征是否按预期生成"}
        return None

    def _cascade(self, changed: str) -> list[str]:
        """关联更新：重建依赖 changed 的装配与工程图（拓扑顺序）。"""
        models = self._models()
        done: list[str] = []
        frontier = [changed]
        while frontier:
            cur = frontier.pop(0)
            for m in list(models.values()):
                if cur in m.deps and m.name not in done:
                    self._build(m.spec, cascade=False)
                    done.append(m.name)
                    frontier.append(m.name)
        return done

    def _source_info(self, source: str) -> SourceInfo:
        kind, _, ref = source.partition(":")
        models = self._models()
        if kind == "part":
            if ref not in models or models[ref].kind != "part":
                raise CadError(DOC_NOT_FOUND, f"part {ref!r} not built in this session",
                               hint="先用 cad_build_part 建零件；可用零件：" + str(sorted(n for n, m in models.items() if m.kind == "part")))
            p: ResolvedPart = models[ref].resolved
            return SourceInfo(source, "part", p.part_no, p.title, p.material, p.mass_kg, p.bbox, p.references,
                              properties={"surface_treatment": p.surface_treatment or ""})
        if kind == "asm":
            if ref not in models or models[ref].kind != "assembly":
                raise CadError(DOC_NOT_FOUND, f"assembly {ref!r} not built in this session")
            a: ResolvedAssembly = models[ref].resolved
            return SourceInfo(source, "assembly", a.part_no, a.title, None, a.mass_kg, a.bbox,
                              {"CS_Origin": ResolvedRef("CS_Origin", "csys", (0.0, 0.0, 0.0), auto=True)})
        if kind == "lib":
            lp = self.library.get(ref)
            return SourceInfo(source, lp.category, lp.code + "".join(f"-{k}{v:g}" for k, v in lp.parameters.items()), lp.title, None,
                              lp.mass_kg, lp.bbox, lp.references, model=lp.model, properties=lp.properties)
        raise CadError(CAPABILITY_UNSUPPORTED, "file: components need open_existing support (P1)",
                       hint="先把现有零件转成 spec，或等待真实 CAD 目标支持")

    # ---------------- edit ----------------
    def get_model(self, name: str) -> dict[str, Any]:
        m = self._get(name)
        d = {"ok": True, **m.brief(), "spec": json.loads(m.spec.model_dump_json()), "findings": m.findings}
        if m.kind == "part":
            d["summary"] = m.resolved.summary()
        elif m.kind == "assembly":
            d["summary"], d["bom"] = m.resolved.summary(), m.resolved.bom()
        else:
            d["plan"] = {k: v for k, v in m.resolved.to_dict().items() if k != "dims"}
            d["dimensions"] = [{"view": x.view, "kind": x.kind, "text": x.text, "ref": x.ref} for x in m.resolved.dims]
        return d

    def _get(self, name: str) -> Model:
        models = self._models()
        if name not in models:
            raise CadError(DOC_NOT_FOUND, f"model {name!r} not found", hint=f"models: {sorted(models)}")
        return models[name]

    def set_parameters(self, name: str, updates: dict[str, float | str]) -> dict[str, Any]:
        m = self._get(name)
        if m.kind == "drawing":
            raise CadError(SPEC_INVALID, "drawings have no parameters; rebuild with a new DrawingSpec")
        missing = [k for k in updates if k not in m.spec.parameters]
        if missing:
            raise CadError(PARAM_NOT_FOUND, f"{name} has no parameters {missing}",
                           hint=f"parameters: {dict(m.spec.parameters)}；新增参数请用 cad_build_* 提交完整 spec")
        updates = {k: float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else v
                   for k, v in updates.items()}
        old = dict(m.spec.parameters)
        data = m.spec.model_dump()
        data["parameters"].update(updates)
        spec = type(m.spec).model_validate(data)
        res = self._build(spec, cascade=True)
        res["changed"] = {k: {"old": old[k], "new": v} for k, v in updates.items()}
        self._session().log("set_parameters", {"name": name, "updates": updates}, {"rebuilt": res.get("rebuilt_dependents", [])})
        return res

    # ---------------- checks / outputs ----------------
    def _gates(self) -> dict[str, bool]:
        ms = list(self._models().values())
        return {
            "has_models": bool(ms),
            "regen_ok": all(m.regen["status"] != "errors" for m in ms),
            "rules_no_error": all(rules.summarize(m.findings)["passed"] for m in ms if m.kind != "drawing"),
            "drawings_no_error": all(rules.summarize(m.findings)["passed"] for m in ms if m.kind == "drawing"),
            "every_part_has_drawing": all(any(d.kind == "drawing" and m.name in d.deps for d in ms)
                                          for m in ms if m.kind == "part"),
        }

    def validate(self, name: str | None = None) -> dict[str, Any]:
        ms = [self._get(name)] if name else list(self._models().values())
        return {"ok": True, "gates": self._gates(),
                "models": [{**m.brief(), "findings": m.findings} for m in ms]}

    def interference(self, name: str) -> dict[str, Any]:
        m = self._get(name)
        if m.kind != "assembly":
            raise CadError(SPEC_INVALID, f"{name} is not an assembly")
        caps = self._target().adapter.capabilities()
        res = aabb_interference(m.resolved)
        return {"ok": True, "method": "exact" if caps.get("interference.exact") == "full" else "aabb(保守，可能误报)",
                "count": len(res), "interferences": res}

    def export_bom(self, name: str) -> dict[str, Any]:
        m = self._get(name)
        if m.kind != "assembly":
            raise CadError(SPEC_INVALID, f"{name} is not an assembly")
        bom = m.resolved.bom()
        out = self._session().out / f"{name}_BOM.csv"
        with out.open("w", encoding="utf-8-sig", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["序号", "图号/编码", "名称", "规格", "材料", "类别", "数量", "单重kg"])
            for r in bom:
                w.writerow([r["item"], r["part_no"], r["title"], r["spec"], r["material"], r["kind"], r["qty"], r["unit_mass_kg"]])
        return {"ok": True, "file": str(out), "lines": len(bom), "bom": bom}

    def export(self, name: str, fmt: str) -> dict[str, Any]:
        m = self._get(name)
        s = self._session()
        out = s.out / f"{name}.{fmt}"
        path = self._target().adapter.export(m.file, fmt, out)
        return {"ok": True, "file": str(path)}

    def commit(self, revision: str) -> dict[str, Any]:
        s = self._session()
        gates = self._gates()
        report = {"models": [m.brief() for m in self._models().values()]}
        dest = s.commit(revision, gates, report)
        return {"ok": True, "release": str(dest), "gates": gates}

    def search_library(self, query: str = "", category: str | None = None) -> dict[str, Any]:
        return {"ok": True, "items": self.library.search(query, category)}

    def open_existing(self, path: str, write: bool = False) -> dict[str, Any]:
        p = Path(path)
        self.policy.check_read(p)
        t = self._target()
        fv = t.adapter.file_version(p)
        if fv is None:
            raise CadError(DOC_NOT_FOUND, f"{p} is not a {t.adapter.cad} file readable by target {t.id}")
        d = decide_open(fv[0], fv[1], t.adapter.cad, t.adapter.version, write, self.policy.allow_upgrade_save)
        info = t.adapter.open(p, readonly=d.readonly)
        return {"ok": True, "file_version": fv[1], "readonly": d.readonly, "note": d.note, **info}
