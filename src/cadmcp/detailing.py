"""出图规划（与 CAD 无关）：把"工程师出图时的决定"程序化。

输入 DrawingSpec + 解析后的零件/装配，输出 DrawingPlan：图幅、比例、视图布局、
尺寸方案（总体尺寸 + 孔坐标尺寸 + 孔标注）、技术要求、标题栏、明细表。
适配器只负责把 DrawingPlan 渲染成原生工程图（SW .SLDDRW / Creo .drw），
并在渲染后由 check_drawing 校验"该标的都标了"。
"""
from __future__ import annotations

import math
import re
from dataclasses import asdict, dataclass, field
from datetime import date
from typing import Any

from .errors import SPEC_INVALID, CadError
from .ir.assembly_resolve import ResolvedAssembly
from .ir.drawing import DrawingSpec
from .ir.resolve import Box, ResolvedPart

SHEETS = {"A4": (210, 297), "A3": (420, 297), "A2": (594, 420), "A1": (841, 594), "A0": (1189, 841)}  # A4 竖放
# GB/T 14690 优先比例
SCALES = [5.0, 2.0, 1.0, 0.5, 0.4, 0.25, 0.2, 0.1, 0.05, 0.02]  # 含允许比例 1:2.5、1:4
PRIMARY_SCALES = [5.0, 2.0, 1.0, 0.5, 0.2, 0.1, 0.05, 0.02]  # GB/T 14690 优先比例（零件图）
TITLE_BLOCK_H = 56  # 标题栏占用高度（mm，GB/T 10609.1 简化）
MARGIN = 10

# 视图 → (图面水平轴, 图面竖直轴)，基于零件坐标系；"-y" 表示该视图中 Y 轴向左（镜像）。
# 由观察方向推出：图面向右 = 视线方向 × 向上方向。
VIEW_AXES = {
    "front": ("x", "z"),    # 从 -Y 看
    "back": ("-x", "z"),    # 从 +Y 看
    "top": ("x", "y"),      # 从 +Z 看
    "bottom": ("-x", "y"),  # 从 -Z 看
    "left": ("-y", "z"),    # 从 -X 看（左视图）
    "right": ("y", "z"),    # 从 +X 看（右视图）
}


def vc(p3, axis: str) -> float:
    """三维点在视图轴上的坐标（带镜像符号）。"""
    v = p3["xyz".index(axis[-1])]
    return -v if axis[0] == "-" else v


def vrange(b: Box, axis: str) -> tuple[float, float]:
    lo, hi = getattr(b, axis[-1] + "min"), getattr(b, axis[-1] + "max")
    return (-hi, -lo) if axis[0] == "-" else (lo, hi)
# 孔所在面 → 优先显示该孔的视图（同向视图优先，其次对面视图：通孔两侧都是实形）
HOLE_FACE_VIEWS = {"top": ("top", "bottom"), "bottom": ("bottom", "top"), "front": ("front", "back"),
                   "back": ("back", "front"), "left": ("left", "right"), "right": ("right", "left")}
HOLE_FACE_VIEW = {k: v[0] for k, v in HOLE_FACE_VIEWS.items()}


def hole_view(face: str, vtypes: set[str]) -> str | None:
    return next((v for v in HOLE_FACE_VIEWS[face] if v in vtypes), None)


def scale_label(s: float) -> str:
    return f"{int(s)}:1" if s >= 1 else f"1:{int(round(1 / s))}"


def _extent(b: Box, axis: str) -> float:
    return {"x": b.xmax - b.xmin, "y": b.ymax - b.ymin, "z": b.zmax - b.zmin}[axis[-1]]


@dataclass
class Dim:
    view: str
    kind: str  # linear | ordinate | diameter | callout
    axis: str | None
    value: float | None
    text: str
    ref: str  # 被标注对象，用于校验
    at: tuple[float, float] | None = None  # 模型坐标（视图平面内）
    extra: dict[str, Any] | None = None  # gtol/datum/roughness：法向 normal、框格内容 cells 等


@dataclass
class ViewPlan:
    type: str
    scale: float
    size_mm: tuple[float, float]  # 图纸上的尺寸
    origin_on_sheet: tuple[float, float]  # 视图中心在图纸上的位置
    label: str = ""  # 断面图名称（如 A-A）
    target: str = ""  # 断面图对应的特征


@dataclass
class DrawingPlan:
    model: str
    model_kind: str
    sheet: str
    sheet_size: tuple[float, float]
    scale: float
    projection: str
    views: list[ViewPlan]
    dims: list[Dim]
    notes: list[str]
    title_block: dict[str, str]
    bom: list[dict[str, Any]] = field(default_factory=list)
    balloons: list[dict[str, Any]] = field(default_factory=list)
    sections: list[Any] = field(default_factory=list)  # section.SectionPlan（剖视图）
    section_skipped: bool = False  # 有内部结构但 sections="none"
    revisions: list[dict[str, str]] = field(default_factory=list)
    flat_pattern: tuple[float, float] | None = None
    name: str = ""  # 图纸文件名（默认 <model>_DRW）
    missing_views: list[str] = field(default_factory=list)  # 有孔但缺少能显示其实形的视图
    layout: str = "standard"  # standard | shaft（轴线水平 + 移出断面）
    cut_list: list[dict[str, Any]] = field(default_factory=list)  # 焊接件切割清单

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["scale_label"] = scale_label(self.scale)
        return d


ISO_FACTOR = 0.5  # 轴测图按半比例放置（辅助视图）
MIN_FILL = {"part": 0.28, "assembly": 0.15, "weldment": 0.2}  # 视图占可用区域的最低比例（装配图需给明细表留位）
MIN_FEATURE_ON_PAPER = 2.0  # 最小孔在图纸上至少 2 mm


def _view_size(b: Box, vtype: str, flat: tuple[float, float] | None) -> tuple[float, float]:
    if vtype == "flat_pattern" and flat:
        return flat
    if vtype == "iso":
        sx, sy, sz = b.size
        return (0.87 * (sx + sy) * ISO_FACTOR, (0.5 * (sx + sy) + sz) * ISO_FACTOR)
    h, v = VIEW_AXES[vtype]
    return (_extent(b, h), _extent(b, v))


# 投影布局。第一角（GB）：俯视图在主视图下方，左视图在主视图右方，右视图在左方，仰视图在上方。
CELLS = {
    "first_angle": {"front": (0, 0), "top": (0, 1), "bottom": (0, -1), "left": (1, 0), "right": (-1, 0),
                    "back": (2, 0), "iso": (1, 1), "flat_pattern": (1, 1)},
    "third_angle": {"front": (0, 0), "top": (0, -1), "bottom": (0, 1), "left": (-1, 0), "right": (1, 0),
                    "back": (2, 0), "iso": (1, -1), "flat_pattern": (1, 1)},
}


def _assign_cells(types: list[str], projection: str = "first_angle") -> list[tuple[int, int]]:
    pref = CELLS[projection]
    taken: set[tuple[int, int]] = set()
    out = []
    for t in types:
        cell = pref.get(t, (0, 0))
        n = 0
        while cell in taken:
            cell = (n % 3, n // 3)
            n += 1
        taken.add(cell)
        out.append(cell)
    c0, r0 = min(c for c, _ in out), min(r for _, r in out)
    return [(c - c0, r - r0) for c, r in out]


def _grid(b: Box, spec: DrawingSpec, flat):
    types = [v.type for v in spec.views]
    sizes = [_view_size(b, t, flat) for t in types]
    cells = _assign_cells(types, spec.projection)
    cols = max(c for c, _ in cells) + 1
    rows = max(r for _, r in cells) + 1
    col_w = [max((sizes[i][0] for i in range(len(types)) if cells[i][0] == c), default=0) for c in range(cols)]
    row_h = [max((sizes[i][1] for i in range(len(types)) if cells[i][1] == r), default=0) for r in range(rows)]
    return types, sizes, cells, col_w, row_h


GAP = 36  # 视图间距：容纳尺寸线与坐标尺寸


def _fits(sh: str, s: float, col_w, row_h) -> bool:
    W, H = SHEETS[sh]
    avail_w = W - 2 * MARGIN - GAP * len(col_w)
    avail_h = H - 2 * MARGIN - TITLE_BLOCK_H - GAP * len(row_h)
    return sum(col_w) * s <= avail_w and sum(row_h) * s <= avail_h


def _fill(sh: str, s: float, col_w, row_h) -> float:
    W, H = SHEETS[sh]
    avail_w = W - 2 * MARGIN - GAP * len(col_w)
    avail_h = H - 2 * MARGIN - TITLE_BLOCK_H - GAP * len(row_h)
    return (sum(col_w) * s * sum(row_h) * s) / (avail_w * avail_h)


def _choose(b: Box, col_w, row_h, sheet: str | None, min_feature: float, kind: str) -> tuple[str, float]:
    sheets = [sheet] if sheet and sheet != "auto" else list(SHEETS)
    if kind == "part":
        # 工程师习惯：能 1:1 就 1:1；小件或孔太小才放大；图幅优先 A4/A3
        tiny = max(b.size) <= 30 or (0 < min_feature < MIN_FEATURE_ON_PAPER)
        order = ([5.0, 2.0] if tiny else []) + [1.0, 0.5, 0.2, 0.1, 0.05, 0.02]
        for group in ([s for s in sheets if s in ("A4", "A3")], [s for s in sheets if s not in ("A4", "A3")]):
            for s in order:
                if s > 1 and min_feature * s < MIN_FEATURE_ON_PAPER and max(b.size) > 30:
                    continue
                for sh in group:
                    if _fits(sh, s, col_w, row_h) and (min_feature <= 0 or min_feature * s >= MIN_FEATURE_ON_PAPER
                                                        or s <= 1 and min_feature * s >= 1.0):
                        return sh, s
    # 装配：最小的图幅，要求比例不小于 1:5 且填充率达标（给明细表留位）
    candidates = []
    for sh in sheets:
        s = next((s for s in SCALES if s <= 1 and _fits(sh, s, col_w, row_h)), None)
        if s is None:
            continue
        candidates.append((sh, s))
        if s >= 0.2 and _fill(sh, s, col_w, row_h) >= MIN_FILL[kind]:
            return sh, s
    if candidates:
        return max(candidates, key=lambda c: (c[1], -list(SHEETS).index(c[0])))
    return sheets[-1], SCALES[-1]


def _layout(b: Box, spec: DrawingSpec, flat, sheet: str | None, min_feature: float = 0.0, kind: str = "part"):
    types, sizes, cells, col_w, row_h = _grid(b, spec, flat)
    sh, s = _choose(b, col_w, row_h, sheet, min_feature, kind)
    W, H = SHEETS[sh]
    views = []
    for i, t in enumerate(types):
        c, r = cells[i]
        cx = MARGIN + GAP / 2 + sum(col_w[:c]) * s + GAP * c + col_w[c] * s / 2 + 8
        cy = H - MARGIN - GAP / 2 - sum(row_h[:r]) * s - GAP * r - row_h[r] * s / 2
        views.append(ViewPlan(t, s, (sizes[i][0] * s, sizes[i][1] * s), (cx, cy)))
    return sh, s, views


AUTO_NOTE_KEYS = ("未注尺寸公差", "未注公差", "去毛刺", "锐边倒钝", "表面处理")


def _dedupe_notes(extra: list[str], auto: list[str], treatment: str | None) -> list[str]:
    """去掉与自动技术要求重复的条目（AI 常会重复写表面处理/去毛刺/未注公差）。"""
    out = []
    for e in extra:
        s = e.strip().rstrip("。.")
        s = s.split(".", 1)[1].strip() if s[:2].rstrip(".").isdigit() and "." in s[:3] else s
        if any(k in s for k in ("表面处理",)) and treatment:
            continue
        if any(k in s for k in ("未注尺寸公差", "未注公差")):
            continue
        if ("去毛刺" in s or "锐边倒钝" in s) and not any(k in s for k in ("倒角", "C0", "C1", "R0")):
            continue
        if s and s not in out:
            out.append(s)
    return out



# ---------------- 精度标注（基准、几何公差、粗糙度） ----------------
FACE_N3 = {"top": (0, 0, 1), "bottom": (0, 0, -1), "front": (0, -1, 0), "back": (0, 1, 0),
           "left": (-1, 0, 0), "right": (1, 0, 0)}
_SLOT = {"datum": 0.22, "roughness": 0.5, "gtol": 0.78}  # 同一面上三类符号沿边错开


def _face_anchor(face: str, views: list[ViewPlan], b: Box, kind: str):
    """面在哪个视图里积聚成边线：返回 (视图, 边上一点(u,w), 视图内外法向)。"""
    n3 = FACE_N3[face]
    order = sorted((v for v in views if v.type in VIEW_AXES), key=lambda v: v.type != "front")
    for v in order:
        ha, va = VIEW_AXES[v.type]
        comp = []
        for ax in (ha, va):
            c = n3["xyz".index(ax[-1])]
            comp.append(-c if ax[0] == "-" else c)
        if comp == [0, 0]:
            continue
        (u0, u1), (w0, w1) = vrange(b, ha), vrange(b, va)
        f = _SLOT[kind]
        u = u1 if comp[0] > 0 else u0 if comp[0] < 0 else u0 + f * (u1 - u0)
        w = w1 if comp[1] > 0 else w0 if comp[1] < 0 else w0 + f * (w1 - w0)
        return v.type, (u, w), (comp[0], comp[1])
    return None


def _precision_dims(p: ResolvedPart, views: list[ViewPlan], vtypes: set[str], shaft: bool) -> list[Dim]:
    dims: list[Dim] = []

    def anchor(target: str, kind: str):
        k, _, ref = target.partition(":")
        if k == "face":
            return _face_anchor(ref, views, p.bbox, kind)
        if k == "hole":
            h = next(x for x in p.holes if x.name == ref)
            view = hole_view(h.face, vtypes) or views[0].type
            ha, va = VIEW_AXES.get(view, ("x", "y"))
            inst = h.instances[0]
            u, w = vc(inst.xyz, ha), vc(inst.xyz, va)
            if kind == "roughness":
                return view, (u - h.max_dia / 2, w), (-1, 0)
            if kind == "datum":
                return view, (u, w - h.max_dia / 2), (0, -1)
            return view, (u, w), (0, -1)  # 框格附在孔标注下方
        i = int(ref)
        z0, z1 = p.step_z(i)
        d = p.shaft_steps[i][1]
        frac = {"datum": 0.5, "roughness": 0.7, "gtol": 0.3}[kind]
        if kind == "datum":  # 基准符号画在轴下方，避开键槽剖切符号（剖切线在键槽中点）
            cuts = [(f["z0"] + f["z1"]) / 2 for f in p.shaft_features if f["kind"] == "keyway" and f["step"] == i]
            if cuts:
                frac = max((0.5, 0.2, 0.8), key=lambda t: min(abs(z0 + t * (z1 - z0) - c) for c in cuts))
        return "front", (z0 + frac * (z1 - z0), (d / 2) if kind != "datum" else -(d / 2)), (0, 1 if kind != "datum" else -1)

    for d in p.datums:
        a = anchor(d["target"], "datum")
        if a:
            dims.append(Dim(a[0], "datum", None, None, d["name"], f"datum:{d['name']}", a[1], {"normal": a[2]}))
    for g in p.geotols:
        a = anchor(g["target"], "gtol")
        if a:
            dims.append(Dim(a[0], "gtol", None, g["value"], g["text"], f"gtol:{g['type']}@{g['target']}", a[1],
                            {"normal": a[2], "cells": [g["symbol"], g["zone"], *g["datums"]],
                             **({"attach": "callout"} if g["kind"] == "hole" else {})}))
    for r in p.roughness:
        a = anchor(r["target"], "roughness")
        if a:
            dims.append(Dim(a[0], "roughness", None, r["ra"], f"Ra {r['ra']:g}", f"ra:{r['target']}", a[1],
                            {"normal": a[2]}))
    return dims

def _notes_and_title(spec: DrawingSpec, p: ResolvedPart, sheet: str, scale: float, today: date | None):
    notes = ["技术要求："]
    n = 1
    if not p.casting.get("blank"):  # 铸件毛坯：尺寸公差按 GB/T 6414（见铸造要求），不倒钝去毛刺
        notes.append(f"1. 未注尺寸公差按 {spec.general_tolerance}。")
        notes.append("2. 锐边倒钝，去毛刺。")
        n = 3
    if p.surface_treatment and p.surface_treatment != "无":
        notes.append(f"{n}. 表面处理：{p.surface_treatment}。")
        n += 1
    if p.base_type == "shaft" and p.shaft_chamfer:
        notes.append(f"{n}. 未注倒角 C0.5。")
        n += 1
    if p.base_type.startswith("weldment"):
        ks = sorted({w["size"] for w in p.welds})
        k0 = max(ks, key=lambda k: sum(1 for w in p.welds if w["size"] == k)) if ks else 3
        proc = p.weld_options.get("process", "135")
        how = {"135": "CO2 气体保护焊（135），焊丝 ER50-6", "111": "焊条电弧焊（111），焊条 E4303",
               "141": "钨极氩弧焊（141）"}[proc]
        notes.append(f"{n}. {how}，坡口按 GB/T 985.1；未注焊缝 K{k0:g} 连续角焊缝。")
        notes.append(f"{n + 1}. 焊缝饱满，无夹渣、气孔、咬边、未焊透。")
        notes.append(f"{n + 2}. 焊后清渣去飞溅，校正变形；"
                     + ("整体去应力退火后再加工。" if p.weld_options.get("stress_relief") else "型材切口去毛刺。"))
        n += 3
        machined = [b["name"] for b in p.weld_bodies if b.get("machined")]
        if machined:
            notes.append(f"{n}. {'、'.join(machined)} 上表面焊后加工，平面度 0.1，孔位以加工面为基准。")
            n += 1
        if p.weld_options.get("end_caps") and p.weld_options.get("open_ends"):
            notes.append(f"{n}. 敞口管端（{len(p.weld_options['open_ends'])} 处）装塑料堵头。")
            n += 1
    for line in p.casting.get("notes", []):
        notes.append(f"{n}. {line}")
        n += 1
    if p.base_type == "sheet_metal" and p.bends:
        bd = p.bends[0]
        notes.append(f"{n}. 板厚 t{p.thickness:g}；未注折弯内半径 R{bd['radius']:g}；"
                     f"展开按 K={bd['k_factor']:g} 计算，下料前按本厂折弯工艺核对。")
        n += 1
    for extra in _dedupe_notes(spec.notes, auto=notes, treatment=p.surface_treatment):
        notes.append(f"{n}. {extra}")
        n += 1

    tb = {
        "name": p.title, "part_no": p.part_no, "material": p.material, "scale": scale_label(scale),
        "mass": f"{p.mass_kg:.3f} kg", "sheet": sheet, "projection": "第一角" if spec.projection == "first_angle" else "第三角",
        "surface_finish": ("" if p.casting.get("blank") else "cast") if p.casting else (spec.surface_finish or ""),
        "designer": "", "checker": "", "approver": "",
        "date": (today or date.today()).isoformat(),
    }
    tb.update(spec.title_block)
    return notes, tb


def plan_part_drawing(spec: DrawingSpec, p: ResolvedPart, today: date | None = None) -> DrawingPlan:
    if p.base_type == "shaft":
        return plan_shaft_drawing(spec, p, today)
    if p.base_type == "weldment":
        return plan_weldment_drawing(spec, p, today)
    b = p.bbox
    min_feat = min((h.drill for h in p.holes), default=0.0)
    # 焊接件尺寸大、孔是焊后加工的少数特征：按装配图方式选图幅（比例 ≥1:5 的最小图幅）
    kind = "weldment" if p.base_type == "weldment_body" else "part"
    sheet, scale, views = _layout(b, spec, p.flat_pattern, spec.sheet, 0.0 if kind == "weldment" else min_feat, kind)
    vtypes = {v.type for v in views}
    dims: list[Dim] = []
    if spec.dimensions != "none":
        # 总体尺寸：每个轴在第一个能看到它的视图里标一次
        done: set[str] = set()
        for v in views:
            if v.type not in VIEW_AXES:
                continue
            for axis in VIEW_AXES[v.type]:
                if axis[-1] in done:
                    continue
                if p.base_type == "shaft" and axis[-1] in ("x", "y"):
                    continue
                dims.append(Dim(v.type, "linear", axis, round(_extent(b, axis), 3), f"{_extent(b, axis):g}",
                                f"overall_{axis[-1]}"))
                done.add(axis[-1])
        if p.base_type == "shaft":
            z = 0.0
            for i, (L, d) in enumerate(p.shaft_steps):
                view = "front" if "front" in vtypes else next(iter(vtypes))
                dims.append(Dim(view, "diameter", None, d, f"Φ{d:g}", f"step{i}_dia"))
                dims.append(Dim(view, "linear", "z", L, f"{L:g}", f"step{i}_len", (0, z + L / 2)))
                z += L
        if p.profile and p.profile.data.get("corner_radius"):
            dims.append(Dim("top", "radius", None, p.profile.data["corner_radius"],
                            f"4×R{p.profile.data['corner_radius']:g}", "corner_radius"))
        # 孔：坐标尺寸（以左下角为基准，机加工常用）+ 孔标注
        missing_views: set[str] = set()
        ordinates: dict[tuple[str, str, float], Dim] = {}
        located: set[str] = set()
        for h in p.holes:
            view = hole_view(h.face, vtypes)
            if view is None:
                missing_views.add(HOLE_FACE_VIEW[h.face])
                view = next((v.type for v in views if v.type in VIEW_AXES), views[0].type)
            ha, va = VIEW_AXES.get(view, ("x", "y"))
            origin = {ha: vrange(b, ha)[0], va: vrange(b, va)[0]}  # 视图左下角为坐标基准
            if h.circular:  # 圆周均布：标分布圆中心坐标 + 分布圆直径，不逐孔标坐标
                c = h.circular
                from .ir.resolve import face_point
                cxyz = face_point(h.face, (c["center_u"], c["center_v"]), b, h.at)
                ptc = {ha: vc(cxyz, ha), va: vc(cxyz, va)}
                for ax in (ha, va):
                    val = round(ptc[ax] - origin[ax], 3)
                    ordinates.setdefault((view, ax, val), Dim(view, "ordinate", ax, val, f"{val:g}",
                                                              f"ord:{view}.{ax}={val:g}", (ptc[ha], ptc[va])))
                dims.append(Dim(view, "diameter", None, c["pcd"], f"Φ{c['pcd']:g} 分布圆", f"{h.name}.pcd",
                                (ptc[ha] + c["pcd"] / 2 * 0.7071, ptc[va] - c["pcd"] / 2 * 0.7071)))
                for inst in h.instances:
                    located.update({f"{h.name}[{inst.index}].{ha}", f"{h.name}[{inst.index}].{va}"})
            for inst in ([] if h.circular else h.instances):
                pt = {ha: vc(inst.xyz, ha), va: vc(inst.xyz, va)}
                for ax in (ha, va):
                    val = round(pt[ax] - origin[ax], 3)
                    key = (view, ax, val)
                    if key not in ordinates:  # 坐标标注：同一坐标值只标一次
                        ordinates[key] = Dim(view, "ordinate", ax, val, f"{val:g}", f"ord:{view}.{ax}={val:g}",
                                             (pt[ha], pt[va]))
                    located.add(f"{h.name}[{inst.index}].{ax}")
            if spec.hole_callouts:
                first = h.instances[0]
                dims.append(Dim(view, "callout", None, None, h.callout, f"{h.name}",
                                (vc(first.xyz, ha), vc(first.xyz, va))))
        if ordinates:
            for view in {k[0] for k in ordinates}:
                ha, va = VIEW_AXES.get(view, ("x", "y"))
                lo = (vrange(b, ha)[0], vrange(b, va)[0])
                for ax in (ha, va):
                    ordinates.setdefault((view, ax, 0.0), Dim(view, "ordinate", ax, 0.0, "0", f"ord:{view}.{ax}=0", lo))
            dims += sorted(ordinates.values(), key=lambda d: (d.view, d.axis, d.value))
        for pk in p.pockets:
            pr = pk["profile"]
            txt = f"{pr.bbox2d[1] - pr.bbox2d[0]:g}×{pr.bbox2d[3] - pr.bbox2d[2]:g}" + ("" if pk["through"] else f" 深{pk['depth']:g}")
            dims.append(Dim(hole_view(pk["face"], vtypes) or views[0].type, "callout", None, None, txt, pk["name"]))
        for f in p.finishes:
            txt = f"C{f['size']:g}" if f["type"] == "chamfer" else f"R{f['size']:g}"
            dims.append(Dim("front" if "front" in vtypes else views[0].type, "callout", None, None, txt, f["name"]))

    if spec.dimensions == "none":
        missing_views = set()
    else:
        dims += _precision_dims(p, views, vtypes, False)
        if "flat_pattern" in vtypes and p.flat_box:
            fu0, fu1, fv0, fv1 = p.flat_box
            dims.append(Dim("flat_pattern", "linear", "u", fu1 - fu0, f"{fu1 - fu0:.1f}", "flat_w"))
            dims.append(Dim("flat_pattern", "linear", "v", fv1 - fv0, f"{fv1 - fv0:.1f}", "flat_h"))
            for i, bd in enumerate(p.bends):
                word = "上折" if bd["direction"] == "up" else "下折"
                at = ((fu0 + fu1) / 2 * 0.4 + fu1 * 0.6, bd["flat_pos"]) if bd["flat_axis"] == "v" \
                    else (bd["flat_pos"], fv0 * 0.6 + (fv0 + fv1) / 2 * 0.4)
                dims.append(Dim("flat_pattern", "callout", None, bd["angle"],
                                f"{word} {bd['angle']:g}° R{bd['radius']:g}", f"bend{i}", at))
    notes, tb = _notes_and_title(spec, p, sheet, scale, today)
    plan = DrawingPlan(p.name, "part", sheet, SHEETS[sheet], scale, spec.projection, views, dims, notes, tb,
                       flat_pattern=p.flat_pattern)
    plan.missing_views = sorted(missing_views)
    plan.revisions = list(spec.revisions)
    _plan_sections(plan, spec, p)
    return plan



# ---------------- 焊接件图 ----------------
def _plan_sections(plan: "DrawingPlan", spec: DrawingSpec, p: ResolvedPart) -> None:
    """剖视图：auto 时有内部结构就把主视图画成全剖/阶梯剖；逐个指定时按指定。剖视图替代原视图位置并标 X-X。"""
    from . import section as sx
    if p.base_type != "extrude" or p.profile is None:
        return
    vt = {v.type for v in plan.views}
    if spec.sections == "none":
        plan.section_skipped = sx.needs_section(p)
        return
    secs = []
    if spec.sections == "auto":
        if {"front", "top"} <= vt:
            s = sx.auto_section(p)
            if s:
                secs.append(s)
    else:
        for i, sp in enumerate(spec.sections):
            if sp.view not in vt or "top" not in vt:
                raise CadError(SPEC_INVALID, f"section {i}: drawing needs views {sp.view!r} and 'top'",
                               hint="剖切线画在俯视图上，剖视图替代主视图/左视图；在 views 中加入它们")
            secs.append(sx.explicit_section(p, sp, sp.label or "ABCDEFGH"[i]))
    for s in secs:
        s.geometry = sx.geometry(p, s)
        s.cut_points = sx.cut_line(p, s)
        s.bbox = p.bbox
        for v in plan.views:
            if v.type == s.view:
                v.label = f"{s.label}-{s.label}"
    plan.sections = secs


def plan_weldment_drawing(spec: DrawingSpec, p: ResolvedPart, today: date | None = None) -> DrawingPlan:
    """焊接件图：通用视图 + 孔标注（复用零件规划），加杆件位置尺寸、焊缝符号、切割清单与序号。"""
    plan = plan_part_drawing(spec, _as_body(p), today)
    plan.layout = "weldment"
    plan.cut_list = [dict(r) for r in p.cut_list]
    # 相同规格的板上孔（如 4 块地脚板各 1 孔）合并为一处标注 "4×Φ14 通"
    holes = {h.name: h for h in p.holes}
    groups: dict[tuple[str, str], list[Dim]] = {}
    for d in plan.dims:
        if d.kind == "callout" and d.ref in holes:
            h = holes[d.ref]
            base = re.sub(r"^\d+×", "", h.callout)
            groups.setdefault((d.view, base), []).append(d)
    drop = set()
    for (_, base), ds in groups.items():
        if len(ds) > 1:
            n = sum(len(holes[d.ref].instances) for d in ds)
            ds[0].text = f"{n}×{base}"
            ds[0].ref = "+".join(d.ref for d in ds)
            drop.update(id(d) for d in ds[1:])
    plan.dims = [d for d in plan.dims if id(d) not in drop]
    views = [v for v in plan.views if v.type in VIEW_AXES]
    if spec.dimensions == "none" or not views:
        return plan
    main = views[0]
    ha, va = VIEW_AXES[main.type]
    b = p.bbox
    have = {d.ref for d in plan.dims}
    # 杆件位置：主视图上水平杆件的顶面高度、竖直杆件的外侧位置（坐标尺寸，基准为视图左下角）
    lo = {ha: vrange(b, ha)[0], va: vrange(b, va)[0]}
    for body in p.weld_bodies:
        if body["kind"] != "member":
            continue
        bb = Box(*body["box"])
        for ax in (ha, va):
            if body["axis"] == ax[-1]:
                continue  # 杆件沿该轴：长度由切割清单给出
            u0, u1 = vrange(bb, ax)
            val = round(u1 - lo[ax], 3) if ax == va else round(u0 - lo[ax], 3)
            ref = f"ord:{main.type}.{ax}={val:g}"
            if ref in have or val <= 0:
                continue
            at_u = (vrange(bb, ha)[0] + vrange(bb, ha)[1]) / 2 if ax == va else u0
            at_w = u1 if ax == va else (vrange(bb, va)[0] + vrange(bb, va)[1]) / 2
            plan.dims.append(Dim(main.type, "ordinate", ax, val, f"{val:g}", ref, (at_u, at_w)))
            have.add(ref)
    for ax in (ha, va):
        ref = f"ord:{main.type}.{ax}=0"
        if ref not in have:
            plan.dims.append(Dim(main.type, "ordinate", ax, 0.0, "0", ref, (lo[ha], lo[va])))
    # 焊缝符号：每种（焊脚, 形式）在主视图上标一处代表焊缝，注明处数；其余见技术要求
    groups: dict[tuple[float, str], list[dict[str, Any]]] = {}
    for w in p.welds:
        groups.setdefault((w["size"], w["type"]), []).append(w)
    used: list[tuple[float, float]] = []
    span = max(_extent(b, ha), _extent(b, va))
    for (k, kind), ws in sorted(groups.items()):
        def score(x):  # 离已标的焊缝符号尽量远，其次靠上、靠左
            c = (vc(x["center"], ha), vc(x["center"], va))
            far = min((math.dist(c, u) for u in used), default=span)
            return (round(far / span * 4), c[1], -c[0])
        w = max(ws, key=score)
        used.append((vc(w["center"], ha), vc(w["center"], va)))
        sx, sy = _project(main, p, tuple(w["center"]))
        plan.dims.append(Dim(main.type, "weld", None, k, f"K{k:g}", f"weld:K{k:g}:{kind}",
                             (vc(w["center"], ha), vc(w["center"], va)),
                             {"symbol": "fillet", "size": k, "all_around": kind == "all_around",
                              "both_sides": kind == "two_sides", "count": len(ws),
                              "process": p.weld_options.get("process", "135"),
                              "welds": [x["id"] for x in ws],
                              "sheet_at": (round(sx, 2), round(sy, 2)),
                              "sheet_pos": (round(sx + 10, 2), round(sy + 12, 2))}))
    # 切割清单序号：与装配图相同的排布规则（主视图四周、顺时针编号），切割清单序号随之重排
    from types import SimpleNamespace as NS
    shim = NS(bbox=p.bbox, instances=[NS(id=x["name"], world_bbox=Box(*x["box"])) for x in p.weld_bodies])
    rows = [{"item": r["item"], "part_no": r["spec"], "instances": list(r["bodies"]), "_r": r} for r in plan.cut_list]
    rows, plan.balloons = layout_balloons(rows, shim, main, plan.sheet_size)
    for r in rows:
        r["_r"]["item"] = r["item"]
    plan.cut_list.sort(key=lambda r: r["item"])
    return plan


def _as_body(p: ResolvedPart) -> ResolvedPart:
    """以普通零件规划焊接件视图（避免递归进入焊接件分支）。"""
    import dataclasses
    return dataclasses.replace(p, base_type="weldment_body")


# ---------------- 轴类零件图 ----------------
def _dia_text(d: float, tol: str | None) -> str:
    return f"Φ{d:g}{tol or ''}"


def plan_shaft_drawing(spec: DrawingSpec, p: ResolvedPart, today: date | None = None) -> DrawingPlan:
    """轴类按工程师习惯：主视图轴线水平、键槽朝前；每个键槽一个移出断面图（A-A、B-B…）放在主视图下方。
    尺寸：各轴段长度（链式）+ 总长、各段直径（带公差带）、键槽长度与定位、断面上的键宽和 d-t1、环槽标注、端部倒角。"""
    b = p.bbox
    Ltot = b.zmax - b.zmin
    D = b.xmax - b.xmin
    kws = [f for f in p.shaft_features if f["kind"] == "keyway"]
    sec_d = max((f["d"] for f in kws), default=0.0)
    col_w = [Ltot]
    row_h = [D] + ([sec_d] if kws else [])
    min_feat = min((f["width"] for f in p.shaft_features), default=0.0)
    sheet, scale = _choose(b, col_w, row_h, spec.sheet, min_feat, "part")
    W, H = SHEETS[sheet]
    x0 = MARGIN + GAP + 8
    cy_main = H - MARGIN - GAP / 2 - D * scale / 2 - 8
    views = [ViewPlan("front", scale, (Ltot * scale, D * scale), (x0 + Ltot * scale / 2, cy_main))]
    cy_sec = cy_main - D * scale / 2 - 44 - sec_d * scale / 2 - 6
    # 剖切字母不得与基准字母重复（GB/T 4458.6），并跳过 I、O
    used = {d["name"] for d in p.datums} | {"I", "O"}
    letters = [c for c in "ABCDEFGHIJKLMNOPQRSTUVWXYZ" if c not in used]
    for i, f in enumerate(kws):
        label = f"{letters[i]}-{letters[i]}"
        zc = (f["z0"] + f["z1"]) / 2
        views.append(ViewPlan("section", scale, (f["d"] * scale, f["d"] * scale), (x0 + zc * scale, cy_sec),
                              label=label, target=f["name"]))
    dims: list[Dim] = []
    if spec.dimensions != "none":
        z = 0.0
        for i, (L, d) in enumerate(p.shaft_steps):
            tol = p.shaft_tolerances[i] if p.shaft_tolerances else None
            dims.append(Dim("front", "diameter", None, d, _dia_text(d, tol), f"step{i}_dia", (z + L / 2, d)))
            if len(p.shaft_steps) > 1:
                dims.append(Dim("front", "linear", "z", L, f"{L:g}", f"step{i}_len", (z, z + L)))
            z += L
        dims.append(Dim("front", "linear", "z", Ltot, f"{Ltot:g}", "overall_z", (0.0, Ltot)))
        if p.shaft_chamfer:
            dims.append(Dim("front", "callout", None, p.shaft_chamfer, f"2×C{p.shaft_chamfer:g}", "chamfer",
                            (0.0, p.shaft_steps[0][1] / 2)))
        for f in p.shaft_features:
            sz0 = p.step_z(f["step"])[0]
            if f["kind"] == "keyway":
                dims.append(Dim("front", "linear", "z", f["length"], f"{f['length']:g}", f"{f['name']}_len",
                                (f["z0"], f["z1"])))
                if abs(f["z0"] - sz0) > 1e-6:
                    dims.append(Dim("front", "linear", "z", f["z0"] - sz0, f"{f['z0'] - sz0:g}", f"{f['name']}_pos",
                                    (sz0, f["z0"])))
                dims.append(Dim("section", "linear", "y", f["width"], f"{f['width']:g}N9", f"{f['name']}_w",
                                (f["d"], f["width"])))
                dims.append(Dim("section", "linear", "x", f["d"] - f["depth"], f"{f['d'] - f['depth']:g}",
                                f"{f['name']}_t", (f["d"], f["depth"])))
            else:
                dims.append(Dim("front", "callout", None, None, f["callout"], f["name"], ((f["z0"] + f["z1"]) / 2, f["d"] / 2)))
                if f["z0"] - sz0 > 1e-6:
                    dims.append(Dim("front", "linear", "z", f["z0"] - sz0, f"{f['z0'] - sz0:g}", f"{f['name']}_pos",
                                    (sz0, f["z0"])))
    if spec.dimensions != "none":
        dims += _precision_dims(p, views, {"front"}, True)
    notes, tb = _notes_and_title(spec, p, sheet, scale, today)
    return DrawingPlan(p.name, "part", sheet, SHEETS[sheet], scale, spec.projection, views, dims, notes, tb,
                       layout="shaft", revisions=list(spec.revisions))

def plan_assembly_drawing(spec: DrawingSpec, a: ResolvedAssembly, today: date | None = None) -> DrawingPlan:
    sheet, scale, views = _layout(a.bbox, spec, None, spec.sheet, kind="assembly")
    dims: list[Dim] = []
    if spec.dimensions != "none":
        done: set[str] = set()
        for v in views:
            for axis in VIEW_AXES.get(v.type, ()):
                if axis not in done:
                    dims.append(Dim(v.type, "linear", axis, round(_extent(a.bbox, axis), 2),
                                    f"{_extent(a.bbox, axis):g}", f"overall_{axis}"))
                    done.add(axis)
    bom = a.bom() if (spec.bom_table or spec.balloons) else []
    balloons: list[dict[str, Any]] = []
    if spec.balloons and bom:
        bom, balloons = layout_balloons(bom, a, views[0], SHEETS[sheet])
    if not spec.bom_table:
        bom = []
    notes = ["技术要求：", "1. 装配前零件清洗去毛刺。", "2. 螺钉按规定扭矩拧紧，重要连接涂螺纹胶。"]
    notes += [f"{i}. {t}" for i, t in enumerate(spec.notes, 3)]
    tb = {"name": a.title, "part_no": a.part_no, "material": "", "scale": scale_label(scale),
          "mass": f"{a.mass_kg:.2f} kg", "sheet": sheet,
          "projection": "第一角" if spec.projection == "first_angle" else "第三角",
          "designer": "", "checker": "", "approver": "", "date": (today or date.today()).isoformat()}
    tb.update(spec.title_block)
    return DrawingPlan(a.name, "assembly", sheet, SHEETS[sheet], scale, spec.projection, views, dims, notes, tb,
                       bom=bom, balloons=balloons, revisions=list(spec.revisions))


BALLOON_R = 4.0       # 序号圆半径（图纸 mm，GB/T 4458.2 细实线圆）
BALLOON_GAP = 10.0    # 相邻序号中心距
BALLOON_OFF = 14.0    # 序号离视图外框距离


def _project(view, a, p3: tuple[float, float, float]) -> tuple[float, float]:
    ha, va = VIEW_AXES[view.type]
    (ulo, uhi), (wlo, whi) = vrange(a.bbox, ha), vrange(a.bbox, va)
    cx, cy = view.origin_on_sheet
    return (cx + (vc(p3, ha) - (ulo + uhi) / 2) * view.scale, cy + (vc(p3, va) - (wlo + whi) / 2) * view.scale)


def layout_balloons(bom: list[dict[str, Any]], a, view, sheet_size) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """序号排布（GB/T 4458.2）：序号沿主视图四周排成整齐的行/列，按顺时针方向依次编号；
    明细表序号随之重排（只进明细表、图上没有的件排在最后）。同一边内按引出点位置排序，引线不交叉。
    返回 (重新编号的 bom, 序号列表[item, instance, anchor, pos, side])。"""
    if view.type not in VIEW_AXES:
        return bom, []
    cx, cy = view.origin_on_sheet
    w, h = view.size_mm
    W, H = sheet_size
    def rect(inst):
        b = inst.world_bbox
        p0, p1 = _project(view, a, (b.xmin, b.ymin, b.zmin)), _project(view, a, (b.xmax, b.ymax, b.zmax))
        return min(p0[0], p1[0]), max(p0[0], p1[0]), min(p0[1], p1[1]), max(p0[1], p1[1])

    rects = {i.id: rect(i) for i in a.instances}
    area = {k: (r[1] - r[0]) * (r[3] - r[2]) for k, r in rects.items()}
    chosen: list[tuple[float, float]] = []
    pts = []
    for r in bom:
        if not r["instances"]:
            continue
        inst = next(i for i in a.instances if i.id == r["instances"][0])
        x0, x1, y0, y1 = rects[inst.id]
        # 引出点：在零件投影内取点，避开更小零件的投影（被遮挡/指不清）和已选引出点，略偏向视图外侧（引线短）
        best, c = None, ((x0 + x1) / 2, (y0 + y1) / 2)
        for fu in (0.5, 0.3, 0.7, 0.15, 0.85):
            for fv in (0.5, 0.3, 0.7, 0.15, 0.85):
                q = (x0 + (x1 - x0) * fu, y0 + (y1 - y0) * fv)
                cover = sum(1 for k, rr in rects.items() if k != inst.id and area[k] <= area[inst.id] * 1.5
                            and rr[0] - 1 <= q[0] <= rr[1] + 1 and rr[2] - 1 <= q[1] <= rr[3] + 1)
                near = min((math.hypot(q[0] - o[0], q[1] - o[1]) for o in chosen), default=99.0)
                edge = min(q[0] - (cx - w / 2), (cx + w / 2) - q[0], q[1] - (cy - h / 2), (cy + h / 2) - q[1])
                score = cover * 100 + max(0.0, 8 - near) * 10 + edge * 0.05
                if best is None or score < best - 1e-9:
                    best, c = score, q
        chosen.append(c)
        dx, dy = (c[0] - cx) / max(w, 1), (c[1] - cy) / max(h, 1)
        side = ("right" if dx >= 0 else "left") if abs(dx) >= abs(dy) else ("top" if dy >= 0 else "bottom")
        pts.append({"row": r, "instance": inst.id, "anchor": (round(c[0], 2), round(c[1], 2)), "side": side})
    # 下方通常是明细表/技术要求，少用：底边序号并到左右两侧
    for p in pts:
        if p["side"] == "bottom":
            p["side"] = "right" if p["anchor"][0] >= cx else "left"
    balloons = []
    order = []
    for side in ("top", "right", "left"):  # 顺时针：上边左→右、右边上→下、左边下→上
        group = [p for p in pts if p["side"] == side]
        if side == "top":
            group.sort(key=lambda p: p["anchor"][0])
            along = [p["anchor"][0] for p in group]
            lo_lim, hi_lim = 15.0, W - 15.0
        else:
            group.sort(key=lambda p: -p["anchor"][1] if side == "right" else p["anchor"][1])
            along = [p["anchor"][1] for p in group]
            lo_lim, hi_lim = 15.0, H - 15.0
        n = len(group)
        if not n:
            continue
        span = BALLOON_GAP * (n - 1)
        mid = sum(along) / n
        start = min(max(mid - span / 2 if side == "top" else mid + span / 2, lo_lim + (0 if side == "top" else span)),
                    hi_lim - (span if side == "top" else 0))
        for k, p in enumerate(group):
            if side == "top":
                pos = (start + k * BALLOON_GAP, min(cy + h / 2 + BALLOON_OFF, H - 12))
            elif side == "right":
                pos = (min(cx + w / 2 + BALLOON_OFF, W - 12), start - k * BALLOON_GAP)
            else:
                pos = (max(cx - w / 2 - BALLOON_OFF, 12), start - span + k * BALLOON_GAP)
            p["pos"] = (round(pos[0], 2), round(pos[1], 2))
            order.append(p)
    new_bom, item = [], 1
    for p in order:
        p["row"]["item"] = item
        balloons.append({"item": item, "instance": p["instance"], "part_no": p["row"]["part_no"],
                         "anchor": p["anchor"], "pos": p["pos"], "side": p["side"]})
        new_bom.append(p["row"])
        item += 1
    for r in bom:
        if not r["instances"]:
            r["item"] = item
            new_bom.append(r)
            item += 1
    return new_bom, balloons


def check_bom_association(plan: "DrawingPlan", rendered: dict[str, Any]) -> list[dict[str, Any]]:
    """图上明细表、序号与方案（= 装配 BOM）是否一致：D011 明细表行、D012 序号缺失/重复/无对应行。"""
    out = []
    if "bom_rows" in rendered and plan.bom:
        want = {(int(r["item"]), str(r["part_no"]), int(r["qty"])) for r in plan.bom}
        got = set()
        for r in rendered["bom_rows"]:
            try:
                got.add((int(r[0]), str(r[1]), int(float(r[2]))))
            except (TypeError, ValueError):
                got.add((r[0], r[1], r[2]))
        if want != got:
            miss, extra = sorted(want - got, key=str), sorted(got - want, key=str)
            out.append({"rule": "D011", "severity": "error",
                        "message": f"图上明细表与装配 BOM 不一致：缺 {miss[:5]}，多/错 {extra[:5]}",
                        "fix": "检查零件文件的 PartNo 属性与 BOM 模板列；或重新出图"})
    if "balloon_items" in rendered and plan.balloons:
        items = [int(x) for x in rendered["balloon_items"] if str(x).isdigit()]
        want = {b["item"] for b in plan.balloons}
        miss = sorted(want - set(items))
        dup = sorted({i for i in items if items.count(i) > 1})
        orphan = sorted(set(items) - {int(r["item"]) for r in plan.bom}) if plan.bom else []
        if miss:
            out.append({"rule": "D012", "severity": "error", "message": f"序号 {miss} 没有标到图上",
                        "fix": "在主视图上补标，或检查该零件在主视图中是否可见"})
        if dup:
            out.append({"rule": "D012", "severity": "warning", "message": f"序号 {dup} 重复标注"})
        if orphan:
            out.append({"rule": "D012", "severity": "error", "message": f"序号 {orphan} 在明细表中没有对应行"})
    return out


def check_weldment_drawing(plan: "DrawingPlan", rendered: dict[str, Any]) -> list[dict[str, Any]]:
    """D013 切割清单与方案不一致（行数/数量/长度）；D014 焊缝符号没有全部标出。"""
    out = []
    if "cut_list_rows" in rendered and plan.cut_list:
        def key(spec_len: str, qty) -> tuple[int, float]:
            nums = [float(x) for x in re.findall(r"\d+(?:\.\d+)?", str(spec_len))]  # 长度或板件最大边
            try:
                return int(float(qty)), round(max(nums), 0) if nums else 0.0
            except (TypeError, ValueError):
                return -1, 0.0
        want = sorted(key(r["length_mm"] if r["kind"] == "member" else max(r["size_mm"]), r["qty"]) for r in plan.cut_list)
        got = sorted(key(x[1], x[2]) for x in rendered["cut_list_rows"])
        if want != got:
            out.append({"rule": "D013", "severity": "error",
                        "message": f"图上切割清单与方案不一致：方案 {len(want)} 行（数量,长度）{want[:4]}，图上 {got[:4]}",
                        "fix": "检查零件切割清单属性（LENGTH/Description）与切割清单模板；SW 中可能需要更新切割清单"})
    if "weld_symbols" in rendered:
        need = sum(1 for d in plan.dims if d.kind == "weld")
        if rendered["weld_symbols"] < need:
            out.append({"rule": "D014", "severity": "error",
                        "message": f"焊缝符号 {rendered['weld_symbols']} 个，方案要求 {need} 个",
                        "fix": "查看 oplog 中 weld_symbol 的失败原因；已退回注释的会在 notes 中"})
    return out


def check_drawing(plan: DrawingPlan, rendered: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """出图校验。rendered = 适配器回读的实际图纸内容（尺寸 ref 列表、视图列表等）；
    无回读时只校验方案本身。"""
    out = []

    def f(rule, sev, msg, fix=None):
        d = {"rule": rule, "severity": sev, "message": msg}
        if fix:
            d["fix"] = fix
        out.append(d)

    for key in ("name", "part_no", "scale", "date"):
        if not plan.title_block.get(key):
            f("D001", "error", f"标题栏缺少 {key}")
    if plan.model_kind == "part" and not plan.title_block.get("material"):
        f("D001", "error", "标题栏缺少材料")
    if not plan.title_block.get("designer"):
        f("D002", "warning", "标题栏未填设计人", "在 title_block 中填写 designer")
    if plan.scale <= SCALES[-1] and plan.model_kind == "part":
        f("D003", "warning", f"比例 {scale_label(plan.scale)} 过小，考虑拆分视图或加局部放大图")
    W, H = plan.sheet_size
    for v in plan.views:
        if v.size_mm[0] > W or v.size_mm[1] > H:
            f("D004", "error", f"视图 {v.type} 超出图幅")
    expected = {d.ref for d in plan.dims}
    if rendered is not None and "dim_refs" in rendered:
        missing = expected - set(rendered["dim_refs"])
        if missing:
            f("D005", "error", f"{len(missing)} 个尺寸/标注未渲染到图纸：{sorted(missing)[:8]}")
    elif rendered is not None and "dim_count" in rendered:
        want = sum(1 for d in plan.dims if d.kind in ("linear", "ordinate"))
        if rendered["dim_count"] < want:
            f("D005", "warning", f"图纸上尺寸 {rendered['dim_count']} 个，方案要求 {want} 个，请人工核对",
              "查看 PDF；必要时补标")
    if plan.missing_views:
        f("D010", "warning", f"有孔/型腔所在面缺少对应视图：{plan.missing_views}，已标在其他视图上",
          f"在 views 中加入 {plan.missing_views}")
    if rendered is not None and rendered.get("collisions"):
        c = rendered["collisions"]
        f("D009", "warning", f"{len(c)} 处标注文字重叠，如 {c[0][0]!r} 与 {c[0][1]!r}",
          "减少视图（views）、指定更大图幅（sheet）或调整孔位标注")
    if plan.model_kind == "part" and not any(d.kind == "linear" for d in plan.dims):
        f("D006", "error", "缺少总体尺寸")
    if plan.model_kind == "assembly" and not plan.bom:
        f("D007", "warning", "装配图没有明细表")
    if rendered is not None and plan.model_kind == "assembly":
        out += check_bom_association(plan, rendered)
    if rendered is not None and plan.layout == "weldment":
        out += check_weldment_drawing(plan, rendered)
    if plan.section_skipped:
        f("D015", "warning", "零件有沉孔/螺纹孔/盲孔/型腔等内部结构，主视图未剖开，只能靠虚线和标注表达",
          "sections 用 auto 或指定剖切位置")
    if rendered is not None and ("dim_refs" in rendered or "sections" in rendered):
        done = set(rendered.get("sections", [])) | {r[8:] for r in rendered.get("dim_refs", []) if r.startswith("section:")}
        for sec in plan.sections:
            if sec.label not in done:
                f("D016", "error", f"剖视图 {sec.label}-{sec.label} 未生成", "查看 oplog 中 section_cut 的失败原因")
    if plan.model_kind == "part" and not any("公差" in n for n in plan.notes):
        f("D008", "warning", "缺少未注公差说明")
    return out
