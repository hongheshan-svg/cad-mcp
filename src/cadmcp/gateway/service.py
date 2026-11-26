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
from ..ir.assembly_resolve import (fit_gaps, part_fits, part_revolve, ResolvedAssembly, SourceInfo, aabb_interference, interface_check,
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
        keys = ("start_part", "udf_dir", "library_root", "templates", "sheet_unit_scale", "view_map", "toolkit")
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
        # 铸件：零件建好后自动生成/更新毛坯件；零件图建好后自动出毛坯图
        if spec.kind == "part" and spec.casting is not None and spec.casting.blank_of is None:
            from .. import casting
            bdict, info = casting.blank_spec(spec, self._models()[spec.name].resolved)
            b = self._build(PartSpec.model_validate(bdict), cascade=True)
            result["blank"] = {"name": b["name"], "part_no": bdict["part_no"], "summary": b["summary"],
                               "rules": b["rules"], **info}
        elif spec.kind == "drawing":
            m = self._models().get(spec.model)
            cast = getattr(m.resolved, "casting", None) if m else None
            if cast and not cast.get("blank"):
                bd = spec.model_copy(update={"model": f"{spec.model}_BLANK", "name": None})
                b = self._build(bd, cascade=True)
                result["blank_drawing"] = {"name": b["name"], "sheet": b["sheet"], "rules": b["rules"]}
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
                if spec.casting is not None and spec.casting.blank_of is None:
                    from .. import casting
                    findings += casting.check(spec, res)
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
                findings += fit_gaps(res)
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
                    if spec.broken_view and not getattr(t.adapter, "supports_broken_view", True):
                        spec = spec.model_copy(update={"broken_view": False})  # 该 CAD 画全长，保证图纸坐标一致
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
                              mounts={f"CS_{m['name']}": m for m in p.mounts},
                              install_holes=[{"name": h.name, "dia": h.drill, "label": f"{len(h.instances)}×Φ{h.drill:g}",
                                              "pts": [tuple(i.xyz) for i in h.instances]}
                                             for h in p.holes if h.through and h.face in ("top", "bottom")
                                             and len(h.instances) >= 2 and not h.slot],
                              fits=part_fits(p), revolve=part_revolve(p)[0], gears=part_revolve(p)[1])
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
                              interface_frame=(self.library.items[lp.code].get("mount") or {}).get("frame"),
                              fits=lp.fits, revolve=lp.revolve, threads=lp.threads,
                              standard=lp.vendor if lp.category == "standard" and lp.vendor.upper().startswith(("GB", "JB", "ISO", "DIN"))
                              else "")
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
            pn, path = self._placeholder(t, ref, work, made)
            files[c.id] = path
        return sorted(made)

    def _placeholder(self, t: Target, ref: str, work: Path, made: dict[str, Path]) -> tuple[str, Path]:
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
        return pn, made[pn]

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

    def export_bom(self, name: str, profile: str = "default", fmt: str = "xlsx") -> dict[str, Any]:
        """多级 BOM + 汇总 BOM + 采购清单 + 原材料汇总 + 下料明细，按 ERP 导入格式映射列。"""
        from .. import bom_export as bx
        m = self._get(name)
        if m.kind != "assembly":
            raise CadError(SPEC_INVALID, f"{name} is not an assembly", hint="BOM 从装配导出")
        if fmt not in ("xlsx", "csv"):
            raise CadError(SPEC_INVALID, f"format {fmt!r}: xlsx or csv")
        profiles = {**bx.PROFILES, **self.policy.bom_profiles}
        if profile not in profiles:
            raise CadError(SPEC_INVALID, f"unknown BOM profile {profile!r}", hint=f"可用：{sorted(profiles)}")
        prof = profiles[profile]
        bad = [f for _, f in prof["columns"] if f not in bx.FIELDS]
        if bad:
            raise CadError(SPEC_INVALID, f"profile {profile}: unknown fields {bad}", hint=f"可用字段：{bx.FIELDS}")
        cmap = bx.load_code_map(self.policy.bom_code_map) if self.policy.bom_code_map else None
        coder = bx.Coder(self.policy.bom_code_rules, cmap)
        models = self._models()
        drawn = {models[x].resolved.part_no for d in models.values() if d.kind == "drawing"
                 for x in d.deps if x in models and models[x].kind != "drawing"}
        res = bx.export(self._bom_node(f"asm:{name}"), self._session().out, name, fmt, prof, coder, drawn)
        res["rules"] = rules.summarize(res["findings"])
        preview = {k: res[k] for k in ("summary", "purchase", "raw_material")}
        return {"ok": True, "files": res["files"], "file": res["files"][0], "lines": res["lines"], "profile": profile,
                "rules": res["rules"], "findings": res["findings"],
                "bom": res["summary"][:60], "purchase": preview["purchase"][:60], "raw_material": preview["raw_material"],
                "multilevel": [{k: r[k] for k in ("seq", "part_no", "title", "qty", "total_qty", "make_buy")}
                               for r in res["multilevel"][:120]]}

    def _bom_node(self, source: str, _depth: int = 0) -> Any:
        from .. import bom_export as bx
        if _depth > 20:
            raise CadError(SPEC_INVALID, "assembly nesting deeper than 20 (cycle?)")
        kind, _, ref = source.partition(":")
        models = self._models()
        if kind == "asm":
            a = models[ref].resolved
            n = bx.Node(source, "assembly", a.part_no, a.title, unit_mass_kg=a.mass_kg)
            # 装配图上的序号（序号按气泡顺时针编号，BOM 跟图纸走）
            drw = next((d.resolved for d in models.values() if d.kind == "drawing"
                        and getattr(d.resolved, "model", None) == ref and d.resolved.bom), None)
            items = {r["part_no"]: r["item"] for r in drw.bom} if drw else {}
            for r in a.bom():
                src = r.get("source") or ""
                if src.startswith(("part:", "asm:", "lib:")):
                    c = self._bom_node(src, _depth + 1)
                else:
                    c = bx.Node(src, r["kind"], r["part_no"], r["title"], r["spec"], r["material"], r["unit_mass_kg"])
                if r.get("note"):
                    c.note = r["note"]
                c.item = items.get(r["part_no"])
                n.children.append((c, r["qty"]))
            return n
        if kind == "part":
            p = models[ref].resolved
            n = bx.Node(source, "part", p.part_no, p.title, material=p.material, unit_mass_kg=p.mass_kg,
                        surface_treatment=p.surface_treatment or "", part=p,
                        spec="×".join(f"{v:g}" for v in (round(x, 1) for x in p.bbox.size)))
            blank = models.get(f"{ref}_BLANK")
            if p.casting and not p.casting.get("blank") and blank:
                b = blank.resolved
                n.children.append((bx.Node(f"part:{ref}_BLANK", "blank", b.part_no, b.title, material=b.material,
                                           unit_mass_kg=b.mass_kg, spec=b.properties.get("毛坯类型", "铸件")), 1))
            return n
        lp = self.library.get(ref)
        item = self.library.items[lp.code]
        pn = lp.code + "".join(f"-{k}{v:g}" for k, v in lp.parameters.items())
        spec = " ".join(x for x in (lp.model, lp.properties.get("规格", "")) if x)
        return bx.Node(source, lp.category, pn, lp.title, spec=spec, unit_mass_kg=lp.mass_kg,
                       vendor=item.get("vendor", "") if item.get("vendor") != "generic" else "", lib_code=pn,
                       material=lp.properties.get("材料", ""))

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
                                + str(sorted(q.relative_to(s.work).as_posix() for q in s.work.rglob('*') if q.is_file())[:20]))
        return p

    # ----- 引用查找（工作区外：policy.read_allow 下的共享盘/PDM 检出目录）-----
    def _ref_index(self, t: Target, root: Path) -> dict[str, list[str]]:
        """root 下所有装配/工程图 → 其引用（按修改时间增量更新，缓存到工作区根目录）。"""
        cache_file = self.policy.workspace_root / "where_used_index.json"
        if not hasattr(self, "_refcache"):
            try:
                self._refcache = json.loads(cache_file.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                self._refcache = {}
        exts = {t.adapter.ext["assembly"].lower(), t.adapter.ext["drawing"].lower()}
        out: dict[str, list[str]] = {}
        dirty = False
        for q in root.rglob("*"):
            if not q.is_file() or q.suffix.lower() not in exts:
                continue
            key = str(q.resolve())
            mt = q.stat().st_mtime
            hit = self._refcache.get(key)
            if not hit or hit[0] != mt:
                hit = [mt, t.adapter.references(q) or []]
                self._refcache[key] = hit
                dirty = True
            out[key] = hit[1]
        if dirty:
            cache_file.parent.mkdir(parents=True, exist_ok=True)
            cache_file.write_text(json.dumps(self._refcache, ensure_ascii=False), encoding="utf-8")
        return out

    def _external_users(self, t: Target, originals: list[Path]) -> tuple[list[Path], list[dict[str, Any]]]:
        """工作区外引用 originals 的装配/工程图（传递：零件 → 装配 → 装配图）。返回 (文件, 提示)。"""
        s = self._session()
        notes: list[dict[str, Any]] = []
        if not self.policy.read_allow:
            notes.append({"rule": "E015", "severity": "warning", "target": "policy.read_allow",
                          "message": "未配置 read_allow，无法检查共享盘上其他引用此文件的装配/工程图",
                          "hint": "在 policy 中配置设计库根目录；或把整个项目目录作为 sources 复制进来"})
            return [], notes
        index: dict[str, list[str]] = {}
        for root in self.policy.read_allow:
            if root.exists():
                index.update(self._ref_index(t, root))
        by_name: dict[str, list[tuple[str, str]]] = {}  # 反向索引：被引用文件名 → [(引用者, 存储路径)]
        for user, refs in index.items():
            for r in refs:
                by_name.setdefault(Path(r.replace("\\", "/")).name.lower(), []).append((user, r))
        found: list[Path] = []
        frontier = [o.resolve() for o in originals]
        seen = set(frontier)
        while frontier:
            tgt = frontier.pop(0)
            for user, r in by_name.get(tgt.name.lower(), []):
                up = Path(user)
                if up in seen:
                    continue
                rp = Path(r.replace("\\", "/"))
                cand = rp if rp.is_absolute() else up.parent / rp
                if cand.exists() and cand.resolve() != tgt:
                    continue  # 引用的是别处的同名文件
                if not cand.exists():
                    notes.append({"rule": "E016", "severity": "warning", "target": str(up),
                                  "message": f"{up.name} 引用的 {r} 找不到原路径，按同名判定为引用本文件",
                                  "hint": "确认它确实用的是这个零件"})
                seen.add(up)
                frontier.append(up)
                found.append(up)
        return found, notes  # 含已在工作区中的；调用方按 source_paths 过滤

    def _by_folder(self, files: list[Path]) -> dict[str, int]:
        """按项目文件夹（read_allow 根下第一级目录）汇总数量，便于判断影响了哪些项目。"""
        out: dict[str, int] = {}
        for q in files:
            root = next((r for r in self.policy.read_allow if r in q.resolve().parents), None)
            key = q.resolve().relative_to(root).parts[0] if root else str(q.parent)
            out[key] = out.get(key, 0) + 1
        return dict(sorted(out.items(), key=lambda kv: -kv[1]))

    def _import_users(self, users: list[Path]) -> list[str]:
        s = self._session()
        k, v = next(iter(s.source_paths.items()))
        src_root = Path(v).parents[len(Path(k).parts) - 1]
        out = []
        for q in users:
            q = q.resolve()
            if src_root in q.parents:
                rel = q.relative_to(src_root).as_posix()
            else:
                root = next(r for r in self.policy.read_allow if r in q.parents)
                rel = (Path("_ext") / root.name / q.relative_to(root)).as_posix()
            s.add_source(q, rel)
            out.append(rel)
        if out:
            s.log("import_dependents", {"files": out})
        return out

    def inspect_existing(self, file: str) -> dict[str, Any]:
        s = self._session()
        p = self._work_file(file)
        t = self._target(s.target_id)
        rel = p.relative_to(s.work).as_posix()
        with t.lock:
            info = t.adapter.inspect(p)
            used = t.adapter.where_used(p, s.work)
            orig = s.source_paths.get(rel)
            users, notes = self._external_users(t, [Path(orig)]) if orig else ([], [])
            known = {Path(v).resolve() for v in s.source_paths.values()}
            outside = [u for u in users if u not in known]
        return {"ok": True, "file": rel, **info, "used_by": [q.relative_to(s.work).as_posix() for q in used],
                "used_by_outside": [str(q) for q in outside[:50]],
                "used_by_outside_count": len(outside),
                "used_by_outside_by_folder": self._by_folder(outside),
                **({"notes": notes} if notes else {}),
                **({"hint": "修改时这些文件会自动复制进工作区一起更新和检查"} if outside else {})}

    def edit_existing(self, file: str, edits: list[dict[str, Any]], dry_run: bool = False,
                      save_as: str | None = None) -> dict[str, Any]:
        """修改工作区中的现有模型：校验 → 执行并重建 → 对比 → 更新引用它的装配/工程图。
        save_as：另存为新文件（新图号）再改，原件与引用它的文件都不受影响。"""
        import shutil

        from ..edit import check_edits, describe, diff, parse_edits
        s = self._session()
        p = self._work_file(file)
        rel = p.relative_to(s.work).as_posix()
        t = self._target(s.target_id)
        parsed = parse_edits(edits)
        with t.lock:
            before = t.adapter.inspect(p)
        extra: list[dict[str, Any]] = []
        for i, e in enumerate(parsed):
            if e.op == "replace_component":
                parsed[i], fs = self._resolve_replacement(t, e, before)
                extra += fs
        findings = check_edits(before, parsed) + extra
        # 引用文件：按目录规则分为 update（纳入并更新）/ report（只报告）/ frozen（已定版：拒绝直接改）
        orig = None if save_as else s.source_paths.get(rel)
        groups: dict[str, list[Path]] = {"update": [], "report": [], "frozen": []}
        if orig:
            with t.lock:
                users, notes = self._external_users(t, [Path(orig)])
            findings += notes
            for u in users:
                groups[self.policy.dependents_mode_for(u)].append(u)
            if len(users) > self.policy.max_import_dependents:
                return {"ok": False, "applied": False, "file": rel, "findings": findings,
                        "error": f"{len(users)} 个文件引用了它（上限 {self.policy.max_import_dependents}）",
                        "hint": "这是大范围共用件：建议走 PDM 变更流程；或调高 policy.max_import_dependents"}
            if groups["frozen"]:
                findings.append({"rule": "E017", "severity": "error", "target": rel,
                                 "message": f"{len(groups['frozen'])} 个已定版文件引用此零件（"
                                            + "、".join(f"{k} {v}" for k, v in self._by_folder(groups["frozen"]).items())
                                            + "），直接修改会改变已定版图纸",
                                 "hint": "用 save_as 另存为新图号后修改，再在本项目装配中 replace_component 换成新件"})
            if groups["report"]:
                findings.append({"rule": "E018", "severity": "warning", "target": rel,
                                 "message": f"{len(groups['report'])} 个引用文件按策略只报告、不更新（"
                                            + "、".join(f"{k} {v}" for k, v in self._by_folder(groups["report"]).items())
                                            + "）；回写后它们打开时会读到新零件，未经检查",
                                 "hint": "通知相关项目负责人复核；或用 save_as 另存新图号"})
        summ = rules.summarize(findings)
        dep = {k: {"count": len(v), "by_folder": self._by_folder(v), "files": [str(x) for x in v[:50]]}
               for k, v in groups.items() if v}
        if not summ["passed"] or dry_run:
            return {"ok": summ["passed"], "applied": False, "file": rel, "findings": findings,
                    **({"dependents": dep} if dep else {}),
                    **({"hint": "修正 error 后重试；dry_run=false 才会执行"} if not summ["passed"] else {})}
        if save_as:
            np_ = (s.work / save_as).resolve()
            if s.work.resolve() not in np_.parents or np_.suffix.lower() != p.suffix.lower():
                raise CadError(SPEC_INVALID, f"save_as {save_as!r}: must be a {p.suffix} file name inside the workspace")
            if np_.exists():
                raise CadError(SPEC_INVALID, f"save_as {save_as!r} already exists", hint="换一个新文件名（新图号）")
            np_.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(p, np_)
            p, rel = np_, np_.relative_to(s.work).as_posix()
        known = {Path(v).resolve() for v in s.source_paths.values()}
        imported = self._import_users([u for u in groups["update"] if u not in known])
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
            # 按引用层级刷新：先直接引用者（装配/零件图），再引用它们的（装配图）；已修改文件预先载入
            exts = {t.adapter.ext["assembly"].lower(), t.adapter.ext["drawing"].lower()}
            users_of: dict[str, list[Path]] = {}  # 工作区反向索引（只读一遍文件）
            for q in sorted(s.work.rglob("*"), key=lambda q: q.relative_to(s.work).as_posix()):  # 跨平台一致的顺序
                if q.is_file() and q.suffix.lower() in exts:
                    for r in t.adapter.references(q) or []:
                        users_of.setdefault(Path(r.replace("\\", "/")).name.lower(), []).append(q)
            order, frontier, loaded, seen = [], [p], [p], {p}
            while frontier:
                cur = frontier.pop(0)
                for q in users_of.get(cur.name.lower(), []):
                    if q not in seen:
                        seen.add(q)
                        order.append(q)
                        frontier.append(q)
            for q in order:
                r = t.adapter.refresh(q, preload=list(loaded))
                if q.suffix.lower() == t.adapter.ext["assembly"].lower():
                    loaded.append(q)
                qr = q.relative_to(s.work).as_posix()
                refreshed.append({"file": qr, **r})
                if r.get("status") == "errors":
                    findings.append({"rule": "E011", "severity": "error", "target": qr,
                                     "message": f"{qr} 更新失败：{r.get('errors')}"})
                if r.get("dangling"):
                    findings.append({"rule": "E010", "severity": "error", "target": qr,
                                     "message": f"{qr} 有 {r['dangling']} 个悬空标注（引用的特征被压缩/删除）",
                                     "hint": "在图上删除或重新标注；或撤销压缩（cad_change_rollback 到修改前检查点）"})
        rec = {"file": rel, "edits": [e.model_dump() for e in parsed], "diff": d, "regen": regen,
               "refreshed": refreshed, "imported_dependents": imported[:50], "imported_count": len(imported),
               "imported_by_folder": self._by_folder([Path(s.source_paths[x]) for x in imported]), "findings": findings, "description": describe(rel, d), "checkpoint": n}
        s.edits.append(rec)
        s.log("edit_existing", {"file": rel, "edits": edits}, {"diff": d, "refreshed": [r["file"] for r in refreshed]})
        if save_as:
            rec["saved_as"] = rel
        return {"ok": True, "applied": True, **{k: v for k, v in rec.items() if k != "checkpoint"},
                "rollback_to": n, "rules": rules.summarize(findings), **({"dependents": dep} if dep else {}),
                **({"hint": f"已另存为 {rel}：用 set_property 改图号，并在需要的装配中 replace_component 换成新件"}
                   if save_as else {})}

    def _resolve_replacement(self, t: Target, e, before: dict[str, Any]):
        """替换件来源 → 文件；库件没有原生模型时生成包络占位件；比对安装接口与配合基准。"""
        from ..edit import interface_check
        s = self._session()
        comp = next((c for c in before.get("components", []) if c["instance"] == e.instance), None)
        if comp is None:
            return e, []
        if e.source.startswith("lib:"):
            ref = e.source[4:]
            code = ref.partition("?")[0]
            if code not in self.library.items:
                raise CadError(DOC_NOT_FOUND, f"library item {code!r} not found", hint="用 cad_search_library 查询可用编码")
            path = t.adapter.library_path(code)
            if path is None:
                _, path = self._placeholder(t, ref, s.work, {})
            new_item = self.library.items[code]
            lp = self.library.get(ref)
            new_refs, mass = list(lp.references), lp.mass_kg
        else:
            path = self._work_file(e.source)
            code, mass = None, None
            new_item = self.library.items.get(path.stem)
            new_refs = t.adapter.inspect(path).get("references", [])
        old_code = comp.get("code") or Path(comp.get("file", "")).stem
        fs = interface_check(e.instance, self.library.items.get(old_code), new_item, before.get("mates", []), new_refs)
        return e.model_copy(update={"file": str(path), "code": code, "mass_kg": mass}), fs

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
        model_exts = {e.lower() for e in self._target(s.target_id).adapter.ext.values()}
        for q in sorted(rel_dir.rglob("*"), key=lambda q: q.as_posix()):
            if not q.is_file() or q.name == "change_report.json" or "_writeback_backup" in q.parts:
                continue
            rel = q.relative_to(rel_dir).as_posix()
            if rel not in s.source_paths:
                if q.suffix.lower() in model_exts:
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
        # 新模型文件（替换进来的件、占位件）：被回写的装配会引用它们，一起复制到原目录（不覆盖同名文件）
        if plan and new_files and s.source_paths:
            k, v = next(iter(s.source_paths.items()))
            root = Path(v).parents[len(Path(k).parts) - 1]
            for rel in new_files:
                dest = root / rel
                item = {"file": rel, "to": str(dest), "new": True}
                try:
                    self.policy.check_writeback(dest)
                    item["status"] = "conflict" if dest.exists() else "ready"
                    if dest.exists():
                        item["reason"] = "原目录已有同名文件，不覆盖"
                except CadError as e:
                    item.update(status="denied", reason=e.message, hint=e.hint)
                plan.append(item)
            new_files = []
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
                if not item.get("new"):
                    bak = rel_dir / "_writeback_backup" / item["file"]
                    bak.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(orig, bak)
                    item["backup"] = str(bak)
                orig.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(rel_dir / item["file"], orig)
                ok = _sha(orig) == _sha(rel_dir / item["file"])
                item["status"] = "written" if ok else "failed"
            # 回写后的文件里保存的引用可能指向工作区：改为原位置的同名文件
            by_name = {Path(i["to"]).name.lower(): Path(i["to"]) for i in plan if i["status"] == "written"}
            by_name.update({Path(v).name.lower(): Path(v) for v in s.source_paths.values()
                            if Path(v).name.lower() not in by_name})
            t_ = self._target(s.target_id)
            with t_.lock:
                for item in plan:
                    if item["status"] == "written":
                        item["retargeted"] = t_.adapter.retarget(Path(item["to"]), by_name)
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
