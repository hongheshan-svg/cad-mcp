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
from ..errors import (POLICY_DENIED, CAPABILITY_UNSUPPORTED, DOC_NOT_FOUND, NO_CHANGE_SESSION, PARAM_NOT_FOUND, SPEC_INVALID,
                      TARGET_UNAVAILABLE, CadError)
from ..ir import AssemblySpec, DrawingSpec, PartSpec
from ..ir.assembly_resolve import (ResolvedAssembly, SourceInfo, aabb_interference, interface_check,
                                  resolve_assembly)
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
                placeholders = self._library_placeholders(t, spec, s.work, files)
                br = t.adapter.build_assembly(spec, res, s.work, files)
                interf = aabb_interference(res)
                findings = [{"rule": "A001", "severity": "warning", "target": f"{i['a']}~{i['b']}",
                             "message": f"包围盒重叠 {i['overlap_mm']} mm（需精确干涉检查确认）"} for i in interf]
                findings += interface_check(spec, self._source_info)
                out = {"summary": res.summary(), "bom": res.bom()}
                for code in placeholders:
                    findings.append({"rule": "A002", "severity": "warning", "target": code,
                                     "message": f"标准件库缺少 {code} 的原生模型，已用包络块占位（BOM 编码不变）",
                                     "hint": "把带 CS_ 基准的模型放入节点配置的 library_root"})
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
                              properties={"surface_treatment": p.surface_treatment or ""},
                              mounts={f"CS_{m['name']}": m for m in p.mounts})
        if kind == "asm":
            if ref not in models or models[ref].kind != "assembly":
                raise CadError(DOC_NOT_FOUND, f"assembly {ref!r} not built in this session")
            a: ResolvedAssembly = models[ref].resolved
            return SourceInfo(source, "assembly", a.part_no, a.title, None, a.mass_kg, a.bbox,
                              {"CS_Origin": ResolvedRef("CS_Origin", "csys", (0.0, 0.0, 0.0), auto=True)})
        if kind == "lib":
            lp = self.library.get(ref)
            return SourceInfo(source, lp.category, lp.code + "".join(f"-{k}{v:g}" for k, v in lp.parameters.items()), lp.title, None,
                              lp.mass_kg, lp.bbox, lp.references, model=lp.model, properties=lp.properties,
                              interface_frame=(self.library.items[lp.code].get("mount") or {}).get("frame"))
        raise CadError(CAPABILITY_UNSUPPORTED, "file: components need open_existing support (P1)",
                       hint="先把现有零件转成 spec，或等待真实 CAD 目标支持")

    def _library_placeholders(self, t: Target, spec, work: Path, files: dict[str, Path]) -> list[str]:
        """库里没有原生模型的标准件：按包络尺寸生成占位块（带同名 CS_ 基准），保证装配能建出来。"""
        made: dict[str, Path] = {}
        for c in spec.components:
            if not c.source.startswith("lib:"):
                continue
            ref = c.source[4:]
            code = ref.partition("?")[0]
            if t.adapter.has_library_item(code):
                continue
            lp = self.library.get(ref)
            pn = lp.code + "".join(f"-{k}{v:g}" for k, v in lp.parameters.items())
            if pn not in made:
                b = lp.bbox
                dz = -b.zmin  # 占位件坐标系 = 库坐标系沿 Z 平移，使块从 z=0 起
                ps = PartSpec.model_validate({
                    "name": f"PH_{pn}", "part_no": pn, "title": f"{lp.title}（占位）", "material": "AL6061-T6",
                    "category": "purchased",
                    "base": {"type": "extrude", "thickness": b.zmax - b.zmin,
                             "profile": {"shape": "rect", "width": b.xmax - b.xmin, "height": b.ymax - b.ymin,
                                         "center": [(b.xmin + b.xmax) / 2, (b.ymin + b.ymax) / 2]}},
                    "references": [{"name": n, "origin": [r.origin[0], r.origin[1], r.origin[2] + dz],
                                    "rotate": list(r.rotate)}
                                   for n, r in lp.references.items() if n.startswith("CS_") and n != "CS_Origin"],
                })
                made[pn] = t.adapter.build_part(ps, resolve_part(ps), work).path
            files[c.id] = made[pn]
        return sorted(made)

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
        s_edits = self.sessions[self.current].edits if self.current else []
        return {
            "has_models": bool(ms) or bool(s_edits),
            "edits_ok": all(e["regen"]["status"] != "errors" and rules.summarize(e["findings"])["passed"]
                            for e in s_edits),
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

    def commit(self, revision: str, description: str = "", by: str = "") -> dict[str, Any]:
        """发布：先把版本号与更改记录写进每张图纸（标题栏 + 更改记录表）并重出图，再门禁检查、归档。"""
        from datetime import date as _date
        s = self._session()
        hist_file = self.policy.release_root / s.project / "revisions.json"
        history = json.loads(hist_file.read_text(encoding="utf-8")) if hist_file.exists() else []
        if any(h["rev"] == revision for h in history):
            raise CadError(POLICY_DENIED, f"revision {revision} already released", hint="使用新的修订号")
        changed = sorted(n for n, m in self._models().items() if m.kind != "drawing")
        auto = "；".join(e["description"] for e in s.edits) if s.edits else \
            ("首次发布" if not history else "更改：" + "、".join(changed))
        entry = {"rev": revision, "date": _date.today().isoformat(), "description": description or auto,
                 "by": by}
        for n, m in list(self._models().items()):
            if m.kind != "drawing":
                continue
            tb = dict(m.spec.title_block)
            tb["revision"] = revision
            if by and not tb.get("designer"):
                tb["designer"] = by
            spec = m.spec.model_copy(update={"title_block": tb, "revisions": [*history, entry]})
            self._build(spec, cascade=False)
        gates = self._gates()
        report = {"models": [m.brief() for m in self._models().values()],
                  "edits": [{k: v for k, v in e.items() if k != "checkpoint"} for e in s.edits]}
        dest = s.commit(revision, gates, report)
        hist_file.write_text(json.dumps([*history, entry], ensure_ascii=False, indent=1), encoding="utf-8")
        return {"ok": True, "release": str(dest), "gates": gates, "revision_entry": entry}

    def build_frame(self, spec_dict: dict[str, Any]) -> dict[str, Any]:
        """型材机架：生成装配 spec 并建模，返回下料清单。"""
        from ..frame import FrameSpec, frame_assembly
        fs = FrameSpec.model_validate(spec_dict)
        if fs.construction == "welded":
            from ..frame import weld_frame_part
            part = weld_frame_part(fs)
            out = self.build(PartSpec.model_validate(part))
            out["cut_list"] = out["summary"]["cut_list"]
            out["total_profile_m"] = round(sum(r["length_mm"] * r["qty"] for r in out["cut_list"]
                                               if r["kind"] == "member") / 1000, 3)
            out["part_spec"] = part
            return out
        asm, cut = frame_assembly(fs, self.library)
        out = self.build(AssemblySpec.model_validate(asm))
        out["cut_list"] = cut
        out["total_profile_m"] = round(sum(c["length_mm"] * c["qty"] for c in cut) / 1000, 3)
        out["assembly_spec"] = asm
        return out

    def calc(self, kind: str, params: dict[str, Any]) -> dict[str, Any]:
        """选型计算；有变更会话时把计算书写入工作区（随发布一起归档）。"""
        from .. import calc as calc_mod
        rep = calc_mod.run(kind, params)
        hint = rep["selection"].get("model_hint")
        if hint:
            rep["library_matches"] = [i["code"] for i in self.library.search(
                "CYL-" + hint if kind == "cylinder" else hint)][:5]
        sess = self.sessions.get(self.current) if self.current else None
        if sess is not None:
            d = sess.work / "calcs"
            d.mkdir(exist_ok=True)
            n = len(list(d.glob("*.json"))) + 1
            base = d / f"{n:02d}_{kind}"
            base.with_suffix(".json").write_text(json.dumps(rep, ensure_ascii=False, indent=1), encoding="utf-8")
            base.with_suffix(".md").write_text(calc_mod.to_markdown(rep), encoding="utf-8")
            rep["report"] = str(base.with_suffix(".md"))
            sess.log("calc", {"kind": kind, "report": rep["report"]})
        return rep

    def search_library(self, query: str = "", category: str | None = None) -> dict[str, Any]:
        return {"ok": True, "items": self.library.search(query, category)}

    # ---------- 修改现有模型 ----------
    def _work_file(self, file: str) -> Path:
        s = self._session()
        p = Path(file)
        p = (p if p.is_absolute() else s.work / p).resolve()
        if s.work.resolve() not in p.parents or not p.is_file():
            raise CadError(DOC_NOT_FOUND, f"{file} is not in this change session's workspace",
                           hint="先用 cad_change_begin(project, sources=[原文件或目录]) 把文件复制进工作区；可用文件："
                                + str(sorted(str(q.relative_to(s.work)) for q in s.work.rglob('*') if q.is_file())[:20]))
        return p

    def inspect_existing(self, file: str) -> dict[str, Any]:
        s = self._session()
        p = self._work_file(file)
        t = self._target(s.target_id)
        with t.lock:
            info = t.adapter.inspect(p)
            used = t.adapter.where_used(p, s.work)
        return {"ok": True, "file": str(p.relative_to(s.work)), **info,
                "used_by": [str(q.relative_to(s.work)) for q in used]}

    def edit_existing(self, file: str, edits: list[dict[str, Any]], dry_run: bool = False) -> dict[str, Any]:
        """修改工作区中的现有模型：校验 → 执行并重建 → 对比 → 更新引用它的装配/工程图。"""
        from ..edit import check_edits, describe, diff, parse_edits
        s = self._session()
        p = self._work_file(file)
        rel = str(p.relative_to(s.work))
        t = self._target(s.target_id)
        parsed = parse_edits(edits)
        with t.lock:
            before = t.adapter.inspect(p)
        findings = check_edits(before, parsed)
        summ = rules.summarize(findings)
        if not summ["passed"] or dry_run:
            return {"ok": summ["passed"], "applied": False, "file": rel, "findings": findings,
                    **({"hint": "修正 error 后重试；dry_run=false 才会执行"} if not summ["passed"] else {})}
        n = s.checkpoint(f"before edit {rel}")
        with t.lock:
            res = t.adapter.apply_edits(p, parsed)
        regen = res["regen"]
        if regen["status"] == "errors":
            s.log("edit_existing", {"file": rel, "edits": edits}, {"regen": regen})
            return {"ok": False, "applied": False, "file": rel, "regen": regen, "findings": findings,
                    "hint": "CAD 重建失败，文件未保存（保持修改前状态）；调整数值后重试"}
        d = diff(before, res["after"])
        refreshed = []
        with t.lock:
            for q in t.adapter.where_used(p, s.work):
                r = t.adapter.refresh(q)
                qr = str(q.relative_to(s.work))
                refreshed.append({"file": qr, **r})
                if r.get("status") == "errors":
                    findings.append({"rule": "E011", "severity": "error", "target": qr,
                                     "message": f"{qr} 更新失败：{r.get('errors')}"})
                if r.get("dangling"):
                    findings.append({"rule": "E010", "severity": "error", "target": qr,
                                     "message": f"{qr} 有 {r['dangling']} 个悬空标注（引用的特征被压缩/删除）",
                                     "hint": "在图上删除或重新标注；或撤销压缩（cad_change_rollback 到修改前检查点）"})
        rec = {"file": rel, "edits": [e.model_dump() for e in parsed], "diff": d, "regen": regen,
               "refreshed": refreshed, "findings": findings, "description": describe(rel, d), "checkpoint": n}
        s.edits.append(rec)
        s.log("edit_existing", {"file": rel, "edits": edits}, {"diff": d, "refreshed": [r["file"] for r in refreshed]})
        return {"ok": True, "applied": True, **{k: v for k, v in rec.items() if k != "checkpoint"},
                "rollback_to": n, "rules": rules.summarize(findings)}

    def writeback(self, confirm: bool = False) -> dict[str, Any]:
        """把已发布版本中改过的源文件回写到原位置（先备份原文件；原文件在此期间被别人改过则不覆盖）。"""
        import os
        import shutil

        from ..changeset import _sha
        s = self._session()
        if not s.committed:
            raise CadError(POLICY_DENIED, "commit the change first (cad_change_commit)", hint="回写只针对已发布的版本")
        rel_dir = Path(s.committed)
        plan, new_files = [], []
        for q in sorted(rel_dir.rglob("*")):
            if not q.is_file() or q.name == "change_report.json" or "_writeback_backup" in q.parts:
                continue
            rel = str(q.relative_to(rel_dir))
            if rel not in s.source_paths:
                if q.suffix.lower() not in (".json", ".md", ".svg", ".png", ".csv", ".jsonl") and "calcs" not in q.parts:
                    new_files.append(rel)
                continue
            if _sha(q) == s.source_hashes[rel]:
                continue  # 未修改
            orig = Path(s.source_paths[rel])
            item = {"file": rel, "to": str(orig)}
            try:
                self.policy.check_writeback(orig)
            except CadError as e:
                item.update(status="denied", reason=e.message, hint=e.hint)
                plan.append(item)
                continue
            if not orig.exists():
                item.update(status="conflict", reason="原文件已被移动或删除")
            elif _sha(orig) != s.source_hashes[rel]:
                item.update(status="conflict", reason="原文件在本次修改期间被别人改过，不覆盖",
                            hint="重新 cad_change_begin 基于最新文件修改")
            elif not os.access(orig, os.W_OK):
                item.update(status="readonly", reason="原文件只读", hint="在 PDM 中检出（Check Out）后重试")
            else:
                item["status"] = "ready"
            plan.append(item)
        blocked = [i["file"] for i in plan if i["status"] != "ready"]
        if confirm and blocked:  # 全部可写才执行，避免原位置出现"新图配旧零件"
            s.log("writeback", {"files": [i["file"] for i in plan]}, {"blocked": blocked})
            return {"ok": False, "confirmed": True, "written": False, "files": plan, "blocked_by": blocked,
                    "new_files_not_written": new_files,
                    "note": "有文件不能回写，为保持一致一个都没有写；处理后重试"}
        if confirm:
            for item in plan:
                if item["status"] != "ready":
                    continue
                orig = Path(item["to"])
                bak = rel_dir / "_writeback_backup" / item["file"]
                bak.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(orig, bak)
                shutil.copy2(rel_dir / item["file"], orig)
                ok = _sha(orig) == _sha(rel_dir / item["file"])
                item.update(status="written" if ok else "failed", backup=str(bak))
            s.log("writeback", {"files": [i["file"] for i in plan]}, {"result": plan})
        return {"ok": all(i["status"] in ("ready", "written") for i in plan), "confirmed": confirm, "files": plan,
                "new_files_not_written": new_files,
                "note": ("确认无误后用 confirm=true 执行回写" if not confirm else "已回写；PDM 中请检入（Check In）")
                if plan else "没有需要回写的已修改源文件"}

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
