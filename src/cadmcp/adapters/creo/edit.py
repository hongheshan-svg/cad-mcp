"""Creo 端"修改现有模型"：inspect / apply_edits / references / refresh（CAD 无关的编辑语义在 cadmcp.edit）。

只做翻译：把平台统一的 inspect 结构与 edit op 映射到 Creopyson（CREOSON）调用。
平台统一 mm / deg / kg：尺寸与质量按 Creo 模型单位换算（file_get_length_units / file_get_mass_units），
查不到单位时假设 mm / kg 并在返回的 warnings 中提示。
⚠ 字段名按 creopyson 0.7.x 源码与 CREOSON 文档编写，实机核对项见 docs/WINDOWS_TEST.md「Creo 修改现有模型」。
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from ...errors import CAPABILITY_UNSUPPORTED, NATIVE_ERROR, CadError

# Creo 长度单位名 → mm；质量单位名 → kg（小写，精确或前缀匹配）
LENGTH_TO_MM = {"mm": 1.0, "millimeter": 1.0, "cm": 10.0, "centimeter": 10.0, "m": 1000.0, "meter": 1000.0,
                "in": 25.4, "inch": 25.4, "ft": 304.8, "foot": 304.8}
MASS_TO_KG = {"kg": 1.0, "kilogram": 1.0, "g": 1e-3, "gram": 1e-3, "tonne": 1000.0, "ton": 1000.0,
              "lbm": 0.45359237, "pound": 0.45359237, "lb": 0.45359237}
SUPPRESSED_STATUS = {"SUPPRESSED", "PROGRAM_SUPPRESSED", "FAMILY_TABLE_SUPPRESSED", "SIMP_REP_SUPPRESSED", "INACTIVE"}
REF_DIM = re.compile(r"^rd\d+", re.I)  # Creo 参考（从动）尺寸默认名 rdN
EQ_LINE = re.compile(r"^\s*([A-Za-z_][\w:.]*)\s*=\s*(.+?)\s*$")


def _factor(unit: str | None, table: dict[str, float]) -> float | None:
    u = (unit or "").strip().lower()
    if not u:
        return None
    if u in table:
        return table[u]
    return next((v for k, v in table.items() if len(k) > 2 and u.startswith(k)), None)


def _kind(name: str) -> str:
    parts = name.lower().split(".")
    return {"prt": "part", "asm": "assembly", "drw": "drawing"}.get(parts[1] if len(parts) > 1 else "", "?")


class CreoEditor:
    def __init__(self, client: Any):
        self.c = client

    def open(self, path: Path, display: bool = True) -> None:
        self.c.creo_cd(str(path.parent))
        self.c.file_open(path.name, display=display, activate=display)

    def _units(self, name: str) -> tuple[float, float, list[str]]:
        """(长度→mm 系数, 质量→kg 系数, 警告)。"""
        warns: list[str] = []
        try:
            lf = _factor(self.c.file_get_length_units(file_=name), LENGTH_TO_MM)
        except Exception as e:  # noqa: BLE001
            lf = None
            warns.append(f"读取长度单位失败（{e}）")
        try:
            mf = _factor(self.c.file_get_mass_units(file_=name), MASS_TO_KG)
        except Exception as e:  # noqa: BLE001
            mf = None
            warns.append(f"读取质量单位失败（{e}）")
        if lf is None:
            lf = 1.0
            warns.append("无法确定模型长度单位，按 mm 处理；若模型是 inch 等单位，尺寸与修改值会错")
        if mf is None:
            mf = 1.0
            warns.append("无法确定模型质量单位，按 kg 处理")
        return lf, mf, warns

    # ---- inspect ----
    def inspect(self, path: Path) -> dict[str, Any]:
        name, c = path.name, self.c
        kind = _kind(name)
        try:
            self.open(path)
        except Exception as e:  # noqa: BLE001
            raise CadError(NATIVE_ERROR, f"Creo open {name} failed: {e}") from None
        lf, mf, warns = self._units(name)

        feats = []
        for f in c.feature_list(file_=name, inc_unnamed=True) or []:
            status = str(f.get("status", "ACTIVE")).upper()
            fid = f.get("feat_id", f.get("feature_id"))
            feats.append({"name": f.get("name") or f"id{fid}", "type": f.get("feat_type") or f.get("type") or "",
                          "suppressed": status in SUPPRESSED_STATUS, "status": status, "id": fid, "parents": []})

        dims = []
        try:
            raw = c.dimension_list_detail(file_=name)
        except Exception:  # noqa: BLE001  老版本 CREOSON 没有 list_detail
            raw = c.dimension_list(file_=name)
        for d in raw or []:
            if d.get("dwg_dim") and kind != "drawing":
                continue
            try:
                v = float(d.get("value"))
            except (TypeError, ValueError):
                continue  # 字符串/编码尺寸不参与数值修改
            ang = str(d.get("dim_type", "")).lower() == "angular"
            dims.append({"name": d["name"], "value": round(v if ang else v * lf, 6), "unit": "deg" if ang else "mm",
                         "feature": None, "driven": bool(REF_DIM.match(d["name"]))})

        variables, props = [], []
        for p in c.parameter_list(file_=name) or []:
            ptype = str(p.get("type", "")).upper()
            if ptype in ("DOUBLE", "INTEGER"):
                variables.append({"name": p["name"], "value": p.get("value"), "equation": f"{p['name']} = {p.get('value')}"})
            elif ptype in ("STRING", "BOOL") and not p.get("encoded"):
                props.append({"name": p["name"], "value": str(p.get("value"))})

        equations = []
        try:
            for line in c.file_relations_get(file_=name) or []:
                m = EQ_LINE.match(str(line))
                if m:
                    equations.append({"lhs": m.group(1), "rhs": m.group(2), "index": len(equations)})
        except Exception as e:  # noqa: BLE001
            warns.append(f"读取关系式失败（{e}）：无法判断尺寸是否被关系式驱动")

        info: dict[str, Any] = {
            "kind": kind, "features": feats, "dimensions": dims, "equations": equations, "variables": variables,
            "properties": props,
            "references": [x["name"] for x in feats if str(x["type"]).upper().startswith(("DATUM", "COORD"))]}
        if kind == "assembly":
            info["components"] = self._components(name)
        if kind in ("part", "assembly"):
            try:
                info["mass_kg"] = round(float(c.file_massprops(file_=name)["mass"]) * mf, 4)
            except Exception as e:  # noqa: BLE001
                warns.append(f"读取质量失败（{e}）")
        if kind == "part":
            try:
                info["material"] = c.file_get_cur_material(file_=name) or ""
            except Exception:  # noqa: BLE001
                info["material"] = ""
            info["bbox_mm"] = None  # CREOSON 无现成包围盒命令，需扩展
        if warns:
            info["warnings"] = warns
        return info

    def _children(self, name: str) -> list[dict[str, Any]]:
        res = self.c.bom_get_paths(file_=name, paths=True, top_level=True) or {}
        node = res.get("children")
        if isinstance(node, dict):  # 根节点是装配自身，其 children 才是直接子件
            node = node.get("children", [])
        return list(node or [])

    def _components(self, name: str) -> list[dict[str, Any]]:
        out = []
        for ch in self._children(name):
            f = str(ch.get("file", ""))
            out.append({"instance": ch.get("seq_path") or f, "file": f, "code": f.split(".")[0],
                        "suppressed": str(ch.get("status", "ACTIVE")).upper() in SUPPRESSED_STATUS})
        return out

    # ---- references ----
    def references(self, path: Path) -> list[str] | None:
        kind = _kind(path.name)
        if kind not in ("assembly", "drawing"):
            return None
        try:
            self.open(path, display=False)
            if kind == "assembly":
                files = [str(ch.get("file", "")) for ch in self._children(path.name)]
            else:
                files = [str(m) for m in self.c.drawing_list_models(drawing=path.name) or []]
        except Exception as e:  # noqa: BLE001
            raise CadError(NATIVE_ERROR, f"Creo references of {path.name} failed: {e}") from None
        return list(dict.fromkeys(f for f in files if f))

    # ---- apply_edits ----
    def apply_edits(self, path: Path, edits: list[Any]) -> dict[str, Any]:
        name, c = path.name, self.c
        if any(e.op == "replace_component" for e in edits):
            raise CadError(CAPABILITY_UNSUPPORTED, "Creo: replace_component 暂不支持（creopyson 没有替换组件函数）",
                           hint="需要 CREOSON 扩展命令（J-Link 替换组件）；或在 Creo 中手工替换后再导入")
        before = self.inspect(path)
        lf, _, warns = self._units(name)
        units = {d["name"]: d["unit"] for d in before["dimensions"]}
        try:
            for e in edits:
                if e.op == "set_dimension":
                    v = e.value if units.get(e.name) == "deg" else e.value / lf
                    c.dimension_set(e.name, v, file_=name)
                elif e.op == "set_variable":
                    c.parameter_set(e.name, e.value, file_=name, no_create=True)
                elif e.op == "set_property":
                    c.parameter_set(e.name, e.value, file_=name, type_="STRING")
                elif e.op == "suppress":
                    (c.feature_suppress if e.suppressed else c.feature_resume)(file_=name, name=e.feature)
                elif e.op == "set_material":
                    c.file_set_cur_material(e.material, file_=name)
            regen = self._regenerate(name)
        except Exception as ex:  # noqa: BLE001
            regen = {"status": "errors", "errors": [str(ex)], "warnings": []}
        regen["warnings"] = [*regen["warnings"], *warns]
        if regen["status"] == "errors":
            self._discard(name)  # 不保存：把被改动的内存模型清出会话，磁盘文件保持原样
            return {"regen": regen, "after": before}
        c.file_save(file_=name)
        return {"regen": regen, "after": self.inspect(path)}

    def _regenerate(self, name: str) -> dict[str, Any]:
        try:
            self.c.file_regenerate(file_=name)
        except Exception as e:  # noqa: BLE001
            return {"status": "errors", "errors": [f"regenerate: {e}"], "warnings": []}
        return {"status": "ok", "errors": [], "warnings": []}

    def _discard(self, name: str) -> None:
        try:
            self.c.file_erase(file_=name)
        except Exception:  # noqa: BLE001
            pass

    # ---- refresh ----
    def refresh(self, path: Path, preload: list[Path] | None = None) -> dict[str, Any]:
        errors: list[str] = []
        try:
            for q in preload or []:
                self.open(q, display=False)  # 已在会话中的文件 Creo 直接复用内存版本
            self.open(path)
            self.c.file_regenerate(file_=path.name)
            self.c.file_save(file_=path.name)
        except Exception as e:  # noqa: BLE001
            errors.append(str(e))
        if _kind(path.name) == "assembly":
            self._discard(path.name)  # 装配下次从磁盘读取；零件留在会话中供上层装配引用
        # 悬空标注数：CREOSON 没有读取命令（需扩展），固定为 0
        return {"status": "errors" if errors else "ok", "dangling": 0, "exports": [], **({"errors": errors} if errors else {})}
