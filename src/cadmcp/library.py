"""标准件/外购件库。条目可带参数，如 lib:PRF-4040?L=600。"""
from __future__ import annotations

from dataclasses import dataclass, field
from importlib import resources
from typing import Any
from urllib.parse import parse_qsl

import yaml

from .errors import DOC_NOT_FOUND, SPEC_INVALID, CadError
from .expr import evaluate
from .ir.resolve import Box, ResolvedRef


@dataclass
class LibraryPart:
    code: str
    title: str
    category: str
    model: str
    mass_kg: float
    bbox: Box
    references: dict[str, ResolvedRef]
    parameters: dict[str, float] = field(default_factory=dict)
    properties: dict[str, str] = field(default_factory=dict)
    vendor: str = ""
    revolve: list[tuple[float, float, float, float]] = field(default_factory=list)  # 回转外形 (z0, z1, 外径, 内径)
    fits: list[dict[str, Any]] = field(default_factory=list)  # 配合面（轴承内/外圈），tol="bearing" 表示由轴承标准定


class Library:
    def __init__(self, items: list[dict[str, Any]]):
        self.items = {it["code"]: it for it in items}

    @classmethod
    def default(cls) -> "Library":
        text = resources.files("cadmcp.data").joinpath("library.yaml").read_text(encoding="utf-8")
        return cls(yaml.safe_load(text)["items"])

    def search(self, query: str = "", category: str | None = None) -> list[dict[str, Any]]:
        q = query.lower()
        res = []
        for it in self.items.values():
            if category and it["category"] != category:
                continue
            hay = " ".join(str(it.get(k, "")) for k in ("code", "title", "model", "vendor")).lower()
            if q in hay:
                res.append({"code": it["code"], "title": it["title"], "category": it["category"],
                            "parameters": it.get("parameters", {}), "references": sorted(it.get("references", {}))})
        return res

    def get(self, ref: str) -> LibraryPart:
        code, _, query = ref.partition("?")
        if code not in self.items:
            raise CadError(DOC_NOT_FOUND, f"library item {code!r} not found", hint="用 cad_search_library 查询可用编码")
        it = self.items[code]
        params = {k: float(v) for k, v in it.get("parameters", {}).items()}
        for k, v in parse_qsl(query):
            if k not in params:
                raise CadError(SPEC_INVALID, f"{code} has no parameter {k!r}", hint=f"parameters: {sorted(params)}")
            params[k] = float(v)
        ev = lambda v: evaluate(v, params)  # noqa: E731
        bb = Box(*[ev(v) for v in it["bbox"]])
        refs = {n: ResolvedRef(n, {"CS": "csys", "AX": "axis", "MP": "plane", "PT": "point"}[n[:2]],
                               tuple(ev(c) for c in r["origin"]), tuple(ev(c) for c in r.get("rotate", (0, 0, 0))),
                               tuple(ev(c) for c in r["direction"]) if "direction" in r else None)
                for n, r in it.get("references", {}).items()}
        refs.setdefault("CS_Origin", ResolvedRef("CS_Origin", "csys", (0.0, 0.0, 0.0), auto=True))
        mass = it["mass_kg"] if "mass_kg" in it else it["mass_per_mm"] * params.get("L", 0)
        model = it["model"].format(**{k: f"{v:g}" for k, v in params.items()})
        fits = [{"role": f["role"], "d": ev(f["d"]), "tol": f.get("tol"), "name": f.get("name", f["role"]),
                 "p0": tuple(ev(c) for c in f["p0"]), "p1": tuple(ev(c) for c in f["p1"])} for f in it.get("fits", [])]
        return LibraryPart(code, it["title"], it["category"], model, mass, bb, refs, params,
                           {k: str(v) for k, v in it.get("properties", {}).items()}, it.get("vendor", ""),
                           [tuple(float(ev(c)) for c in seg) for seg in it.get("revolve", [])], fits)
