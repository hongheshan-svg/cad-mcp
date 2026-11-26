"""Mock 适配器：不需要 CAD，可在 macOS/Linux/CI 上跑通完整流程。

模拟一个指定 CAD+版本（如 solidworks 2024），模型文件是带版本头的 JSON，
工程图渲染成 SVG。测量值 = IR 解析估算值。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .. import render_svg
from ..detailing import DrawingPlan
from ..errors import CAPABILITY_UNSUPPORTED, DOC_NOT_FOUND, CadError
from .base import CAPABILITIES, BuildResult, CadAdapter

MAGIC = "cadmcp-mock"


class MockAdapter(CadAdapter):
    def __init__(self, cad: str = "solidworks", version: str = "2024"):
        self.cad, self.version = cad, version
        self.ext = {"part": ".mprt", "assembly": ".masm", "drawing": ".mdrw"}
        self.open_docs: dict[str, dict[str, Any]] = {}

    def info(self) -> dict[str, Any]:
        return {"cad": self.cad, "version": self.version, "adapter": "mock", "revision": f"mock-{self.version}"}

    def capabilities(self) -> dict[str, str]:
        caps = {c: "full" for c in CAPABILITIES}
        caps.update({"interference.exact": "none", "export.step": "none", "export.dxf": "none",
                     "replace_component": "partial", "open_existing": "partial"})
        return caps

    def _write(self, path: Path, kind: str, body: dict[str, Any]) -> None:
        path.write_text(json.dumps({"magic": MAGIC, "cad": self.cad, "version": self.version, "kind": kind, **body},
                                   ensure_ascii=False, indent=1, default=str), encoding="utf-8")

    def _read(self, path: Path) -> dict[str, Any]:
        if not path.exists():
            raise CadError(DOC_NOT_FOUND, f"{path} not found")
        try:
            d = json.loads(path.read_text(encoding="utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise CadError(DOC_NOT_FOUND, f"{path} is not a mock CAD file") from None
        if d.get("magic") != MAGIC:
            raise CadError(DOC_NOT_FOUND, f"{path} is not a mock CAD file")
        return d

    def file_version(self, path: Path) -> tuple[str, str] | None:
        try:
            d = self._read(path)
        except CadError:
            return None
        return d["cad"], d["version"]

    def build_part(self, spec, resolved, out_dir: Path) -> BuildResult:
        path = out_dir / f"{spec.name}{self.ext['part']}"
        self._write(path, "part", {"name": spec.name, "summary": resolved.summary()})
        sx, sy, sz = resolved.bbox.size
        return BuildResult(path, {"status": "ok", "errors": [], "warnings": []},
                           {"mass_kg": round(resolved.mass_kg, 4), "bbox_mm": [round(sx, 3), round(sy, 3), round(sz, 3)],
                            "source": "estimate"})

    def build_assembly(self, spec, resolved, out_dir: Path, component_files) -> BuildResult:
        path = out_dir / f"{spec.name}{self.ext['assembly']}"
        self._write(path, "assembly", {
            "name": spec.name, "summary": resolved.summary(),
            "components": [{"id": i.id, "file": str(component_files.get(i.component, "")),
                            "transform": i.transform} for i in resolved.instances]})
        return BuildResult(path, {"status": "ok", "errors": [], "warnings": []},
                           {"mass_kg": round(resolved.mass_kg, 3),
                            "bbox_mm": [round(v, 2) for v in resolved.bbox.size], "source": "estimate"})

    def build_drawing(self, plan: DrawingPlan, model_file: Path, out_dir: Path, model) -> BuildResult:
        name = plan.name or f"{plan.model}_DRW"
        path = out_dir / f"{name}{self.ext['drawing']}"
        svg, rendered = render_svg.render(plan, model)
        svg_path = out_dir / f"{name}.svg"
        svg_path.write_text(svg, encoding="utf-8")
        self._write(path, "drawing", {"name": name, "model": str(model_file), "plan": plan.to_dict(),
                                      "preview": str(svg_path)})
        return BuildResult(path, {"status": "ok", "errors": [], "warnings": []},
                           extra={"rendered": rendered, "preview_svg": str(svg_path)})

    # ---- 修改现有模型（模拟）----
    # 旧文件的 "native" 段模拟 CAD 内部：features/dimensions/variables/equations/properties/material/
    # mass_kg/bbox_mm；mass_dims = 与质量成正比的尺寸；constraints = [{"lt": [a, b], "feature": F}]
    # 模拟重建失败（a 必须 < b）；工程图 native.references 为引用的文件名，annotations 为标在图上的尺寸。
    def _native(self, d: dict[str, Any]) -> dict[str, Any]:
        if "native" in d:
            return d["native"]
        s = d.get("summary") or {}
        return {"features": [], "dimensions": [], "equations": [], "properties": {},
                "variables": [{"name": k, "value": v} for k, v in (s.get("parameters") or {}).items()],
                "material": s.get("material"), "mass_kg": s.get("mass_kg"), "bbox_mm": s.get("bbox_mm")}

    def inspect(self, path: Path) -> dict[str, Any]:
        d = self._read(path)
        n = self._native(d)
        info = {"kind": d["kind"], "features": n.get("features", []), "dimensions": n.get("dimensions", []),
                "equations": n.get("equations", []), "variables": n.get("variables", []),
                "properties": [{"name": k, "value": v} for k, v in n.get("properties", {}).items()],
                "references": n.get("cs") or (d.get("summary") or {}).get("references", [])}
        if d["kind"] == "part":
            info.update(material=n.get("material"), mass_kg=n.get("mass_kg"), bbox_mm=n.get("bbox_mm"))
        else:
            info.update(files=n.get("references", []), components=n.get("components", []), mates=n.get("mates", []),
                        mass_kg=n.get("mass_kg"))
        return info

    def apply_edits(self, path: Path, edits: list[Any]) -> dict[str, Any]:
        import copy

        from ..expr import evaluate
        d = self._read(path)
        n = copy.deepcopy(self._native(d))
        dims = {x["name"]: x for x in n.get("dimensions", [])}
        vars_ = {x["name"]: x for x in n.get("variables", [])}
        before = {k: float(v["value"]) for k, v in dims.items()}
        errors = []
        for e in edits:
            if e.op == "set_dimension":
                dims[e.name]["value"] = e.value
            elif e.op == "set_variable":
                vars_[e.name]["value"] = e.value
            elif e.op == "set_property":
                n.setdefault("properties", {})[e.name] = e.value
            elif e.op == "suppress":
                names = {e.feature}
                for f in n.get("features", []):  # 子特征随父特征压缩
                    if f["name"] in names or set(f.get("parents", [])) & names:
                        names.add(f["name"])
                        if e.suppressed or f["name"] == e.feature:
                            f["suppressed"] = e.suppressed
            elif e.op == "set_material":
                n["material"] = e.material
            elif e.op == "replace_component":
                new = self._read(Path(e.file))
                nn = self._native(new)
                new_refs = nn.get("cs") or (new.get("summary") or {}).get("references", [])
                new_mass = e.mass_kg if e.mass_kg is not None else (nn.get("mass_kg") or 0.0)
                old_file = next(c["file"] for c in n["components"] if c["instance"] == e.instance)
                sel = [c for c in n["components"] if c["instance"] == e.instance or (e.all_instances and c["file"] == old_file)]
                for c in sel:
                    n["mass_kg"] = round((n.get("mass_kg") or 0) - c.get("mass_kg", 0) + new_mass, 4)
                    c.update(file=Path(e.file).name, code=e.code or Path(e.file).stem, mass_kg=new_mass)
                    for m in n.get("mates", []):
                        ref = m.get("a_ref") if m["a"] == c["instance"] else m.get("b_ref") if m["b"] == c["instance"] else None
                        if ref and ref not in new_refs:
                            errors.append(f"{m['name']}: 配合断开（新件没有 {ref}）")
                refs = n.setdefault("references", [])
                if Path(e.file).name not in refs:
                    refs.append(Path(e.file).name)
                if not any(c["file"] == old_file for c in n["components"]) and old_file in refs:
                    refs.remove(old_file)
        params = {k: float(v["value"]) for k, v in vars_.items()}
        for eq in n.get("equations", []):  # 方程式驱动尺寸
            if eq["lhs"] in dims:
                dims[eq["lhs"]]["value"] = float(evaluate(eq["rhs"].replace('"', ""), params))
        for c in n.get("constraints", []):
            a, b = (float(dims[x]["value"]) if x in dims else float(x) for x in c["lt"])
            if not a < b:
                errors.append(f"{c['feature']}: 重建失败（{c['lt'][0]}={a:g} 须 < {c['lt'][1]}={b:g}）")
        if n.get("mass_kg") is not None:
            k = 1.0
            for x in n.get("mass_dims", []):
                if before.get(x):
                    k *= float(dims[x]["value"]) / before[x]
            from .. import materials
            dens = {k_: v["density"] for k_, v in materials.MATERIALS.items()}
            k *= dens.get(n.get("material"), 7.85) / dens.get(self._native(d).get("material"), 7.85)
            n["mass_kg"] = round(n["mass_kg"] * k, 4)
        regen = {"status": "errors" if errors else "ok", "errors": errors, "warnings": []}
        if not errors:  # 出错不保存：文件保持原样
            d["native"] = n
            path.write_text(json.dumps(d, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
        after = self.inspect(path) if not errors else {**self.inspect(path), "unsaved": True}
        return {"regen": regen, "after": after}

    def where_used(self, path: Path, search_dir: Path) -> list[Path]:
        out = []
        for q in sorted(search_dir.rglob("*")):
            if q == path or not q.is_file():
                continue
            try:
                d = self._read(q)
            except CadError:
                continue
            if d["kind"] in ("assembly", "drawing") and path.name in self._native(d).get("references", []):
                out.append(q)
        return out

    def refresh(self, path: Path) -> dict[str, Any]:
        d = self._read(path)
        n = self._native(d)
        dangling = 0
        for ref in n.get("references", []):
            try:
                m = self._native(self._read(path.parent / ref))
            except CadError:
                return {"status": "errors", "dangling": 0, "errors": [f"missing reference {ref}"]}
            sup = {f["name"] for f in m.get("features", []) if f.get("suppressed")}
            feat_of = {x["name"]: x.get("feature") for x in m.get("dimensions", [])}
            dangling += sum(1 for a in n.get("annotations", []) if feat_of.get(a) in sup)
        n["refreshed"] = True
        d["native"] = n
        path.write_text(json.dumps(d, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
        return {"status": "ok", "dangling": dangling, "exports": []}

    def open(self, path: Path, readonly: bool) -> dict[str, Any]:
        d = self._read(path)
        self.open_docs[str(path)] = d
        return {"kind": d["kind"], "name": d.get("name"), "summary": d.get("summary")}

    def regenerate(self, path: Path) -> dict[str, Any]:
        self._read(path)
        return {"status": "ok", "errors": [], "warnings": []}

    def export(self, path: Path, fmt: str, out: Path) -> Path:
        d = self._read(path)
        if fmt == "svg" and d["kind"] == "drawing":
            out.write_text(Path(d["preview"]).read_text(encoding="utf-8"), encoding="utf-8")
            return out
        if fmt == "json":
            out.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")
            return out
        raise CadError(CAPABILITY_UNSUPPORTED, f"mock adapter cannot export {fmt}",
                       hint="mock 只支持 svg(工程图)/json；STEP/PDF/DXF 需要真实 CAD 目标")

    def close_all(self) -> None:
        self.open_docs.clear()
