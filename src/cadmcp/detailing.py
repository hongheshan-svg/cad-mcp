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
    hole_table: list[dict[str, Any]] = field(default_factory=list)  # 孔表行：tag/x/y/size
    hole_table_view: str = ""
    hole_table_box: tuple[float, float, float, float] | None = None  # 孔表占位（图纸 mm：x0, y0, x1, y1）
    extra_findings: list[dict[str, Any]] = field(default_factory=list)  # 规划阶段发现的问题
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


RIGHT_COL_W = 185.0  # 右侧栏（标题栏 180 + 间隙）：技术要求、明细表、孔表都在这一栏


def _fits(sh: str, s: float, col_w, row_h) -> bool:
    """视图区放得下：① 全宽、标题栏以上；或 ② 右侧栏以左、全高（标题栏/明细表只占右侧栏）。"""
    W, H = SHEETS[sh]
    vw, vh = sum(col_w) * s + GAP * len(col_w), sum(row_h) * s + GAP * len(row_h)
    return (vw <= W - 2 * MARGIN and vh <= H - 2 * MARGIN - TITLE_BLOCK_H) or \
        (vw <= W - 2 * MARGIN - RIGHT_COL_W and vh <= H - 2 * MARGIN)


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
        if any(s <= s0 for _, s0 in candidates):
            continue  # 大图幅没换来更大比例：只会更空
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


OPPOSITE = {"top": "bottom", "bottom": "top", "front": "back", "back": "front", "left": "right", "right": "left"}


def _face_gaps(face: str, p: ResolvedPart | None, axis: str) -> list[tuple[float, float]]:
    """面上被孔口/型腔打断的区间（沿 axis，模型坐标）：符号引线不得指到空处。通孔也打断对面。"""
    if p is None:
        return []
    out = []
    for h in p.holes:
        if h.face == face or (h.through and OPPOSITE.get(h.face) == face):
            r = h.max_dia / 2 if h.face == face else h.drill / 2
            out += [(vc(i.xyz, axis) - r, vc(i.xyz, axis) + r) for i in h.instances]
    for pk in p.pockets:
        if (pk["face"] == face or (pk.get("through") and OPPOSITE.get(pk["face"]) == face)) \
                and pk["face"] in ("top", "bottom") and axis[-1] in "xy":
            vals = [vc((x, y, 0), axis) for x, y in pk["profile"].outline]
            out.append((min(vals), max(vals)))
    return out


def _face_anchor(face: str, views: list[ViewPlan], b: Box, kind: str, p: ResolvedPart | None = None):
    """面在哪个视图里积聚成边线：返回 (视图, 边上一点(u,w), 视图内外法向)。
    边上的点按符号类型错开（_SLOT），并避开孔口/型腔打断处（离空处 ≥2 mm）。"""
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
        ti = 0 if comp[0] == 0 else 1  # 沿边方向
        lo, hi = (u0, u1) if ti == 0 else (w0, w1)
        gaps = _face_gaps(face, p, (ha, va)[ti])

        # 实体段 = 边线减去空处（各留 2 mm），按 f 在实体段总长上取点：各类符号保持顺序且彼此错开
        segs = [(lo, hi)]
        for a, b_ in sorted(gaps):
            nxt = []
            for s0, s1 in segs:
                if b_ + 2 <= s0 or a - 2 >= s1:
                    nxt.append((s0, s1))
                    continue
                if a - 2 > s0:
                    nxt.append((s0, a - 2))
                if b_ + 2 < s1:
                    nxt.append((b_ + 2, s1))
            segs = nxt
        total = sum(s1 - s0 for s0, s1 in segs)
        t_ = lo + f * (hi - lo)
        if gaps and total > 0:
            d = f * total
            for s0, s1 in segs:
                if d <= s1 - s0:
                    t_ = s0 + d
                    break
                d -= s1 - s0
        u = u1 if comp[0] > 0 else u0 if comp[0] < 0 else t_
        w = w1 if comp[1] > 0 else w0 if comp[1] < 0 else t_
        return v.type, (u, w), (comp[0], comp[1])
    return None


def _precision_dims(p: ResolvedPart, views: list[ViewPlan], vtypes: set[str], shaft: bool) -> list[Dim]:
    dims: list[Dim] = []

    def anchor(target: str, kind: str):
        k, _, ref = target.partition(":")
        if k == "face":
            return _face_anchor(ref, views, p.bbox, kind, p)
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
        elif kind == "roughness":  # 环槽等的引线从槽口斜向右上：符号（文字在右）避开，离槽越远越好
            grooves = [(f["z0"] + f["z1"]) / 2 for f in p.shaft_features if f["kind"] == "groove"
                       and z0 - 1 <= (f["z0"] + f["z1"]) / 2 <= z1 + 1]
            if grooves:
                frac = max((0.7, 0.5, 0.3, 0.2), key=lambda t: round(min(abs(z0 + t * (z1 - z0) - c) for c in grooves), 3))
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


def prune_default_views(spec: DrawingSpec, p: ResolvedPart, min_feat: float) -> tuple[DrawingSpec, list[dict[str, Any]]]:
    """视图数量最少、表达完整（GB/T 17451）：用户没指定 views 时，
    - 拉伸类棱柱件左右两侧面上没有孔/型腔/凸台、也没有剖视落在左视图 → 左视图只重复主视图已表达的厚度，省略；
    - 轴测图是辅助视图：带它只能用更小比例（或更大图幅）时省略，保证主要视图清晰。
    返回（新 spec, info 级说明）。"""
    if "views" in spec.model_fields_set:
        return spec, []
    infos: list[dict[str, Any]] = []
    if p.base_type == "sheet_metal" and p.flat_box and all(v.type != "flat_pattern" for v in spec.views):
        from .ir.drawing import ViewSpec
        spec = spec.model_copy(update={"views": [*spec.views, ViewSpec(type="flat_pattern")]})
        infos.append({"rule": "D021", "severity": "info", "message": "钣金件默认加展开图（下料依据，标注展开尺寸与折弯线）",
                      "fix": "不需要时在 views 中显式列出视图"})
    elif p.base_type != "extrude":
        return spec, []
    types = [v.type for v in spec.views]
    side = {h.face for h in p.holes} | {pk["face"] for pk in p.pockets} | {b_.get("face", "top") for b_ in p.bosses}
    left_sec = isinstance(spec.sections, list) and any(s.view == "left" for s in spec.sections)
    if p.base_type == "extrude" and "left" in types and not side & {"left", "right"} and not left_sec:
        types.remove("left")
        infos.append({"rule": "D021", "severity": "info", "message": "省略左视图：左右侧面无特征，厚度已在主视图表达",
                      "fix": "需要时在 views 中显式列出 left"})
    if "iso" in types:
        with_iso = spec.model_copy(update={"views": [v for v in spec.views if v.type in types]})
        no_iso = spec.model_copy(update={"views": [v for v in spec.views if v.type in types and v.type != "iso"]})
        sh1, s1, _ = _layout(p.bbox, with_iso, p.flat_pattern, spec.sheet, min_feat, "part")
        sh2, s2, _ = _layout(p.bbox, no_iso, p.flat_pattern, spec.sheet, min_feat, "part")
        if s2 > s1:  # 只为更大比例让位；同比例换小图幅只会更挤
            types.remove("iso")
            infos.append({"rule": "D021", "severity": "info",
                          "message": f"省略轴测图：带轴测图只能用 {sh1} {scale_label(s1)}，去掉后可用 {sh2} {scale_label(s2)}",
                          "fix": "需要时在 views 中显式列出 iso（或指定更大 sheet）"})
    if not infos:
        return spec, []
    return spec.model_copy(update={"views": [v for v in spec.views if v.type in types]}), infos


def plan_part_drawing(spec: DrawingSpec, p: ResolvedPart, today: date | None = None) -> DrawingPlan:
    if p.base_type == "shaft":
        return plan_shaft_drawing(spec, p, today)
    if p.base_type == "weldment":
        return plan_weldment_drawing(spec, p, today)
    b = p.bbox
    min_feat = min((h.drill for h in p.holes), default=0.0)
    # 焊接件尺寸大、孔是焊后加工的少数特征：按装配图方式选图幅（比例 ≥1:5 的最小图幅）
    kind = "weldment" if p.base_type == "weldment_body" else "part"
    spec, view_infos = prune_default_views(spec, p, min_feat) if kind == "part" else (spec, [])
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
            r_ = p.profile.data["corner_radius"]
            u0_, u1_, v0_, v1_ = p.profile.bbox2d
            dims.append(Dim("top", "radius", None, r_, f"4×R{r_:g}", "corner_radius",
                            (u1_ - r_ * 0.293, v1_ - r_ * 0.293)))  # 右上圆角弧中点
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
            row = None if h.circular else _regular_row(h, ha, va)
            if row is not None:  # 等距一排孔：只标首孔坐标 + 链式节距 n×p(=L)（GB/T 4458.4 等距尺寸简化注法）
                ax_, n_, pitch, first = row
                L_ = round(pitch * (n_ - 1), 3)
                dims.append(Dim(view, "linear", ax_, L_, f"{n_ - 1}×{pitch:g}(={L_:g})", f"{h.name}.pitch",
                                extra={"pitch": True, "prefix": f"{n_ - 1}×{pitch:g}(=", "suffix": ")"}))
            for inst in ([] if h.circular else (h.instances[:1] if row else h.instances)):
                pt = {ha: vc(inst.xyz, ha), va: vc(inst.xyz, va)}
                for ax in (ha, va):
                    val = round(pt[ax] - origin[ax], 3)
                    key = (view, ax, val)
                    if key not in ordinates:  # 坐标标注：同一坐标值只标一次
                        ordinates[key] = Dim(view, "ordinate", ax, val, f"{val:g}", f"ord:{view}.{ax}={val:g}",
                                             (pt[ha], pt[va]))
                    located.add(f"{h.name}[{inst.index}].{ax}")
            if row is not None:
                located.update(f"{h.name}[{i.index}].{a}" for i in h.instances for a in (ha, va))
            if spec.hole_callouts:
                first = h.instances[0]
                dims.append(Dim(view, "callout", None, None, h.callout, f"{h.name}",
                                (vc(first.xyz, ha), vc(first.xyz, va))))
        # 多边形外形（L 形、凸字形等）：顶点坐标进坐标尺寸，壁厚/台阶才有尺寸（总长、总宽另有总体尺寸）
        if p.base_type == "extrude" and p.profile is not None and p.profile.shape == "polygon" and "top" in vtypes:
            ha, va = VIEW_AXES["top"]
            origin = {ha: vrange(b, ha)[0], va: vrange(b, va)[0]}
            top_ = {ha: vrange(b, ha)[1], va: vrange(b, va)[1]}
            for u, v_ in p.profile.data["points"]:
                pt = {ha: vc((u, v_, b.zmax), ha), va: vc((u, v_, b.zmax), va)}
                for ax in (ha, va):
                    if abs(pt[ax] - top_[ax]) < 1e-6:
                        continue  # 最大值 = 总体尺寸
                    val = round(pt[ax] - origin[ax], 3)
                    ordinates.setdefault(("top", ax, val), Dim("top", "ordinate", ax, val, f"{val:g}",
                                                               f"ord:top.{ax}={val:g}", (pt[ha], pt[va])))
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
            pv = hole_view(pk["face"], vtypes) or views[0].type
            at_ = None
            if pv in VIEW_AXES and pk["face"] in ("top", "bottom"):
                pha, pva = VIEW_AXES[pv]
                z_ = b.zmax if pk["face"] == "top" else b.zmin
                pts_ = [(vc((x, y, z_), pha), vc((x, y, z_), pva)) for x, y in pr.outline]
                at_ = max(pts_, key=lambda q: q[0] + q[1])  # 型腔轮廓右上角
            dims.append(Dim(pv, "callout", None, None, txt, pk["name"], at_))
        for f in p.finishes:
            txt = f"C{f['size']:g}" if f["type"] == "chamfer" else f"R{f['size']:g}"
            fv = "front" if "front" in vtypes else views[0].type
            at_ = None
            if fv in VIEW_AXES:
                fha, fva = VIEW_AXES[fv]
                at_ = (vrange(b, fha)[1], vrange(b, fva)[1])  # 视图右上角棱边
            dims.append(Dim(fv, "callout", None, None, txt, f["name"], at_))

    if spec.dimensions == "none":
        missing_views = set()
    else:
        dims += _precision_dims(p, views, vtypes, False)
        if "flat_pattern" in vtypes and p.flat_box:
            fu0, fu1, fv0, fv1 = p.flat_box
            dims.append(Dim("flat_pattern", "linear", "u", fu1 - fu0, f"{round(fu1 - fu0, 1):g}", "flat_w"))
            dims.append(Dim("flat_pattern", "linear", "v", fv1 - fv0, f"{round(fv1 - fv0, 1):g}", "flat_h"))
            for i, bd in enumerate(p.bends):  # 折弯线定位：自展开外轮廓起的基线尺寸（下料划线/折弯机挡料用）
                lo = fv0 if bd["flat_axis"] == "v" else fu0
                L = bd["flat_pos"] - lo
                dims.append(Dim("flat_pattern", "linear", "u" if bd["flat_axis"] != "v" else "v", L,
                                f"{round(L, 1):g}", f"flat_bend{i}", extra={"bend": i}))
            for i, bd in enumerate(p.bends):
                word = "上折" if bd["direction"] == "up" else "下折"
                at = ((fu0 + fu1) / 2 * 0.4 + fu1 * 0.6, bd["flat_pos"]) if bd["flat_axis"] == "v" \
                    else (bd["flat_pos"], fv0 * 0.6 + (fv0 + fv1) / 2 * 0.4)
                dims.append(Dim("flat_pattern", "callout", None, bd["angle"],
                                f"{word} {bd['angle']:g}° R{bd['radius']:g}", f"bend{i}", at))
    ht_rows, ht_view = ([], "") if spec.dimensions == "none" else _hole_table(dims, p, views, spec)
    notes, tb = _notes_and_title(spec, p, sheet, scale, today)
    plan = DrawingPlan(p.name, "part", sheet, SHEETS[sheet], scale, spec.projection, views, dims, notes, tb,
                       flat_pattern=p.flat_pattern)
    plan.hole_table, plan.hole_table_view = ht_rows, ht_view
    if ht_rows:
        plan.hole_table_box = _place_table(plan, HOLE_TABLE_W, 5 * (len(ht_rows) + 1) + 8)
    plan.missing_views = sorted(missing_views)
    plan.extra_findings += view_infos
    plan.revisions = list(spec.revisions)
    _plan_sections(plan, spec, p)
    _hole_ra_to_section(plan, p)
    if spec.dimensions != "none":
        pitch_dim_sheet(plan, p)
        place_labels(plan, p.bbox, part=p)
        precision_sheet(plan, p)
        overall_dim_sheet(plan, p)
        ordinate_dim_sheet(plan, p)
        flat_dim_sheet(plan, p)
    return plan



# ---------------- 焊接件图 ----------------
HOLE_TABLE_MIN = 16      # 一个视图孔数达到此值用孔表
HOLE_TABLE_ORD = 14      # 或某方向坐标值个数超过此值


HOLE_TABLE_W = 180.0


def right_column_bottom(plan: "DrawingPlan") -> float:
    """右侧栏（标题栏、技术要求、切割清单、"其余"粗糙度）已占到的高度。"""
    return 5 + 56 + 14 + 5 * len(plan.notes) + 6 + (7 * (len(plan.cut_list) + 1) + 16 if plan.cut_list else 0) + 16


GR_W, GR_H = 48.0, 10.0  # "其余 √Ra x (√)" 占位（render_gdt.general_roughness）


def notes_origin(plan: "DrawingPlan") -> tuple[float, float]:
    """技术要求文字块左下角（图纸 mm，同 render_svg：标题栏上方，最后一行基线）。"""
    W, _ = plan.sheet_size
    return (W - 5 - 180, 5 + 56 + 11)


def general_roughness_at(plan: "DrawingPlan") -> tuple[float, float]:
    """"其余"表面结构符号的位置（图纸 mm，左端/基线），SVG 与 CAD 共用。
    默认在技术要求（或右侧栏孔表）上方；与视图（含尺寸带）或孔表重叠时依次改放：同高度靠图框右边 → 默认 x 向上逐格 →
    图纸右上角。"""
    W, H = plan.sheet_size
    x0 = W - 5 - 180
    hb = plan.hole_table_box
    y0 = hb[3] + 4 if hb and hb[0] > W / 2 else (5 + 56 + 14 + 5 * len(plan.notes) + 6
                                                  + (7 * (len(plan.cut_list) + 1) + 16 if plan.cut_list else 0))
    obstacles = []
    for v in plan.views:
        cx, cy = v.origin_on_sheet
        vw, vh = v.size_mm
        obstacles.append((cx - vw / 2 - 6, cy - vh / 2 - 6, cx + vw / 2 + 14, cy + vh / 2 + 6))
    if hb:
        obstacles.append(tuple(hb))

    def free(x, y):
        box = (x, y - 2, x + GR_W, y + GR_H)
        return box[3] <= H - 6 and not any(_box_hit(box, o) > 0 for o in obstacles)

    cands = [(x0, y0), (W - 6 - GR_W, y0)] + [(x0, y0 + 4 * k) for k in range(1, 60)] + [(W - 6 - GR_W, H - 6 - GR_H)]
    return next(((round(x, 2), round(y, 2)) for x, y in cands if free(x, y)), (x0, y0))


def _place_table(plan: "DrawingPlan", w: float, h: float) -> tuple[float, float, float, float]:
    """表格放置：右侧栏技术要求上方 → 左下空白区；都放不下仍放右侧栏并标记（D018）。"""
    W, H = plan.sheet_size
    def rects(views):
        out = []
        for v in views:
            cx, cy = v.origin_on_sheet
            vw, vh = v.size_mm
            out.append((cx - vw / 2 - 28, cy - vh / 2 - 28, cx + vw / 2 + 16, cy + vh / 2 + 10))  # 含尺寸带
        return out

    yb = right_column_bottom(plan)
    cands = [(W - 5 - w, yb, W - 5, yb + h), (15.0, 12.0, 15.0 + w, 12.0 + h)]

    def fits(c, rs):
        return c[3] <= H - 10 and not any(_box_hit(c, r) > 0 for r in rs) and \
            not (c is cands[1] and c[2] > W - 5 - 180 - 4)

    for c in cands:
        if fits(c, rects(plan.views)):
            return c
    main = [v for v in plan.views if v.type != "iso"]
    if len(main) < len(plan.views):  # 轴测图是辅助视图：让位给孔表
        for c in cands:
            if fits(c, rects(main)):
                plan.views[:] = main
                plan.dims[:] = [d for d in plan.dims if d.view != "iso"]
                return c
    plan.extra_findings.append({"rule": "D018", "severity": "warning",
                                "message": f"孔表（{len(plan.hole_table)} 行）放不下，与视图重叠",
                                "fix": "指定更大图幅（sheet），或减少视图"})
    return cands[0]


def _regular_row(h, ha: str, va: str):
    """一组孔是否为等距一排（≥3 个、同一行/列、节距相等）：返回 (排列方向轴, 个数, 节距, 首孔) 或 None。"""
    if len(h.instances) < 3 or h.slot:
        return None
    for along, across in ((ha, va), (va, ha)):
        if len({round(vc(i.xyz, across), 3) for i in h.instances}) != 1:
            continue
        ins = sorted(h.instances, key=lambda i: vc(i.xyz, along))
        gaps = [round(vc(b.xyz, along) - vc(a.xyz, along), 3) for a, b in zip(ins, ins[1:])]
        if gaps[0] > 0 and all(abs(g - gaps[0]) < 1e-3 for g in gaps):
            return along, len(ins), gaps[0], ins[0]
    return None


def pitch_dim_sheet(plan: "DrawingPlan", p: ResolvedPart) -> None:
    """等距孔节距尺寸的图纸坐标：尺寸界线引自首、末孔（孔圆周上远离尺寸线的点，CAD 选到圆按圆心标注），
    横排标在视图上方，竖排标在视图左侧总体尺寸之外。"""
    vmap = {v.type: v for v in plan.views}
    for d in plan.dims:
        if not (d.extra or {}).get("pitch") or d.view not in vmap:
            continue
        v = vmap[d.view]
        h = next(h for h in p.holes if h.name == d.ref[:-len(".pitch")])
        ha, va = VIEW_AXES[d.view]
        ins = sorted(h.instances, key=lambda i: vc(i.xyz, d.axis))
        a_, b_ = (_uv_sheet(v, p.bbox, (vc(i.xyz, ha), vc(i.xyz, va))) for i in (ins[0], ins[-1]))
        r = h.max_dia / 2 * v.scale
        cx, cy = v.origin_on_sheet
        w, hh = v.size_mm
        if d.axis == ha:
            pts = [(round(a_[0], 2), round(a_[1] + r, 2)), (round(b_[0], 2), round(b_[1] + r, 2))]
            pos, orient = (round((a_[0] + b_[0]) / 2, 2), round(cy + hh / 2 + 8, 2)), "h"
        else:
            pts = [(round(a_[0] - r, 2), round(a_[1], 2)), (round(b_[0] - r, 2), round(b_[1], 2))]
            pos, orient = (round(cx - w / 2 - 22, 2), round((a_[1] + b_[1]) / 2, 2)), "v"
        d.extra = {**d.extra, "sheet_pts": pts, "sheet_pos": pos, "orient": orient}


def _hole_table(dims: list[Dim], p: ResolvedPart, views: list[ViewPlan], spec: DrawingSpec):
    """孔表：每组孔一个字母、每孔一个编号（A1…），坐标相对视图左下角基准；该视图的坐标尺寸与孔标注由孔表代替。
    直接修改 dims；返回 (行, 视图)。"""
    if spec.hole_table is False:
        return [], ""
    vtypes = {v.type for v in views}
    by_view: dict[str, list] = {}
    for h in p.holes:
        v = hole_view(h.face, vtypes)
        if v in VIEW_AXES and not h.slot:
            by_view.setdefault(v, []).append(h)
    if not by_view:
        return [], ""
    view = max(by_view, key=lambda v: sum(len(h.instances) for h in by_view[v]))
    holes = by_view[view]
    ha0, va0 = VIEW_AXES[view]
    # 等距一排的孔组已用 n×p 节距尺寸表达，不计入孔表触发条件
    n = sum(len(h.instances) for h in holes if h.circular or _regular_row(h, ha0, va0) is None)
    ords = [d for d in dims if d.view == view and d.kind == "ordinate"]
    many_ord = max((sum(1 for d in ords if d.axis == ax) for ax in VIEW_AXES[view]), default=0) > HOLE_TABLE_ORD
    if spec.hole_table == "auto" and n < HOLE_TABLE_MIN and not many_ord:
        return [], ""
    ha, va = VIEW_AXES[view]
    b = p.bbox
    o = (vrange(b, ha)[0], vrange(b, va)[0])
    vp = next(v for v in views if v.type == view)
    rows: list[dict[str, Any]] = []
    tags: list[Dim] = []
    names = {h.name for h in holes}
    for gi, h in enumerate(holes):
        letter = "ABCDEFGHJKLMNPQRSTUVWXYZ"[gi % 24] + ("" if gi < 24 else str(gi // 24))
        insts = sorted(h.instances, key=lambda i: (round(vc(i.xyz, ha), 3), round(vc(i.xyz, va), 3)))
        size = re.sub(r"^\d+×", "", h.callout).replace(" 均布", "")
        for k, inst in enumerate(insts, 1):
            u, w = vc(inst.xyz, ha), vc(inst.xyz, va)
            tag = f"{letter}{k}"
            rows.append({"tag": tag, "x": round(u - o[0], 2), "y": round(w - o[1], 2), "size": size, "group": h.name})
            sx, sy = _uv_sheet(vp, b, (u, w))
            off = h.max_dia / 2 * vp.scale * 0.75
            tags.append(Dim(view, "tag", None, None, tag, f"tag:{tag}", (u, w),
                            {"sheet_at": (round(sx + off + 0.6, 2), round(sy + off + 0.4, 2))}))
    dims[:] = [d for d in dims if not (d.view == view and (d.kind == "ordinate" or d.ref in names
                                                        or d.ref.split(".")[0] in names and d.ref.endswith(".pcd")))]
    dims += tags
    dims.append(Dim(view, "origin", None, None, "X0 Y0", f"origin:{view}", o,
                    {"sheet_at": tuple(round(c, 2) for c in _uv_sheet(vp, b, o))}))
    return rows, view


def _hole_ra_to_section(plan: "DrawingPlan", p: ResolvedPart) -> None:
    """孔壁表面结构：孔被全剖/阶梯剖视图剖开且孔径够放符号时，改标在剖视图的孔壁上
    （符号在孔腔内、尖端触孔壁，GB/T 131 常规注法），不再挤在孔所在面视图的孔旁。"""
    from .section import AX, FRAME, hole_profile
    vscale = {v.type: v.scale for v in plan.views}
    for sec in plan.sections:
        if sec.kind not in ("full", "step") or sec.view not in VIEW_AXES:
            continue
        hi_, di_ = AX[FRAME[sec.view][0]], AX[FRAME[sec.view][1]]
        ha, va = VIEW_AXES[sec.view]
        for d in plan.dims:
            if d.kind != "roughness" or not d.ref.startswith("ra:hole:"):
                continue
            h = next((x for x in p.holes if x.name == d.ref[len("ra:hole:"):]), None)
            if h is None or h.slot or h.face not in ("top", "bottom"):
                continue
            r = h.drill / 2
            cut = [(abs(i.xyz[di_] - d_), i, d_) for i in h.instances for h0, h1, d_ in sec.path
                   if h0 - 1e-6 <= i.xyz[hi_] <= h1 + 1e-6 and abs(i.xyz[di_] - d_) < r * 0.5]
            if not cut:
                continue
            dist, inst, dcut = min(cut, key=lambda c: c[0])
            half = math.sqrt(r * r - dist * dist)
            need = 8.0 + 2 * 4.2 * 0.58 + len(d.text) * 2.0 + 3
            if 2 * half * vscale.get(sec.view, 1.0) < need:
                continue
            prof = hole_profile(h)
            depths = [dd for dd, rr in prof if abs(rr - r) < 1e-6]
            zmid_depth = (min(depths) + max(depths)) / 2 if depths else h.depth / 2
            walls = []
            for sgn in (-1, 1):
                q = list(inst.xyz)
                q[hi_] += sgn * half
                q[di_] = dcut
                q[2] += -inst.normal[2] * zmid_depth
                walls.append((vc(tuple(q), ha), vc(tuple(q), va)))
            u, w = min(walls)
            d.view, d.at = sec.view, (round(u, 6), round(w, 6))
            d.extra = {**(d.extra or {}), "normal": (1, 0), "in_section": sec.label}


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
            if sp.view not in vt or ("top" not in vt and sp.kind != "local"):
                raise CadError(SPEC_INVALID, f"section {i}: drawing needs views {sp.view!r} and 'top'",
                               hint="剖切线画在俯视图上，剖视图替代主视图/左视图；在 views 中加入它们")
            try:
                secs.append(sx.explicit_section(p, sp, sp.label or "ABCDEFGH"[i]))
            except ValueError as e:
                raise CadError(SPEC_INVALID, f"section {i}: {e}", hint='例：{"kind": "local", "through": ["M8"]}') from e
    n_local = 0
    for s in secs:
        if s.kind == "local":  # 局部剖不用字母
            n_local += 1
            s.label = f"局部{n_local}"
            s.wave_amp = 1.0 / next(v.scale for v in plan.views if v.type == s.view)
        s.geometry = sx.geometry(p, s)
        s.cut_points = sx.cut_line(p, s, next((v.scale for v in plan.views if v.type == s.parent), 1.0))
        s.bbox = p.bbox
        for v in plan.views:
            if v.type == s.view and s.kind != "local":
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
    # 孔标注合并、切割清单与焊缝符号加入后重新排布引出标注，避开序号和焊缝符号
    reserved = [(b_["pos"][0] - 6, b_["pos"][1] - 6, b_["pos"][0] + 6, b_["pos"][1] + 6) for b_ in plan.balloons if "pos" in b_]
    reserved += [(d.extra["sheet_pos"][0] - 2, d.extra["sheet_pos"][1] - 3, d.extra["sheet_pos"][0] + 32,
                  d.extra["sheet_pos"][1] + 8) for d in plan.dims if d.kind == "weld"]
    place_labels(plan, p.bbox, reserved, part=p)
    precision_sheet(plan, p)
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
    min_feat = min((f["width"] for f in p.shaft_features if "width" in f), default=0.0)
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
            th = next((f for f in p.shaft_features if f["kind"] == "thread" and f["step"] == i), None)
            # 螺纹段：螺纹代号注在大径上替代 Φ（GB/T 4459.1）
            dims.append(Dim("front", "diameter", None, d, th["callout"] if th else _dia_text(d, tol), f"step{i}_dia",
                            (z + L / 2, d)))
            if len(p.shaft_steps) > 1:
                dims.append(Dim("front", "linear", "z", L, f"{L:g}", f"step{i}_len", (z, z + L)))
            z += L
        dims.append(Dim("front", "linear", "z", Ltot, f"{Ltot:g}", "overall_z", (0.0, Ltot)))
        if p.shaft_chamfer:
            dims.append(Dim("front", "callout", None, p.shaft_chamfer, f"2×C{p.shaft_chamfer:g}", "chamfer",
                            (0.0, p.shaft_steps[0][1] / 2)))
        for f in p.shaft_features:
            sz0 = p.step_z(f["step"])[0]
            if f["kind"] == "thread":  # 未满整段：注螺纹长度（自轴段端面起）
                if not f["full"]:
                    dims.append(Dim("front", "linear", "z", f["length"], f"{f['length']:g}", f"{f['name']}_len",
                                    (f["z0"], f["z1"])))
                continue
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
    plan = DrawingPlan(p.name, "part", sheet, SHEETS[sheet], scale, spec.projection, views, dims, notes, tb,
                       layout="shaft", revisions=list(spec.revisions))
    precision_sheet(plan, p)
    if spec.dimensions != "none":
        shaft_dim_sheet(plan, p)
    return plan

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
    inst_dims, inst_note = install_dims(a, views) if spec.dimensions != "none" else ([], None)
    dims += inst_dims
    if inst_note:
        notes.append(f"3. {inst_note}")
    notes += [f"{i}. {t}" for i, t in enumerate(spec.notes, len(notes))]
    tb = {"name": a.title, "part_no": a.part_no, "material": "", "scale": scale_label(scale),
          "mass": f"{a.mass_kg:.2f} kg", "sheet": sheet,
          "projection": "第一角" if spec.projection == "first_angle" else "第三角",
          "designer": "", "checker": "", "approver": "", "date": (today or date.today()).isoformat()}
    tb.update(spec.title_block)
    plan = DrawingPlan(a.name, "assembly", sheet, SHEETS[sheet], scale, spec.projection, views, dims, notes, tb,
                       bom=bom, balloons=balloons, revisions=list(spec.revisions))
    overall_dim_sheet(plan, a)
    return plan


def install_dims(a: ResolvedAssembly, views: list[ViewPlan]) -> tuple[list[Dim], str | None]:
    """安装尺寸（GB/T 4458.1 装配图应注 规格/装配/安装/外形 尺寸）：最底层基础件上成组通孔的孔距，
    标在俯视图上方（横向）与右侧（纵向），尺寸界线引自孔心；孔规格写进技术要求。"""
    from .ir.assembly_resolve import apply
    v = next((v for v in views if v.type == "top"), None)
    if v is None or not a.instances:
        return [], None
    base = min((i for i in a.instances if i.source.install_holes), key=lambda i: i.world_bbox.zmin, default=None)
    if base is None or base.world_bbox.zmin > a.bbox.zmin + 1e-6:
        return [], None
    hg = max(base.source.install_holes, key=lambda h: len(h["pts"]))
    w = [apply(base.transform, q) for q in hg["pts"]]
    ha, va = VIEW_AXES["top"]
    (x0, y0), (x1, y1) = _uv_sheet(v, a.bbox, (vrange(a.bbox, ha)[0], vrange(a.bbox, va)[0])), \
        _uv_sheet(v, a.bbox, (vrange(a.bbox, ha)[1], vrange(a.bbox, va)[1]))
    r = hg["dia"] / 2 * v.scale
    holes = [[*(round(c, 2) for c in _uv_sheet(v, a.bbox, (vc(q, ha), vc(q, va)))), round(r, 2)] for q in w]
    out, sizes = [], []
    for axis, orient in ((ha, "h"), (va, "v")):
        vals = sorted({round(vc(q, axis), 3) for q in w})
        if len(vals) < 2:
            continue
        lo, hi = vals[0], vals[-1]
        # 两端各取最靠近尺寸线（上方/右侧）的孔，且同排/同列：CAD 生成水平/竖直尺寸而不是斜向对齐尺寸
        sh = [_uv_sheet(v, a.bbox, (vc(q, ha), vc(q, va))) for q in w]
        k = 1 if orient == "h" else 0

        def pick(val):
            return max((s_ for q, s_ in zip(w, sh) if abs(vc(q, axis) - val) < 1e-3), key=lambda s_: s_[k])
        sa, sb = pick(lo), pick(hi)
        if orient == "h":  # 孔圆周上远离尺寸线一侧的点：CAD 选到圆，按圆心标注
            pts = [(round(sa[0], 2), round(sa[1] + r, 2)), (round(sb[0], 2), round(sb[1] + r, 2))]
            pos = (round((sa[0] + sb[0]) / 2, 2), round(max(y0, y1) + 10, 2))
        else:
            pts = [(round(sa[0] + r, 2), round(sa[1], 2)), (round(sb[0] + r, 2), round(sb[1], 2))]
            pos = (round(max(x0, x1) + 10, 2), round((sa[1] + sb[1]) / 2, 2))
        out.append(Dim("top", "linear", axis, round(hi - lo, 2), f"{hi - lo:g}", f"install_{axis}",
                       extra={"install": True, "holes": holes, "sheet_pts": pts, "sheet_pos": pos, "orient": orient}))
        sizes.append(f"{hi - lo:g}")
    if not out:
        return [], None
    return out, f"安装孔 {hg['label']}（{base.source.part_no or base.id}），孔距 {'×'.join(sizes)}，见俯视图。"


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


LABEL_H = 3.0  # 引出标注字高
FACE_AXIS = {"top": "z", "bottom": "z", "front": "y", "back": "y", "left": "x", "right": "x"}


def _tw(s: str, size: float = LABEL_H) -> float:
    return sum(size if ord(ch) > 0x2E80 else 0.6 * size for ch in s)


def _uv_sheet(view: "ViewPlan", bbox, uv: tuple[float, float]) -> tuple[float, float]:
    ha, va = VIEW_AXES[view.type]
    (ulo, uhi), (wlo, whi) = vrange(bbox, ha), vrange(bbox, va)
    cx, cy = view.origin_on_sheet
    return cx + (uv[0] - (ulo + uhi) / 2) * view.scale, cy + (uv[1] - (wlo + whi) / 2) * view.scale


def _box_hit(a, b) -> float:
    ox, oy = min(a[2], b[2]) - max(a[0], b[0]), min(a[3], b[3]) - max(a[1], b[1])
    return ox * oy if ox > 0 and oy > 0 else 0.0


def _seg_cross(p1, p2, p3, p4) -> bool:
    def o(a, b, c):
        return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])
    return o(p1, p2, p3) * o(p1, p2, p4) < 0 and o(p3, p4, p1) * o(p3, p4, p2) < 0


def _seg_box(p1, p2, b) -> bool:
    corners = [(b[0], b[1]), (b[2], b[1]), (b[2], b[3]), (b[0], b[3])]
    return any(_seg_cross(p1, p2, corners[i], corners[(i + 1) % 4]) for i in range(4))


def ra_reach(nx: float, text: str, hole_r: float | None = None) -> float:
    """侧向表面结构符号的引出长度：向左引出时符号（√ + 向右的 Ra 横线）整体须落在轮廓外，引线加长；
    孔左侧（hole_r = 孔在图上的半径）小孔时 Ra 横线高出孔顶，不必加长。"""
    if nx >= 0 or hole_r is not None and hole_r < 7:
        return 8.0
    return 8.0 + 2 * 4.2 * 0.58 + len(text) * 2.0 + 3


def _ra_box(x, y, nx, ny, text, reach=None):
    """侧向表面结构符号（render_gdt.roughness）的占位框：引出 reach（缺省 ra_reach）后画 √ 与 Ra 值。"""
    r = ra_reach(nx, text) if reach is None else reach
    sx, sy = x + nx * r, y + ny * r + (0 if ny >= 0 else -9)
    return (min(x, sx - 3), min(y, sy - 1), max(x, sx + 7 + len(text) * 2.0), max(y, sy + 9))


def _flip_hole_roughness(plan: "DrawingPlan", vmap, bbox, part, hard=(), feat=()) -> None:
    """孔的表面结构默认在孔左侧；标注排完后选边：不被任何引线穿过、不压已放标注文字的一侧（左侧优先）。"""
    if part is None:
        return
    for d in plan.dims:
        if d.kind != "roughness" or not d.ref.startswith("ra:hole:") or d.at is None or d.view not in vmap:
            continue
        name = d.ref[len("ra:hole:"):]
        h = next((h for h in part.holes if h.name == name), None)
        if h is None or tuple(d.extra.get("normal", ())) != (-1, 0):
            continue
        lbs = [(d2.extra or {})["label"] for d2 in plan.dims if d2.view == d.view and (d2.extra or {}).get("label")]
        v = vmap[d.view]
        rs = h.max_dia / 2 * v.scale

        def bad(at_, nx):
            x, y = _uv_sheet(v, bbox, at_)
            rb = _ra_box(x, y, nx, 0, d.text, ra_reach(nx, d.text, rs))
            n = sum(1 for lb in lbs if _seg_box(lb["anchor"], lb["elbow"], rb))
            n += sum(1 for lb in lbs if _box_hit(rb, (min(lb["elbow"][0], lb["end"][0]), lb["elbow"][1] - 1,
                                                       max(lb["elbow"][0], lb["end"][0]), lb["elbow"][1] + LABEL_H)) > 0)
            n += sum(1 for hb in hard if _box_hit(rb, hb) > 0)  # 总体尺寸带、其他文字
            n += sum(1 for fb in feat if _box_hit(rb, fb) > 0)  # 别的孔、型腔（符号不压图形）
            return n
        at2 = (d.at[0] + h.max_dia, d.at[1])
        if bad(d.at, -1) and bad(at2, 1) < bad(d.at, -1):
            d.at, d.extra = at2, {**d.extra, "normal": (1, 0)}
        d.extra = {**d.extra, "reach": ra_reach(d.extra["normal"][0], d.text, rs)}


def place_labels(plan: "DrawingPlan", bbox, reserved: list[tuple[float, float, float, float]] | None = None,
                 part: ResolvedPart | None = None) -> None:
    """引出标注排布（孔标注/半径/型腔/倒角等）：对每个标注在 8 个方向 × 5 个距离中选位置，
    硬约束：不压其他文字、不出图框、不进标题栏/技术要求区、不压坐标尺寸带；软约束：少压视图、引线不交叉、引线短。
    结果写入 Dim.extra["label"] = {anchor, elbow, text_at, dir}（图纸坐标 mm），预览与 CAD 共用。"""
    W, H = plan.sheet_size
    vmap = {v.type: v for v in plan.views if v.type in VIEW_AXES}
    hard: list[tuple[float, float, float, float]] = list(reserved or [])
    hard.append((W - 5 - 180, 0, W, 5 + 56 + 14 + 5 * len(plan.notes) + 14
                 + (7 * (len(plan.cut_list) + 1) + 16 if plan.cut_list else 0)
                 + (7 * (len(plan.bom) + 1) if plan.bom else 0)))
    if plan.hole_table_box:
        b_ = plan.hole_table_box
        hard.append((b_[0] - 2, b_[1] - 2, b_[2] + 2, b_[3] + 14 if b_[0] > W / 2 else b_[3] + 2))
    soft = []
    for v in vmap.values():
        cx, cy = v.origin_on_sheet
        w, h = v.size_mm
        soft.append((cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2))
        if any(d.view == v.type and d.kind == "ordinate" for d in plan.dims):  # 坐标尺寸带（下方、右侧）
            hard.append((cx - w / 2 - 2, cy - h / 2 - 14, cx + w / 2 + 2, cy - h / 2))
            hard.append((cx + w / 2, cy - h / 2 - 2, cx + w / 2 + 12, cy + h / 2 + 2))
        hard.append((cx - w / 2 - 2, cy - h / 2 - 26, cx + w / 2 + 2, cy - h / 2 - 8))  # 总体尺寸（下）
        hard.append((cx - w / 2 - 26, cy - h / 2 - 2, cx - w / 2 - 6, cy + h / 2 + 2))  # 总体尺寸（左）
        if v.label:
            hard.append((cx - 10, cy + h / 2 + 2, cx + 10, cy + h / 2 + 9))
    for d in plan.dims:  # 规划层已定位的自由尺寸（等距节距、安装尺寸）：尺寸线与文字占位
        ex = d.extra or {}
        if ex.get("sheet_pos") and (ex.get("pitch") or ex.get("install")):
            (x1_, y1_), (x2_, y2_) = ex["sheet_pts"]
            px_, py_ = ex["sheet_pos"]
            hard.append((min(x1_, x2_, px_ - 4), min(y1_, y2_, py_) - 1, max(x1_, x2_, px_ + 4), max(y1_, y2_, py_) + 5)
                        if ex["orient"] == "h" else
                        (min(x1_, x2_, px_) - 5, min(y1_, y2_, py_ - 4), max(x1_, x2_, px_) + 1, max(y1_, y2_, py_ + 4)))
    for d in plan.dims:  # 精度符号大致占位
        if d.kind in ("datum", "gtol", "roughness") and d.at is not None and d.view in vmap \
                and (d.extra or {}).get("attach") != "callout" \
                and not (d.ref.startswith("ra:hole:") and not (d.extra or {}).get("in_section")):
            # 挂在孔标注下的框格随标注摆放（below 计入）；孔的 Ra 排完后选边（标在剖视图孔壁上的照常占位）
            x, y = _uv_sheet(vmap[d.view], bbox, d.at)
            nx, ny = (d.extra or {}).get("normal", (0, 1))
            if d.kind == "roughness" and (nx, ny) != (0, 1):  # 同 render_gdt.roughness：引出 8 mm 后画符号
                hard.append(_ra_box(x, y, nx, ny, d.text))
            else:
                hard.append((x - 4, y - 14, x + 26, y + 2) if d.kind != "datum" else (x - 5, y - 14, x + 5, y + 2))
    for s in plan.sections:  # 局部剖区域（剖面线 + 波浪线）：文字不得压在上面
        if s.kind == "local":
            loc = local_section_sheet(plan, s)
            if loc:
                xs, ys = [q[0] for q in loc["boundary"]], [q[1] for q in loc["boundary"]]
                hard.append((min(xs) - 1, min(ys), max(xs) + 1, max(ys)))
    feat: list[tuple[float, float, float, float]] = []  # 孔、型腔投影（文字不压图形）
    if part is not None:
        for h in part.holes:
            for v in vmap.values():
                if FACE_AXIS.get(h.face) != FACE_AXIS.get(v.type):
                    continue
                ha_, va_ = VIEW_AXES[v.type]
                for inst in h.instances:
                    x, y = _uv_sheet(v, bbox, (vc(inst.xyz, ha_), vc(inst.xyz, va_)))
                    r = h.max_dia / 2 * v.scale + 1
                    feat.append((x - r, y - r, x + r, y + r))
                if h.circular:
                    from .ir.resolve import face_point
                    c = h.circular
                    cxyz = face_point(h.face, (c["center_u"], c["center_v"]), bbox, h.at)
                    x, y = _uv_sheet(v, bbox, (vc(cxyz, ha_), vc(cxyz, va_)))
                    r = c["pcd"] / 2 * v.scale
                    feat.append((x - r, y - r, x + r, y + r))
        for pk in part.pockets:
            for v in vmap.values():
                if pk["face"] in ("top", "bottom") and v.type in ("top", "bottom"):
                    ha_, va_ = VIEW_AXES[v.type]
                    pts = [_uv_sheet(v, bbox, (vc((x, y, 0), ha_), vc((x, y, 0), va_))) for x, y in pk["profile"].outline]
                    feat.append((min(q[0] for q in pts), min(q[1] for q in pts), max(q[0] for q in pts), max(q[1] for q in pts)))
    for s in plan.sections:  # 全剖/阶梯剖视图满布剖面线：文字压在上面须断开剖面线，尽量放到视图外
        if s.kind != "local" and s.view in vmap:
            v = vmap[s.view]
            cx, cy = v.origin_on_sheet
            w, h = v.size_mm
            feat.append((cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2))
    if part is not None and getattr(part, "weld_bodies", None):  # 焊接件：杆件/板件投影（文字不压型材）
        for body in part.weld_bodies:
            bb = Box(*body["box"])
            for v in vmap.values():
                if v.type not in VIEW_AXES:
                    continue
                ha_, va_ = VIEW_AXES[v.type]
                (u0, u1), (w0, w1) = vrange(bb, ha_), vrange(bb, va_)
                x0, y0 = _uv_sheet(v, bbox, (u0, w0))
                x1, y1 = _uv_sheet(v, bbox, (u1, w1))
                feat.append((min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1)))
    for sec in plan.sections:  # 剖切符号与字母
        pv = vmap.get(sec.parent)
        if pv is None:
            continue
        ha_, va_ = VIEW_AXES[pv.type]
        for q in sec.cut_points:
            x, y = _uv_sheet(pv, bbox, (vc(q, ha_), vc(q, va_)))
            hard.append((x - 6, y - 6, x + 6, y + 6))
    leaders: list[tuple[tuple[float, float], tuple[float, float]]] = []
    labels = [d for d in plan.dims if d.kind in ("callout", "radius", "diameter") and d.view in vmap
              and not (plan.layout == "shaft" and d.kind == "diameter")]
    for d in labels:  # 各标注的引出点：别的标注文字不得压上（否则其引线无处可走）；自身引线从 1.5 mm 外起算，不受影响
        if d.at is not None:
            x, y = _uv_sheet(vmap[d.view], bbox, d.at)
            hard.append((x - 1.2, y - 1.2, x + 1.2, y + 1.2))
    for d in sorted(labels, key=lambda d: -_tw(d.text)):  # 长的先放
        v = vmap[d.view]
        cx, cy = v.origin_on_sheet
        anchors = [d.at] if d.at is not None else [None]
        h_ = next((h for h in (part.holes if part is not None else []) if h.name == d.ref), None)
        tied = any(d2.kind in ("gtol", "roughness", "datum") and d2.ref.endswith(f"hole:{d.ref}") for d2 in plan.dims)
        if d.kind == "callout" and h_ is not None and not h_.circular and not tied and v.type in VIEW_AXES \
                and FACE_AXIS.get(h_.face) == FACE_AXIS.get(v.type):  # 孔上挂了公差框格/粗糙度时不换实例
            ha_, va_ = VIEW_AXES[v.type]  # 多个同规格孔：引线可指向任一个，取最好放的
            anchors += [(vc(i.xyz, ha_), vc(i.xyz, va_)) for i in h_.instances[1:]]
        tw = _tw(d.text) + 1
        frame = next((d2 for d2 in plan.dims if d2.kind == "gtol" and (d2.extra or {}).get("attach") == "callout"
                      and d2.ref.endswith(f"hole:{d.ref}")), None)
        below = 0.0
        if frame is not None:  # 孔标注下方挂位置度框格：占位一并计入
            cells = frame.extra.get("cells", ["", "", ""])
            fw = 7.0 + max(7.0, len(cells[1]) * 2.2 + 3) + 6.0 * len(cells[2:]) if len(cells) > 1 else 14.0
            tw, below = max(tw, fw + 1), 6.5
        # 孔的引线从孔轮廓沿径向引出（GB/T 4458.4：指向圆心、箭头止于轮廓），大孔才能把标注引到视图外
        rr = h_.max_dia / 2 * v.scale if d.kind == "callout" and h_ is not None and not h_.circular \
            and v.type in VIEW_AXES and FACE_AXIS.get(h_.face) == FACE_AXIS.get(v.type) else 0.0
        best = None
        for at_ in anchors:
            if at_ is not None:
                ax, ay = _uv_sheet(v, bbox, at_)
            else:
                ax, ay = cx + v.size_mm[0] / 2, cy + v.size_mm[1] / 2
            best = _best_label(best, at_, ax, ay, tw, W, H, hard, soft, feat, leaders, below, rr)
        _, ex, ey, dirn, box, at_, ax, ay = best
        if at_ is not None and at_ != d.at:
            d.at = (round(at_[0], 6), round(at_[1], 6))
        hard.append(box)
        leaders.append(((ax, ay), (ex, ey)))
        d.extra = {**(d.extra or {}), "label": {"anchor": (round(ax, 2), round(ay, 2)), "elbow": (round(ex, 2), round(ey, 2)),
                                                 "text_at": (round(box[0] + (0.5 if dirn > 0 else 0.5), 2), round(ey + 0.8, 2)),
                                                 "end": (round(ex + dirn * tw, 2), round(ey, 2)), "dir": dirn,
                                                 **({"on_edge": True} if rr >= 5 else {})}}
    _flip_hole_roughness(plan, vmap, bbox, part, hard, feat)


def _best_label(best, at_, ax0, ay0, tw, W, H, hard, soft, feat, leaders, below: float = 0.0, rr: float = 0.0):
    """引出标注一个锚点的 8 方向 × 7 档长度候选，与当前最优比较（代价：长度 + 压线/压图/交叉/出图框罚分）。
    rr ≥ 5：锚点为孔心，引线起点取在孔轮廓上（沿引线方向偏 rr）；返回的 ax, ay 为该轮廓点。"""
    for L in (10, 16, 24, 34, 46, 60, 80):
        for ang in (45, 135, -45, -135, 25, 155, -25, -155):
            c_, s_ = math.cos(math.radians(ang)), math.sin(math.radians(ang))
            ax, ay = (ax0 + rr * c_, ay0 + rr * s_) if rr >= 5 else (ax0, ay0)
            ex, ey = ax + L * c_, ay + L * s_
            dirn = 1 if math.cos(math.radians(ang)) >= 0 else -1
            box = (ex, ey - 0.5 - below, ex + tw, ey + LABEL_H + 0.5) if dirn > 0 else \
                (ex - tw, ey - 0.5 - below, ex, ey + LABEL_H + 0.5)
            cost = L * 1.5
            if box[0] < 8 or box[2] > W - 8 or box[1] < 8 or box[3] > H - 8:
                cost += 5000
            cost += sum(1000 for hb in hard if _box_hit(box, hb) > 0)
            for sb in soft:  # 压视图：120；骑在视图轮廓线上（部分在内）更差：+250
                a_ = _box_hit(box, sb)
                if a_ > 0:
                    cost += 120 + (250 if a_ < (box[2] - box[0]) * (box[3] - box[1]) - 1e-6 else 0)
            cost += sum(400 for fb in feat if _box_hit(box, fb) > 0)
            cost += sum(300 for l1, l2 in leaders if _seg_cross((ax, ay), (ex, ey), l1, l2))
            cost += sum(300 for l1, l2 in leaders if _seg_box(l1, l2, box))  # 文字不压先放的引线
            sx_, sy_ = ax + 1.5 * math.cos(math.radians(ang)), ay + 1.5 * math.sin(math.radians(ang))  # 锚点本身可能落在别的框内
            cost += sum(300 for hb in hard if _seg_box((sx_, sy_), (ex, ey), hb))
            if best is None or cost < best[0]:
                best = (cost, ex, ey, dirn, box, at_, ax, ay)
    return best


def precision_sheet(plan: "DrawingPlan", p: ResolvedPart) -> None:
    """精度符号（基准、几何公差框格、表面结构）的图纸坐标（mm），供 CAD 原生符号定位：
    extra["sheet_at"] = 贴在被测/基准要素上的点（用于选中边线作引线附着），
    extra["sheet_pos"] = 符号/框格放置点（与 SVG 预览一致的外法向偏移）。"""
    vmap = {v.type: v for v in plan.views}
    labels = {d.ref: d.extra["label"] for d in plan.dims if d.extra and "label" in d.extra}
    for d in plan.dims:
        if d.kind not in ("datum", "gtol", "roughness") or d.at is None or d.view not in vmap:
            continue
        v = vmap[d.view]
        if plan.layout == "shaft":
            cx, cy = v.origin_on_sheet
            L = p.bbox.zmax - p.bbox.zmin
            ax, ay = cx - L * v.scale / 2 + d.at[0] * v.scale, cy + d.at[1] * v.scale
        elif d.view in VIEW_AXES:
            ax, ay = _uv_sheet(v, p.bbox, d.at)
        else:
            continue
        nx, ny = (d.extra or {}).get("normal", (0, 1))
        reach = {"datum": 10.0, "gtol": 12.0 if plan.layout != "shaft" else 18.0, "roughness": 0.0 if (nx, ny) == (0, 1) else (d.extra or {}).get("reach") or ra_reach(nx, d.text)}[d.kind]
        pos = (ax + nx * reach, ay + ny * reach)
        if d.kind == "gtol" and (d.extra or {}).get("attach") == "callout":
            hole = d.ref.split("@hole:")[-1]
            lb = labels.get(hole)
            if lb:  # 框格放在孔标注文字下方（顶边贴标注横线，左端对齐文字）
                pos = (min(lb["elbow"][0], lb["end"][0]), lb["elbow"][1] - 6.0)
                d.extra = {**(d.extra or {}), "callout_label": lb}
        d.extra = {**(d.extra or {}), "sheet_at": (round(ax, 2), round(ay, 2)), "sheet_pos": (round(pos[0], 2), round(pos[1], 2))}


REV_COLUMNS = (("版本", 14), ("更改内容", 78), ("日期", 24), ("签名", 18))  # (表头, 列宽 mm)，与 render_svg 一致
REV_MAX_ROWS = 6


def revision_rows(plan: "DrawingPlan") -> list[dict[str, str]]:
    """图上显示的修订行：最新在上，最多 REV_MAX_ROWS 行（render_svg 与 SW 修订表共用）。"""
    return list(plan.revisions[::-1][:REV_MAX_ROWS])


def revision_table_origin(plan: "DrawingPlan") -> tuple[float, float]:
    """修订表左下角（图纸 mm）：标题栏左侧，底边与标题栏对齐（同 render_svg）。"""
    W, _ = plan.sheet_size
    return (W - 5 - 180 - sum(w for _, w in REV_COLUMNS), 5.0)


def check_revision_table(plan: "DrawingPlan", rendered: dict[str, Any]) -> list[dict[str, Any]]:
    """D020 图上修订表行数与方案不一致。"""
    if "revision_rows" not in rendered or not plan.revisions:
        return []
    want = len(revision_rows(plan))
    if rendered["revision_rows"] != want:
        return [{"rule": "D020", "severity": "error",
                 "message": f"修订表 {rendered['revision_rows']} 行，方案要求 {want} 行",
                 "fix": "核对 InsertRevisionTable2 / AddRevision 回读"}]
    return []

def shaft_dia_z(p: ResolvedPart, i: int) -> float:
    """第 i 段直径尺寸线的轴向位置：该轴段内离键槽/环槽最远、避开端部倒角的位置（SVG 与原生标注共用）。"""
    Ltot = p.bbox.zmax - p.bbox.zmin
    c = p.shaft_chamfer
    z0, z1 = p.step_z(i)
    busy = [(f["z0"], f["z1"]) for f in p.shaft_features if f["step"] == i]
    cands = [z0 + (z1 - z0) * k / 20 for k in range(2, 19)]
    return max(cands, key=lambda z: (min(min([abs(z - (a + b) / 2) - (b - a) / 2 for a, b in busy] or [1e9]), 4.0),
                                      min(z, Ltot - z) > c + 2,  # 避开端部倒角
                                      -abs(z - (z0 + z1) / 2)))


def shaft_dim_sheet(plan: "DrawingPlan", p: ResolvedPart) -> None:
    """轴类主视图尺寸的图纸坐标（mm，与 render_shaft 的 SVG 一致：P(z, r) = (cx - Ltot*sc/2 + z*sc, cy + r*sc)），
    供 CAD 原生标注选边：extra["sheet_pts"] = 两个被标注点（线性尺寸 = 两条垂直边线上的点；
    直径 = 上下两条轮廓线上的点），extra["sheet_pos"] = 尺寸线/文字放置点，extra["orient"] = h | v。"""
    from .render_shaft import _profile
    main = next((v for v in plan.views if v.type == "front"), None)
    if main is None:
        return
    sc = main.scale
    Ltot = p.bbox.zmax - p.bbox.zmin
    cx, cy = main.origin_on_sheet
    xl = cx - Ltot * sc / 2
    c = p.shaft_chamfer
    segs = _profile(p)
    R = max(d for _, d in p.shaft_steps) / 2
    y_top, y_bot = cy + R * sc, cy - R * sc

    def radius_at(z):
        for a, b, r in segs:
            if a - 1e-9 <= z <= b + 1e-9:
                return r
        return 0.0

    def edge_pt(z, keyway_end):
        """z 处垂直边线上的一点：键槽端部取圆弧最远点（轴线高度），轴肩/端面取偏离轴线的点避开键槽。"""
        if keyway_end:
            return (round(xl + z * sc, 2), round(cy, 2))
        rs = [r for r in (radius_at(z - 1e-6), radius_at(z + 1e-6)) if r > 0]
        r = min(rs)
        off = min(0.85 * r, max(r - c - 0.5, 0.3 * r))
        return (round(xl + z * sc, 2), round(cy - off * sc, 2))

    kw_ends = {round(f[k], 6) for f in p.shaft_features if f["kind"] == "keyway" for k in ("z0", "z1")}
    for d in plan.dims:
        if d.view != "front" or d.at is None:
            continue
        if d.kind == "linear" and d.axis == "z":
            za, zb = d.at
            if d.ref == "overall_z":
                y = y_bot - 26
            elif d.ref.startswith("step"):
                y = y_bot - 15
            else:
                y = y_top + 14
            kw = not d.ref.startswith(("step", "overall"))
            pts = [edge_pt(z, kw and round(z, 6) in kw_ends) for z in (za, zb)]
            d.extra = {**(d.extra or {}), "sheet_pts": pts, "sheet_pos": (round((pts[0][0] + pts[1][0]) / 2, 2), round(y, 2)),
                       "orient": "h"}
        elif d.kind == "diameter" and d.ref.startswith("step"):
            i = int(d.ref[4:].split("_")[0])
            x = xl + shaft_dia_z(p, i) * sc
            r = d.at[1] / 2
            d.extra = {**(d.extra or {}), "sheet_pts": [(round(x, 2), round(cy - r * sc, 2)), (round(x, 2), round(cy + r * sc, 2))],
                       "sheet_pos": (round(x + 6, 2), round(cy, 2)), "orient": "v"}
        elif d.kind == "callout" and d.ref in ({"chamfer"} | {f["name"] for f in p.shaft_features}):
            # 位置同 render_shaft：引线从轮廓点斜向上到主视图上方的水平线
            z, r = d.at
            ax, ay = xl + z * sc, cy + r * sc
            ty = y_top + 4 + (4 if d.ref == "chamfer" else 14)
            tx = ax - 4 - len(d.text) * 2 if d.ref == "chamfer" else ax + 6
            w = len(d.text) * 2.4
            d.extra = {**(d.extra or {}), "label": {"anchor": (round(ax, 2), round(ay, 2)), "elbow": (round(tx, 2), round(ty, 2)),
                                                     "text_at": (round(tx + 0.5, 2), round(ty + 0.8, 2)),
                                                     "end": (round(tx + w, 2), round(ty, 2)), "dir": 1}}
    _shaft_section_dim_sheet(plan, p)


def _shaft_section_dim_sheet(plan: "DrawingPlan", p: ResolvedPart) -> None:
    """移出断面图（键槽）上的尺寸：图纸坐标与 render_shaft 一致（键槽朝上，断面中心 = 视图 origin_on_sheet）。
    槽宽 = 两侧壁上的点（水平）；槽底到对侧轮廓 d−t = 圆周最低点 + 槽底线上的点（竖直），
    extra["arc_end"] = ("max", None) 表示第一个点在圆上、应量到圆的远端（CAD 端设圆弧条件“最大”）。
    extra["section"] = 断面标签（A-A），供 CAD 端在多张同类断面图中找到对应视图。"""
    for v in plan.views:
        if v.type != "section" or not v.target:
            continue
        f = next((k for k in p.shaft_features if k["name"] == v.target and k["kind"] == "keyway"), None)
        if f is None:
            continue
        sc = v.scale
        vx, vy = v.origin_on_sheet
        r, hb, t1 = f["d"] / 2 * sc, f["width"] / 2 * sc, f["depth"] * sc
        y_floor = vy + r - t1
        y_rim = vy + math.sqrt(max(r * r - hb * hb, 0.0))
        for d in plan.dims:
            if d.view != "section" or not d.ref.startswith(f["name"] + "_"):
                continue
            if d.ref.endswith("_w"):
                ym = (y_rim + y_floor) / 2
                ex = {"sheet_pts": [(round(vx - hb, 2), round(ym, 2)), (round(vx + hb, 2), round(ym, 2))],
                      "sheet_pos": (round(vx, 2), round(vy + r + 5, 2)), "orient": "h"}
            elif d.ref.endswith("_t"):
                ex = {"sheet_pts": [(round(vx, 2), round(vy - r, 2)), (round(vx, 2), round(y_floor, 2))],
                      "sheet_pos": (round(vx + r + 6, 2), round((vy - r + y_floor) / 2, 2)), "orient": "v",
                      "arc_end": ("max", None)}
            else:
                continue
            d.extra = {**(d.extra or {}), **ex, "section": v.label}


def flat_dim_sheet(plan: "DrawingPlan", p: ResolvedPart) -> None:
    """钣金展开图尺寸的图纸坐标，与 render_svg._draw_flat 一致（F(u, v) = 视图中心 + (u−中点)·比例）：
    展开总宽/总高取外轮廓两条对边上的点（避开折弯线位置），尺寸线在下方 10 mm / 右侧 10 mm；
    折弯说明 extra["sheet_pos"] = 文字左下角（无引线）。"""
    fv = next((v for v in plan.views if v.type == "flat_pattern"), None)
    if fv is None or not p.flat_box:
        return
    fu0, fu1, fv0, fv1 = p.flat_box
    cx, cy = fv.origin_on_sheet
    sc = fv.scale

    def F(u, v):
        return cx + (u - (fu0 + fu1) / 2) * sc, cy + (v - (fv0 + fv1) / 2) * sc

    x0, y0 = F(fu0, fv0)
    x1, y1 = F(fu1, fv1)
    bends_u = [F(b["flat_pos"], 0)[0] for b in p.bends if b["flat_axis"] != "v"]  # 竖直折弯线的 x
    bends_v = [F(0, b["flat_pos"])[1] for b in p.bends if b["flat_axis"] == "v"]  # 水平折弯线的 y

    def clear(lo, hi, busy):
        """轮廓边上离折弯线最远的点（折弯线与外轮廓相交处选边不唯一）。"""
        cands = [lo + (hi - lo) * k / 20 for k in range(2, 19)]
        return max(cands, key=lambda c: (min([abs(c - b) for b in busy] or [1e9]), -abs(c - (lo + hi) / 2)))

    for d in plan.dims:
        if d.view != "flat_pattern":
            continue
        if d.ref == "flat_w":
            y = clear(y0, y1, bends_v)
            d.extra = {**(d.extra or {}), "sheet_pts": [(round(x0, 2), round(y, 2)), (round(x1, 2), round(y, 2))],
                       "sheet_pos": (round((x0 + x1) / 2, 2), round(y0 - 10, 2)), "orient": "h"}
        elif d.ref == "flat_h":
            x = clear(x0, x1, bends_u)
            d.extra = {**(d.extra or {}), "sheet_pts": [(round(x, 2), round(y0, 2)), (round(x, 2), round(y1, 2))],
                       "sheet_pos": (round(x1 + 10, 2), round((y0 + y1) / 2, 2)), "orient": "v"}
        elif d.ref.startswith("flat_bend"):
            bd = p.bends[d.extra["bend"]]
            same = sorted((b_["flat_pos"], j) for j, b_ in enumerate(p.bends) if b_["flat_axis"] == bd["flat_axis"])
            k = [j for _, j in same].index(d.extra["bend"])  # 基线尺寸由近到远逐层外移
            if bd["flat_axis"] == "v":  # 水平折弯线：竖直尺寸，放展开图左侧
                yb = F(0, bd["flat_pos"])[1]
                x = clear(x0, x1, bends_u)
                xx = x0 - 10 - 8 * k
                d.extra = {**d.extra, "sheet_pts": [(round(x0, 2), round(y0, 2)), (round(x, 2), round(yb, 2))],
                           "sheet_pos": (round(xx, 2), round((y0 + yb) / 2, 2)), "orient": "v"}
            else:  # 竖直折弯线：水平尺寸，放展开图上方
                xb = F(bd["flat_pos"], 0)[0]
                y = clear(y0, y1, bends_v)
                yy = y1 + 10 + 8 * k
                d.extra = {**d.extra, "sheet_pts": [(round(x0, 2), round(y1, 2)), (round(xb, 2), round(y, 2))],
                           "sheet_pos": (round((x0 + xb) / 2, 2), round(yy, 2)), "orient": "h"}
        elif d.kind == "callout" and d.at is not None:
            px, py = F(*d.at)
            d.extra = {**(d.extra or {}), "sheet_pos": (round(px - _tw(d.text) / 2, 2), round(py + 1, 2))}


def overall_dim_sheet(plan: "DrawingPlan", p) -> None:
    """零件各视图总体尺寸的图纸坐标（mm），位置规则与 render_svg 一致：
    p 为 ResolvedPart / ResolvedAssembly（只用 bbox）。水平尺寸取视图左右两条竖直轮廓线（高度中点），尺寸线在视图下方；竖直尺寸取上下两条水平轮廓线，尺寸线在视图左侧。"""
    for v in plan.views:
        if v.type not in VIEW_AXES:
            continue
        ha, va = VIEW_AXES[v.type]
        (ulo, uhi), (wlo, whi) = vrange(p.bbox, ha), vrange(p.bbox, va)
        (x0, y0), (x1, y1) = _uv_sheet(v, p.bbox, (ulo, wlo)), _uv_sheet(v, p.bbox, (uhi, whi))
        mid = _uv_sheet(v, p.bbox, ((ulo + uhi) / 2, (wlo + whi) / 2))
        ords = [d for d in plan.dims if d.view == v.type]
        has_ord_h = any(d.kind == "ordinate" and d.axis == ha for d in ords)
        norms = [tuple(d.extra["normal"]) for d in ords if d.kind in ("datum", "gtol", "roughness")
                 and d.extra and "normal" in d.extra and not d.extra.get("attach")]
        for d in ords:
            if d.kind != "linear" or d.at is not None or (d.extra or {}).get("install") or (d.extra or {}).get("pitch"):
                continue
            if d.axis == ha:
                y = y0 - (22 if has_ord_h or (0, -1) in norms else 10)
                pts = [(round(x0, 2), round(mid[1], 2)), (round(x1, 2), round(mid[1], 2))]
                pos, orient = (round((x0 + x1) / 2, 2), round(y, 2)), "h"
            elif d.axis == va:
                x = x0 - (22 if (-1, 0) in norms else 10)
                pts = [(round(mid[0], 2), round(y0, 2)), (round(mid[0], 2), round(y1, 2))]
                pos, orient = (round(x, 2), round((y0 + y1) / 2, 2)), "v"
            else:
                continue
            d.extra = {**(d.extra or {}), "sheet_pts": pts, "sheet_pos": pos, "orient": orient}


def local_section_sheet(plan: "DrawingPlan", s) -> dict[str, Any] | None:
    """局部剖的 CAD 原生参数（与 render_svg 的 _draw_local 同一几何）：
    boundary = 局部区域闭合折线（图纸 mm，两条波浪线围成，上下各外扩 1 mm 保证包住轮廓），
    cut_points = 剖切面在零件顶面上的两点（模型坐标）：H 区间 [h0, h1]、深度 D = d。"""
    from .section import _p3
    v = next((v for v in plan.views if v.type == s.view), None)
    region = (s.geometry or {}).get("region")
    if v is None or v.type not in VIEW_AXES or not region or s.bbox is None or not s.path:
        return None
    ha, va = VIEW_AXES[v.type]
    pts = [_uv_sheet(v, s.bbox, (vc(q, ha), vc(q, va))) for q in region[0]]
    ys = [y for _, y in pts]
    lo, hi = min(ys), max(ys)
    pts = [(x, y - 1 if abs(y - lo) < 1e-6 else y + 1 if abs(y - hi) < 1e-6 else y) for x, y in pts]
    out: list[tuple[float, float]] = []
    for x, y in pts:  # 去掉重复点
        q = (round(x, 2), round(y, 2))
        if not out or out[-1] != q:
            out.append(q)
    if len(out) > 1 and out[0] == out[-1]:
        out.pop()
    h0, h1, d = s.path[0]
    z = s.bbox.zmax
    return {"boundary": out, "cut_points": [_p3(s.view, h0, d, z), _p3(s.view, h1, d, z)]}


def ordinate_dim_sheet(plan: "DrawingPlan", p: ResolvedPart) -> None:
    """坐标尺寸的图纸坐标（mm），与 render_svg 的坐标尺寸带一致，供 CAD 原生坐标尺寸选边：
    extra["sheet_pts"] = [基准轮廓边上一点（横向取左边线、纵向取下边线），被标注要素边上一点（孔取圆周最近点）]，
    extra["sheet_pos"] = 数字位置（横向在视图下方、纵向在视图右侧，奇偶错开），extra["orient"] = h | v。"""
    for v in plan.views:
        if v.type not in VIEW_AXES:
            continue
        ha, va = VIEW_AXES[v.type]
        (ulo, uhi), (wlo, whi) = vrange(p.bbox, ha), vrange(p.bbox, va)
        (x0, y0), (x1, y1) = _uv_sheet(v, p.bbox, (ulo, wlo)), _uv_sheet(v, p.bbox, (uhi, whi))
        mid = _uv_sheet(v, p.bbox, ((ulo + uhi) / 2, (wlo + whi) / 2))
        n_h = n_v = 0
        for d in plan.dims:
            if d.view != v.type or d.kind != "ordinate" or d.at is None:
                continue
            px, py = _uv_sheet(v, p.bbox, d.at)
            r = next((h.max_dia / 2 * v.scale for h in getattr(p, "holes", []) for i in h.instances
                      if abs(vc(i.xyz, ha) - d.at[0]) < 1e-6 and abs(vc(i.xyz, va) - d.at[1]) < 1e-6), 0.0)
            if d.axis == ha:
                pts = [(x0, mid[1]), (px, py + r)]  # 圆周最上点；r=0 时要素点本身就在边上
                pos, orient = (px, y0 - 6.5 - (n_h % 2) * 3), "h"
                n_h += 1
            elif d.axis == va:
                pts = [(mid[0], y0), (px + r, py)]
                pos, orient = (x1 + 5 + (n_v % 2) * 3, py), "v"
                n_v += 1
            else:
                continue
            d.extra = {**(d.extra or {}), "sheet_pts": [(round(a, 2), round(b, 2)) for a, b in pts],
                       "sheet_pos": (round(pos[0], 2), round(pos[1], 2)), "orient": orient}


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
    if rendered is not None and rendered.get("crossings"):
        c = rendered["crossings"]
        f("D022", "warning", f"{len(c)} 处引线/尺寸线穿过标注文字，如 {c[0]!r}",
          "调整孔位标注或指定更大图幅（sheet）；人工核对")
    if plan.model_kind == "part" and not any(d.kind == "linear" for d in plan.dims):
        f("D006", "error", "缺少总体尺寸")
    if plan.model_kind == "assembly" and not plan.bom:
        f("D007", "warning", "装配图没有明细表")
    if rendered is not None and plan.model_kind == "assembly":
        out += check_bom_association(plan, rendered)
    if rendered is not None and plan.layout == "weldment":
        out += check_weldment_drawing(plan, rendered)
    if rendered is not None:
        out += check_revision_table(plan, rendered)
    out += [{k: v for k, v in x.items()} for x in plan.extra_findings]
    if plan.section_skipped:
        f("D015", "warning", "零件有沉孔/螺纹孔/盲孔/型腔等内部结构，主视图未剖开，只能靠虚线和标注表达",
          "sections 用 auto 或指定剖切位置")
    if rendered is not None and ("dim_refs" in rendered or "sections" in rendered):
        done = set(rendered.get("sections", [])) | {r[8:] for r in rendered.get("dim_refs", []) if r.startswith("section:")}
        for sec in plan.sections:
            if sec.label not in done:
                f("D016", "error", f"剖视图 {sec.label if sec.kind == 'local' else sec.label + '-' + sec.label} 未生成",
                  "查看 oplog 中 section_cut / broken_out_section 的失败原因")
    if rendered is not None and rendered.get("gdt_fallback"):
        fb = rendered["gdt_fallback"]
        f("D019", "warning", f"{len(fb)} 处精度符号未能生成原生符号，已用带引线注释代替：{', '.join(fb[:4])}",
          "查看 oplog 中 probe_gdt_constants 列出的常量名与 datum_tag/gtol/surface_finish 的失败原因，发回开发组")
    if rendered is not None and plan.hole_table and "hole_table_rows" in rendered:
        want = {(r["tag"], r["x"], r["y"]) for r in plan.hole_table}
        got = set()
        for r in rendered["hole_table_rows"]:
            try:
                got.add((str(r[0]), round(float(r[1]), 2), round(float(r[2]), 2)))
            except (TypeError, ValueError, IndexError):
                pass
        if want != got:
            f("D017", "error", f"孔表与孔位不一致：缺 {sorted(want - got)[:4]}，多/错 {sorted(got - want)[:4]}",
              "重新出图；检查表格写入（oplog 中 hole_table）")
    if plan.model_kind == "part" and not any("公差" in n for n in plan.notes):
        f("D008", "warning", "缺少未注公差说明")
    return out
