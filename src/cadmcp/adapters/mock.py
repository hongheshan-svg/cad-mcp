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
