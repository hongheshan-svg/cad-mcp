"""BOM 导出（与 CAD 无关）：多级 BOM、汇总 BOM、外购件采购清单、原材料下料清单，按 ERP 导入格式映射列。

输入是已解析的装配树；输出 CSV（每张表一个文件）或 xlsx（一个工作簿多张表，无第三方依赖）。
物料编码：policy 可配 code_rules（模板）与 code_map（已有物料：图号/库编码 → ERP 编码的 CSV）。
"""
from __future__ import annotations

import csv
import math
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable
from xml.sax.saxutils import escape

MAKE_BUY = {"assembly": "自制", "part": "自制", "purchased": "外购", "standard": "标准件", "blank": "外协"}
UNIT = {"assembly": "套", "part": "件", "purchased": "件", "standard": "件", "blank": "件"}

# 字段名 → 说明（列映射里可用的字段）
FIELDS = {
    "seq": "层级序号（1.2.1）", "level": "层级（根=0）", "parent_no": "父项图号", "parent_code": "父项物料编码",
    "part_no": "图号/编码", "code": "物料编码", "title": "名称", "spec": "规格型号", "material": "材料",
    "make_buy": "物料属性", "kind": "类别", "qty": "单位用量", "total_qty": "总用量", "unit": "单位",
    "unit_mass_kg": "单重kg", "total_mass_kg": "总重kg", "surface_treatment": "表面处理", "vendor": "供应商/品牌",
    "code_status": "编码状态", "note": "备注", "drawing_item": "装配图序号",
}

PROFILES: dict[str, dict[str, Any]] = {
    "default": {"columns": [["序号", "seq"], ["图号/编码", "part_no"], ["名称", "title"], ["规格", "spec"],
                            ["材料", "material"], ["属性", "make_buy"], ["数量", "qty"], ["总数", "total_qty"],
                            ["单位", "unit"], ["单重kg", "unit_mass_kg"], ["表面处理", "surface_treatment"],
                            ["品牌", "vendor"], ["备注", "note"]]},
    # 通用 ERP 导入格式（父子项结构）：多数 ERP 的 BOM 导入模板都是 父项编码 + 子项编码 + 用量
    "erp": {"columns": [["父项物料编码", "parent_code"], ["子项物料编码", "code"], ["子项名称", "title"],
                        ["规格型号", "spec"], ["材质", "material"], ["物料属性", "make_buy"], ["计量单位", "unit"],
                        ["用量", "qty"], ["层级", "level"], ["图号", "part_no"], ["装配图序号", "drawing_item"],
                        ["备注", "note"]],
            "skip_root": True},
}

# 原材料规格（向上取整到常备规格）
PLATE_T = [1, 1.5, 2, 2.5, 3, 4, 5, 6, 8, 10, 12, 14, 16, 18, 20, 22, 25, 28, 30, 35, 40, 45, 50, 55, 60, 65, 70, 80,
           90, 100, 110, 120, 130, 140, 150]
BAR_D = [6, 8, 10, 12, 14, 16, 18, 20, 22, 25, 28, 30, 32, 35, 38, 40, 42, 45, 48, 50, 55, 60, 65, 70, 75, 80, 85, 90,
         95, 100, 110, 120, 130, 140, 150, 160, 180, 200]
SAW = 5.0      # 锯切/铣削下料单边余量
BAR_ADD = 3.0  # 棒料直径余量（车外圆）


@dataclass
class Node:
    """装配树节点（由服务层构造）。"""
    source: str
    kind: str
    part_no: str
    title: str
    spec: str = ""
    material: str = ""
    unit_mass_kg: float = 0.0
    surface_treatment: str = ""
    vendor: str = ""
    note: str = ""
    lib_code: str | None = None
    part: Any = None  # ResolvedPart（自制零件）
    children: list[tuple["Node", int]] = field(default_factory=list)
    item: int | None = None  # 在父装配的装配图上的序号（有装配图时）


def _ceil_std(v: float, table: list[float]) -> float:
    return next((x for x in table if x >= v - 1e-6), math.ceil(v))


def _f(v: float) -> str:
    return f"{v:g}"


class Coder:
    """物料编码：code_map 已有编码优先；否则按 code_rules 模板生成并标记为"新建"。"""

    def __init__(self, rules: dict[str, str] | None = None, code_map: dict[str, str] | None = None):
        self.rules = {"assembly": "{part_no}", "part": "{part_no}", "blank": "{part_no}",
                      "purchased": "{lib_code}", "standard": "{lib_code}", **(rules or {})}
        self.map = code_map
        self.new: list[str] = []

    def __call__(self, n: Node) -> tuple[str, str]:
        key = n.lib_code or n.part_no
        if self.map is not None:
            for k in (n.part_no, key):
                if k in self.map:
                    return self.map[k], "已有"
        code = self.rules.get(n.kind, "{part_no}").format(part_no=n.part_no, lib_code=n.lib_code or n.part_no,
                                                         title=n.title, material=n.material)
        if self.map is not None and key not in self.new:
            self.new.append(key)
        return code, "新建" if self.map is not None else ""


def load_code_map(path: str | Path) -> dict[str, str]:
    """CSV：第一列图号/库编码，第二列 ERP 物料编码（有表头）。"""
    out = {}
    with Path(path).open(encoding="utf-8-sig", newline="") as fh:
        rd = csv.reader(fh)
        next(rd, None)
        for row in rd:
            if len(row) >= 2 and row[0].strip():
                out[row[0].strip()] = row[1].strip()
    return out


def multilevel(root: Node, coder: Coder) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []

    def walk(n: Node, qty: int, total: int, level: int, seq: str, parent: dict[str, Any] | None):
        code, status = coder(n)
        r = {"seq": seq, "level": level, "parent_no": parent["part_no"] if parent else "",
             "parent_code": parent["code"] if parent else "", "part_no": n.part_no, "code": code,
             "code_status": status, "title": n.title, "spec": n.spec, "material": n.material,
             "make_buy": MAKE_BUY.get(n.kind, n.kind), "kind": n.kind, "qty": qty, "total_qty": total,
             "unit": UNIT.get(n.kind, "件"), "unit_mass_kg": round(n.unit_mass_kg, 4),
             "total_mass_kg": round(n.unit_mass_kg * total, 3), "surface_treatment": n.surface_treatment,
             "vendor": n.vendor, "note": n.note, "drawing_item": n.item if n.item is not None else "", "_node": n}
        rows.append(r)
        kids = n.children
        if kids and all(c.item is not None for c, _ in kids):  # 父装配有装配图：序号与图纸一致
            kids = sorted(kids, key=lambda cq: cq[0].item)
        for i, (c, q) in enumerate(kids, 1):
            no = c.item if c.item is not None else i
            walk(c, q, total * q, level + 1, f"{seq}.{no}" if seq else str(no), r)

    walk(root, 1, 1, 0, "", None)
    return rows


def summary(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """汇总 BOM：同一物料在各层级的总用量合并（不含根）。"""
    agg: dict[str, dict[str, Any]] = {}
    for r in rows[1:]:
        a = agg.setdefault(r["code"] or r["part_no"], {**{k: v for k, v in r.items() if not k.startswith("_")},
                                                         "total_qty": 0, "where_used": []})
        a["total_qty"] += r["total_qty"]
        wu = r["parent_no"] + (f"（序号{r['drawing_item']}）" if r["drawing_item"] != "" else "")
        if wu not in a["where_used"]:
            a["where_used"].append(wu)
    order = {"自制": 0, "外协": 1, "外购": 2, "标准件": 3}
    out = sorted(agg.values(), key=lambda r: (order.get(r["make_buy"], 9), r["part_no"]))
    for i, r in enumerate(out, 1):
        r["seq"] = str(i)
        r["qty"] = r["total_qty"]
        r["total_mass_kg"] = round(r["unit_mass_kg"] * r["total_qty"], 3)
        r["note"] = "用于 " + "、".join(r.pop("where_used"))
    return out


def purchase(sumrows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """采购清单：外购件、标准件、外协（铸件毛坯）。"""
    out = [r for r in sumrows if r["make_buy"] in ("外购", "标准件", "外协")]
    return [{**r, "seq": str(i)} for i, r in enumerate(out, 1)]


def raw_materials(rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """原材料下料：板材（机加工板件/钣金展开）、棒料（轴）、型材（焊接件切割清单）。
    返回 (下料明细, 按材料+规格汇总)。"""
    detail: list[dict[str, Any]] = []
    seen: dict[str, int] = {}
    for r in rows:
        n: Node = r["_node"]
        p = n.part
        if n.kind != "part" or p is None:
            continue
        if p.part_no in seen:  # 同一零件多处使用：数量累加
            for d in detail:
                if d["part_no"] == p.part_no:
                    d["qty"] += d["per_part"] * r["total_qty"]
            continue
        seen[p.part_no] = 1
        for d in _stock(p):
            d.update(part_no=p.part_no, title=p.title, per_part=d["qty"], qty=d["qty"] * r["total_qty"])
            detail.append(d)
    agg: dict[tuple[str, str, str], dict[str, Any]] = {}
    for d in detail:
        k = (d["form"], d["material"], d["spec"])
        a = agg.setdefault(k, {"form": d["form"], "material": d["material"], "spec": d["spec"], "unit": d["unit"],
                               "qty": 0, "amount": 0.0, "amount_unit": d["amount_unit"], "mass_kg": 0.0, "parts": []})
        a["qty"] += d["qty"]
        a["amount"] += d["amount"] * d["qty"]
        a["mass_kg"] += d["mass_kg"] * d["qty"]
        if d["part_no"] not in a["parts"]:
            a["parts"].append(d["part_no"])
    tot = sorted(agg.values(), key=lambda a: (a["form"], a["material"], a["spec"]))
    for i, a in enumerate(tot, 1):
        a.update(seq=str(i), amount=round(a["amount"], 3), mass_kg=round(a["mass_kg"], 2), parts="、".join(a["parts"]))
    for i, d in enumerate(detail, 1):
        d["seq"] = str(i)
    return detail, tot


def _stock(p) -> list[dict[str, Any]]:
    from . import materials
    rho = p.density  # kg/m³
    m = p.material
    al = m.upper().startswith(("AL", "ZL"))
    if p.casting:
        return []  # 铸件毛坯外协采购，见采购清单
    if p.weld_bodies:
        out = []
        for c in p.cut_list:
            if c["kind"] == "member":
                L = c["length_mm"]
                out.append({"form": "型材", "material": m, "spec": c["spec"], "size": f"L={_f(L)}", "unit": "根",
                            "qty": c["qty"], "amount": L / 1000, "amount_unit": "m",
                            "mass_kg": c["unit_area_mm2"] * L * rho * 1e-9})
            else:
                a, b = c["size_mm"]
                t = float(c["spec"].split("t")[-1])
                out.append({"form": "板材", "material": m, "spec": f"{'铝板' if al else '钢板'} δ{_f(t)}",
                            "size": f"{_f(a)}×{_f(b)}", "unit": "块", "qty": c["qty"], "amount": a * b / 1e6,
                            "amount_unit": "m²", "mass_kg": a * b * t * rho * 1e-9})
        return out
    if p.base_type == "sheet_metal" and p.flat_pattern:
        a, b = sorted(p.flat_pattern, reverse=True)
        t = p.thickness
        return [{"form": "板材", "material": m, "spec": f"{'铝板' if al else '钢板'} δ{_f(t)}",
                 "size": f"{_f(round(a, 1))}×{_f(round(b, 1))}（展开）", "unit": "块", "qty": 1,
                 "amount": a * b / 1e6, "amount_unit": "m²", "mass_kg": a * b * t * rho * 1e-9}]
    if p.shaft_steps:
        d = _ceil_std(max(dd for _, dd in p.shaft_steps) + BAR_ADD, BAR_D)
        L = sum(x for x, _ in p.shaft_steps) + 2 * SAW
        return [{"form": "棒料", "material": m, "spec": f"圆钢 Φ{_f(d)}" if not al else f"铝棒 Φ{_f(d)}",
                 "size": f"L={_f(L)}", "unit": "根", "qty": 1, "amount": L / 1000, "amount_unit": "m",
                 "mass_kg": math.pi * d * d / 4 * L * rho * 1e-9}]
    if materials.MATERIALS.get(m, {}).get("cast_min_wall") is not None:
        return []
    sx, sy, sz = sorted(p.bbox.size, reverse=True)
    t = _ceil_std(sz + (0 if sz in PLATE_T else 1), PLATE_T)  # 非标准厚度：留铣削量后取整到常备板厚
    a, b = sx + 2 * SAW, sy + 2 * SAW
    return [{"form": "板材", "material": m, "spec": f"{'铝板' if al else '钢板'} δ{_f(t)}",
             "size": f"{_f(a)}×{_f(b)}", "unit": "块", "qty": 1, "amount": a * b / 1e6, "amount_unit": "m²",
             "mass_kg": a * b * t * rho * 1e-9}]


def check(rows: list[dict[str, Any]], coder: Coder, drawn: set[str]) -> list[dict[str, Any]]:
    out = []
    titles: dict[str, set[str]] = {}
    for r in rows:
        titles.setdefault(r["part_no"], set()).add(r["title"])
    for pn, ts in titles.items():
        if len(ts) > 1:
            out.append({"rule": "B001", "severity": "error", "target": pn,
                        "message": f"图号 {pn} 对应多个不同名称 {sorted(ts)}，ERP 会合并成同一物料",
                        "hint": "检查图号是否重复"})
    made = sorted({r["part_no"] for r in rows if r["kind"] in ("part", "assembly", "blank")} - drawn)
    if made:
        out.append({"rule": "B002", "severity": "warning", "target": ",".join(made[:8]),
                    "message": f"{len(made)} 个自制件/部件没有工程图", "hint": "先 cad_build_drawing"})
    nov = sorted({r["part_no"] for r in rows if r["kind"] == "purchased" and not r["vendor"]})
    if nov:
        out.append({"rule": "B003", "severity": "warning", "target": ",".join(nov[:8]),
                    "message": "外购件未登记供应商/品牌，采购无法下单", "hint": "在标准件库中补充 vendor"})
    nom = sorted({r["part_no"] for r in rows if r["kind"] == "part" and not r["material"]})
    if nom:
        out.append({"rule": "B004", "severity": "error", "target": ",".join(nom), "message": "自制件没有材料"})
    if coder.new:
        out.append({"rule": "B005", "severity": "info", "target": ",".join(coder.new[:8]),
                    "message": f"{len(coder.new)} 个物料在 ERP 编码表中不存在，需新建物料档案",
                    "hint": "导出的编码为按规则生成的临时编码（编码状态=新建）"})
    return out


# ---------- 输出 ----------

def table(rows: list[dict[str, Any]], columns: list[list[str]]) -> list[list[Any]]:
    return [[h for h, _ in columns]] + [[r.get(f, "") for _, f in columns] for r in rows]


SUM_COLS = [["序号", "seq"], ["物料编码", "code"], ["图号/编码", "part_no"], ["名称", "title"], ["规格", "spec"],
            ["材料", "material"], ["属性", "make_buy"], ["总数", "total_qty"], ["单位", "unit"],
            ["单重kg", "unit_mass_kg"], ["总重kg", "total_mass_kg"], ["品牌", "vendor"], ["编码状态", "code_status"],
            ["备注", "note"]]
BUY_COLS = [["序号", "seq"], ["物料编码", "code"], ["名称", "title"], ["规格型号", "spec"], ["品牌/供应商", "vendor"],
            ["类别", "make_buy"], ["数量", "total_qty"], ["单位", "unit"], ["单重kg", "unit_mass_kg"],
            ["材料", "material"], ["编码状态", "code_status"], ["备注", "note"]]
RAW_COLS = [["序号", "seq"], ["形态", "form"], ["材料", "material"], ["规格", "spec"], ["数量", "qty"],
            ["单位", "unit"], ["用量", "amount"], ["用量单位", "amount_unit"], ["重量kg", "mass_kg"],
            ["用于零件", "parts"]]
CUT_COLS = [["序号", "seq"], ["零件图号", "part_no"], ["零件名称", "title"], ["材料", "material"], ["规格", "spec"],
            ["下料尺寸", "size"], ["数量", "qty"], ["单位", "unit"]]


def write_csv(path: Path, data: list[list[Any]]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        csv.writer(fh).writerows(data)


def write_xlsx(path: Path, sheets: list[tuple[str, list[list[Any]]]]) -> None:
    """最小 xlsx 写入（内联字符串，首行加粗冻结），不依赖第三方库。"""
    def col(i: int) -> str:
        s = ""
        i += 1
        while i:
            i, r = divmod(i - 1, 26)
            s = chr(65 + r) + s
        return s

    def cell(ref: str, v: Any, bold: bool) -> str:
        st = ' s="1"' if bold else ""
        if isinstance(v, bool) or v is None or v == "":
            return f'<c r="{ref}"{st}/>' if bold else ""
        if isinstance(v, (int, float)):
            return f'<c r="{ref}"{st}><v>{v:g}</v></c>' if isinstance(v, float) else f'<c r="{ref}"{st}><v>{v}</v></c>'
        return f'<c r="{ref}" t="inlineStr"{st}><is><t xml:space="preserve">{escape(str(v))}</t></is></c>'

    def sheet_xml(data: list[list[Any]]) -> str:
        widths = [max((len(str(r[j])) if j < len(r) else 0) for r in data) for j in range(len(data[0]))] if data else []
        cols = "".join(f'<col min="{j + 1}" max="{j + 1}" width="{min(60, max(6, w * 1.6 + 2)):.1f}" customWidth="1"/>'
                       for j, w in enumerate(widths))
        rows = "".join(f'<row r="{i + 1}">' + "".join(cell(f"{col(j)}{i + 1}", v, i == 0) for j, v in enumerate(r))
                       + "</row>" for i, r in enumerate(data))
        return ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
                '<sheetViews><sheetView workbookViewId="0"><pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" '
                'state="frozen"/></sheetView></sheetViews>'
                + (f"<cols>{cols}</cols>" if cols else "") + f"<sheetData>{rows}</sheetData></worksheet>")

    ns = "http://schemas.openxmlformats.org"
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml",
                   f'<?xml version="1.0" encoding="UTF-8"?><Types xmlns="{ns}/package/2006/content-types">'
                   '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
                   '<Default Extension="xml" ContentType="application/xml"/>'
                   '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
                   '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
                   + "".join(f'<Override PartName="/xl/worksheets/sheet{i}.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
                             for i in range(1, len(sheets) + 1)) + "</Types>")
        z.writestr("_rels/.rels", f'<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="{ns}/package/2006/relationships">'
                   f'<Relationship Id="rId1" Type="{ns}/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
                   "</Relationships>")
        z.writestr("xl/workbook.xml", f'<?xml version="1.0" encoding="UTF-8"?><workbook xmlns="{ns}/spreadsheetml/2006/main" '
                   f'xmlns:r="{ns}/officeDocument/2006/relationships"><sheets>'
                   + "".join(f'<sheet name="{escape(n[:31])}" sheetId="{i}" r:id="rId{i}"/>' for i, (n, _) in enumerate(sheets, 1))
                   + "</sheets></workbook>")
        z.writestr("xl/_rels/workbook.xml.rels", f'<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="{ns}/package/2006/relationships">'
                   + "".join(f'<Relationship Id="rId{i}" Type="{ns}/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet{i}.xml"/>'
                             for i in range(1, len(sheets) + 1))
                   + f'<Relationship Id="rId{len(sheets) + 1}" Type="{ns}/officeDocument/2006/relationships/styles" Target="styles.xml"/>'
                   "</Relationships>")
        z.writestr("xl/styles.xml", f'<?xml version="1.0" encoding="UTF-8"?><styleSheet xmlns="{ns}/spreadsheetml/2006/main">'
                   '<fonts count="2"><font><sz val="11"/><name val="等线"/></font><font><b/><sz val="11"/><name val="等线"/></font></fonts>'
                   '<fills count="2"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill></fills>'
                   '<borders count="1"><border/></borders><cellStyleXfs count="1"><xf/></cellStyleXfs>'
                   '<cellXfs count="2"><xf fontId="0"/><xf fontId="1" applyFont="1"/></cellXfs></styleSheet>')
        for i, (_, data) in enumerate(sheets, 1):
            z.writestr(f"xl/worksheets/sheet{i}.xml", sheet_xml(data))


def export(root: Node, out_dir: Path, base: str, fmt: str, profile: dict[str, Any], coder: Coder,
           drawn: set[str]) -> dict[str, Any]:
    rows = multilevel(root, coder)
    sumrows = summary(rows)
    buy = purchase(sumrows)
    cut, raw = raw_materials(rows)
    findings = check(rows, coder, drawn)
    ml_rows = rows[1:] if profile.get("skip_root") else rows
    sheets = [("多级BOM", table(ml_rows, profile["columns"])), ("汇总BOM", table(sumrows, SUM_COLS)),
              ("采购清单", table(buy, BUY_COLS)), ("原材料汇总", table(raw, RAW_COLS)), ("下料明细", table(cut, CUT_COLS))]
    files = []
    if fmt == "xlsx":
        f = out_dir / f"{base}_BOM.xlsx"
        write_xlsx(f, sheets)
        files.append(str(f))
    else:
        names = {"多级BOM": "BOM", "汇总BOM": "BOM_summary", "采购清单": "purchase", "原材料汇总": "raw_material",
                 "下料明细": "cutting"}
        for n, data in sheets:
            f = out_dir / f"{base}_{names[n]}.csv"
            write_csv(f, data)
            files.append(str(f))
    clean = [{k: v for k, v in r.items() if not k.startswith("_")} for r in rows]
    return {"files": files, "lines": len(rows) - 1, "multilevel": clean, "summary": sumrows, "purchase": buy,
            "raw_material": raw, "cutting": cut, "findings": findings}


Resolver = Callable[[str], Node]
