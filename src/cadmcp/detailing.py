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
    # GB/T 14690：1:2.5、2.5:1 等带小数的比例照写（不能取整）
    return f"{round(s, 2):g}:1" if s >= 1 else f"1:{round(1 / s, 2):g}"


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
    full_section: dict | None = None  # 原位全剖（空心轴/套类主视图）：{boundary 图纸 mm 矩形, depth 模型 mm, cut_points 模型}
    local_sections: list = field(default_factory=list)  # 局部剖（轴类主视图竖直横孔）：[{boundary 图纸 mm, depth 模型 mm, zone 模型 (za, zb, y0, y1)}]
    breaks: list = field(default_factory=list)  # 断开画法：去掉的模型 z 区间 [(a, b)]（轴类主视图），每处在图上留 BREAK_GAP
    detail: dict | None = None  # 局部放大图：{zc, yc 模型 mm（放大区域中心）, rm 模型半径, circle (cx, cy, r) 主视图图纸 mm,
    #                             label_at 主视图罗马数字位置, leader [(x, y), (x, y)]}


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
    asm_section: dict[str, Any] = field(default_factory=dict)  # 装配图主视图全剖（见 plan_asm_section）
    axes: list[dict[str, Any]] = field(default_factory=list)  # 装配图回转件轴线/中心线：{view, pts:[(x,y),(x,y)]}（图纸 mm）
    gear_table: dict[str, Any] = field(default_factory=dict)  # 齿轮参数表：origin/columns/rows/row_height/size

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


RIGHT_COL_W = 185.0
NOTES_H = 30.0  # 带展开图时：视图伸进右侧栏时，标题栏之上还要让出技术要求（约 4 条 + "其余"粗糙度）  # 右侧栏（标题栏 180 + 间隙）：技术要求、明细表、孔表都在这一栏


def _fits(sh: str, s: float, col_w, row_h, notes_h: float = 0.0) -> bool:
    """视图区放得下：① 全宽、标题栏以上；或 ② 右侧栏以左、全高（标题栏/明细表只占右侧栏）。"""
    W, H = SHEETS[sh]
    vw, vh = sum(col_w) * s + GAP * len(col_w), sum(row_h) * s + GAP * len(row_h)
    return (vw <= W - 2 * MARGIN and vh <= H - 2 * MARGIN - TITLE_BLOCK_H - notes_h) or \
        (vw <= W - 2 * MARGIN - RIGHT_COL_W and vh <= H - 2 * MARGIN)


def _fill(sh: str, s: float, col_w, row_h) -> float:
    W, H = SHEETS[sh]
    avail_w = W - 2 * MARGIN - GAP * len(col_w)
    avail_h = H - 2 * MARGIN - TITLE_BLOCK_H - GAP * len(row_h)
    return (sum(col_w) * s * sum(row_h) * s) / (avail_w * avail_h)


def _choose(b: Box, col_w, row_h, sheet: str | None, min_feature: float, kind: str,
            notes_h: float = 0.0) -> tuple[str, float]:
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
                    if _fits(sh, s, col_w, row_h, notes_h) and (min_feature <= 0 or min_feature * s >= MIN_FEATURE_ON_PAPER
                                                        or s <= 1 and min_feature * s >= 1.0):
                        return sh, s
    # 装配：最小的图幅，要求比例不小于 1:5 且填充率达标（给明细表留位）；
    # 但 A4 只能缩小画、A3 能放大一档比例时用 A3（A3 是常用图幅，小件不缩小画）
    if kind != "part" and (sheet is None or sheet == "auto"):
        s4 = next((s for s in SCALES if s <= 1 and _fits("A4", s, col_w, row_h, notes_h)), None)
        s3 = next((s for s in SCALES if s <= 1 and _fits("A3", s, col_w, row_h, notes_h)), None)
        if s4 is not None and s3 is not None and s4 < 1 and s3 > s4 and s3 >= 0.2:
            sheets = [s_ for s_ in sheets if s_ != "A4"]
    candidates = []
    for sh in sheets:
        s = next((s for s in SCALES if s <= 1 and _fits(sh, s, col_w, row_h, notes_h)), None)
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


def _layout(b: Box, spec: DrawingSpec, flat, sheet: str | None, min_feature: float = 0.0, kind: str = "part",
            col_h: float = 0.0):
    types, sizes, cells, col_w, row_h = _grid(b, spec, flat)
    # 展开图占右下格，正压在技术要求栏上：选图幅/比例时让出技术要求高度
    sh, s = _choose(b, col_w, row_h, sheet, min_feature, kind, max(col_h, NOTES_H if "flat_pattern" in types else 0.0))
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

    gkey = None  # 当前几何公差（type@target），轴类框格联合排布用

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
            if kind == "datum":  # 孔的基准符号贴在左下 45° 的轮廓上，沿径向引出：避开孔的十字中心线
                k = 0.7071
                return view, (u - h.max_dia / 2 * k, w - h.max_dia / 2 * k), (-k, -k)
            return view, (u, w), (0, -1)  # 框格附在孔标注下方
        if k == "pocket":  # 型腔底面：主视图（全剖）底面线上，符号立在型腔内，靠左 1/4
            pk = next(x for x in p.pockets if x["name"] == ref)
            u0, u1, _, _ = pk["profile"].bbox2d
            return "front", (u0 + 0.25 * (u1 - u0), p.bbox.zmax - pk["depth"]), (0, 1)
        if k == "bore":
            return _bore_anchor(p, int(ref), kind, views[0].scale if views else 1.0)
        if k == "keyway":  # 键槽中心平面：槽宽尺寸线上（轴键槽在断面图，见 _shaft_section_dim_sheet；轮毂键槽在左视图）
            hub = any(b.get("kw") and b["kw"]["name"] == ref for b in p.shaft_bores)
            return ("left" if hub else "section"), None, ((1, 0) if hub else (-1, 0)), {"keyway": ref}
        if k == "shoulder":  # 轴肩端面：锚点 = 端面上半段中点；框格位置见 shoulder_gtol_geom
            g = shoulder_gtol_geom(p, int(ref), views[0].scale if views else 1.0)
            return "front", (g["z"], g["r_at"]), (-g["from"], 0), {"shoulder": int(ref)}
        i = int(ref)
        z0, z1 = p.step_z(i)
        d = p.shaft_steps[i][1]
        frac = {"datum": 0.5, "roughness": 0.7, "gtol": 0.3}[kind]
        if kind == "gtol":
            frac = shaft_gtol_fracs(p, views[0].scale if views else 1.0).get(gkey, 0.3)
        if kind == "datum":  # 基准符号画在轴下方，避开键槽剖切符号（剖切线在键槽中点）
            cuts = [(f["z0"] + f["z1"]) / 2 for f in p.shaft_features if f["kind"] == "keyway" and f["step"] == i]
            if cuts:
                frac = max((0.5, 0.2, 0.8), key=lambda t: min(abs(z0 + t * (z1 - z0) - c) for c in cuts))
        elif kind == "roughness":  # 环槽等的引线从槽口斜向右上：符号（文字在右）避开，离槽越远越好
            grooves = [(f["z0"] + f["z1"]) / 2 for f in p.shaft_features if f["kind"] == "groove"
                       and z0 - 1 <= (f["z0"] + f["z1"]) / 2 <= z1 + 1]
            if grooves:
                frac = max((0.7, 0.5, 0.3, 0.2), key=lambda t: round(min(abs(z0 + t * (z1 - z0) - c) for c in grooves), 3))
            # 直径文字长于轴段直径时沿尺寸界线伸出轮廓（render_shaft._vdim）：Ra 符号（横线向右约 21 mm）避开这些位置
            # 上方的竖线（伸出的直径文字、尺寸界线、框格引线）会穿过符号文字：上下两侧都试，取冲突最少的位置
            sc = views[0].scale if views else 1.0
            top = shaft_top_obstacles(p, sc)
            bot = shaft_bottom_obstacles(p, sc)
            R = max(dd for _, dd in p.shaft_steps) / 2
            bottom_ok = (R - d / 2) * sc + 15 - 1.5 >= 19  # 引线 8 + 竖线 9 + 文字
            fr0 = frac

            def clash(t, obs):
                z = z0 + t * (z1 - z0)
                return sum(z - 3 / sc <= c <= z + 22 / sc for c in obs)
            cands = [(clash(t, top), 0, 0.0 if t == fr0 else 1.0, t, 1) for t in (fr0, 0.5, 0.3, 0.2, 0.1, 0.85)]
            if bottom_ok:
                cands += [(clash(t, bot), 1, 0.0 if t == fr0 else 1.0, t, -1) for t in (fr0, 0.5, 0.3, 0.2, 0.1, 0.85)]
            best = min(cands)
            _, _, _, frac, side = best
            g_here = [g for g in p.geotols if g["target"] == f"step:{i}"]
            if best[0] > 0 and g_here:  # 上下都无干净位置：标在该要素几何公差框格的上方（GB/T 131）
                g = g_here[0]
                fr = shaft_gtol_fracs(p, sc).get(f"{g['type']}@{g['target']}", 0.3)
                return "front", (z0 + fr * (z1 - z0), d / 2), (0, 1), {"on_frame": f"{g['type']}@{g['target']}"}
            if side < 0:
                return "front", (unbreak_z(p, z0 + frac * (z1 - z0)), -(d / 2)), (0, -1)
        return "front", (unbreak_z(p, z0 + frac * (z1 - z0)), (d / 2) if kind != "datum" else -(d / 2)), (0, 1 if kind != "datum" else -1)

    for d in p.datums:
        a = anchor(d["target"], "datum")
        if a:
            dims.append(Dim(a[0], "datum", None, None, d["name"], f"datum:{d['name']}", a[1],
                            {"normal": a[2], **(a[3] if len(a) > 3 else {})}))
    for g in p.geotols:
        gkey = f"{g['type']}@{g['target']}"
        a = anchor(g["target"], "gtol")
        if a:
            dims.append(Dim(a[0], "gtol", None, g["value"], g["text"], f"gtol:{g['type']}@{g['target']}", a[1],
                            {"normal": a[2], "cells": [g["symbol"], g["zone"], *g["datums"]],
                             **({"attach": "callout"} if g["kind"] == "hole" else {}),
                             **(a[3] if len(a) > 3 else {})}))
    for r in p.roughness:
        a = anchor(r["target"], "roughness")
        if a:
            dims.append(Dim(a[0], "roughness", None, r["ra"], f"Ra {r['ra']:g}", f"ra:{r['target']}", a[1],
                            {"normal": a[2], **(a[3] if len(a) > 3 else {})}))
    return dims

def _notes_and_title(spec: DrawingSpec, p: ResolvedPart, sheet: str, scale: float, today: date | None):
    notes = ["技术要求："]
    n = 1
    if not p.casting.get("blank"):  # 铸件毛坯：尺寸公差按 GB/T 6414（见铸造要求），不倒钝去毛刺
        notes.append(f"1. 未注尺寸公差按 {spec.general_tolerance}。")
        n = 2
        if spec.general_geometric_tolerance and not p.base_type.startswith("weldment") and p.base_type != "sheet_metal":
            notes.append(f"{n}. 未注几何公差按 {spec.general_geometric_tolerance}。")
            n += 1
        notes.append(f"{n}. 锐边倒钝，去毛刺。")
        n += 1
    if p.surface_treatment and p.surface_treatment != "无":
        notes.append(f"{n}. 表面处理：{p.surface_treatment}。")
        n += 1
    if p.heat.get("text"):
        notes.append(f"{n}. {p.heat['text']}。")
        n += 1
    if p.heat.get("local"):  # 合并同一工艺的轴颈；直径名写出来，原生图上没有粗点画线时也看得懂
        groups: dict[str, list[str]] = {}
        for lh in p.heat["local"]:
            groups.setdefault(lh["text"], []).append(_shaft_dia_label(p, lh["step"], False))
        for text, names in groups.items():
            uniq = [f"{x}（{names.count(x)} 处）" if names.count(x) > 1 else x for x in dict.fromkeys(names)]
            notes.append(f"{n}. {'、'.join(uniq)} 轴颈表面（图中粗点画线处）{text}。")
            n += 1
    asm = [h for h in getattr(p, "cross_holes", []) if h.get("at_assembly")]
    if asm:
        what = "、".join(h["callout"].replace(" 配作", "") for h in asm)
        notes.append(f"{n}. {what} 孔与相配件装配后配钻{'铰' if any(h['tol'] for h in asm) else ''}。")
        n += 1
    if p.shoulder_fillets:
        notes.append(f"{n}. 未注轴肩圆角 R{p.shoulder_fillets[0]['R']:g}。")
        n += 1
    if p.base_type == "shaft" and p.shaft_chamfer:
        notes.append(f"{n}. 未注倒角 C0.5。")
        n += 1
    keep = {h["keep"] for h in p.center_holes}
    if keep & {"required", "forbidden"}:
        where = "两端" if len(p.center_holes) == 2 else ""
        notes.append(f"{n}. {where}中心孔完工后{'须保留' if 'required' in keep else '不得保留'}（GB/T 4459.5）。")
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
    if not p.casting:  # 铸造材料但没做毛坯设计（如车削的灰铸铁端盖）：仍要写铸件质量与去应力要求
        from .materials import cast_part_notes
        for line in cast_part_notes(p.material):
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
    if p.gear:
        from .gear_drawing import plan_gear_drawing
        return plan_gear_drawing(spec, p, today)
    b = p.bbox
    min_feat = min((h.drill for h in p.holes), default=0.0)
    # 焊接件尺寸大、孔是焊后加工的少数特征：按装配图方式选图幅（比例 ≥1:5 的最小图幅）
    kind = "weldment" if p.base_type == "weldment_body" else "part"
    spec, view_infos = prune_default_views(spec, p, min_feat) if kind == "part" else (spec, [])
    # 焊接件：标题栏之上还有技术要求（约 5 条）与切割清单，视图伸进右侧栏时须让出
    col_h = 77 + 7 * (len(p.cut_list) + 1) if kind == "weldment" else 0.0
    sheet, scale, views = _layout(b, spec, p.flat_pattern, spec.sheet, 0.0 if kind == "weldment" else min_feat, kind,
                                  col_h)
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
                dims.append(Dim(view, "callout", None, None,
                                with_deviations(h.callout) if spec.fit_deviations else h.callout, f"{h.name}",
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
        # 型腔定位：矩形型腔左下角、圆形型腔圆心进坐标尺寸（大小在型腔标注里，不重复标）
        if "top" in vtypes and p.base_type in ("extrude", "cast"):
            ha, va = VIEW_AXES["top"]
            origin = {ha: vrange(b, ha)[0], va: vrange(b, va)[0]}
            for pk in p.pockets:
                if pk["face"] not in ("top", "bottom") or pk["profile"].shape not in ("rect", "circle"):
                    continue
                pr = pk["profile"]
                u, v_ = pr.data["center"] if pr.shape == "circle" else (pr.bbox2d[0], pr.bbox2d[2])
                pt = {ha: vc((u, v_, b.zmax), ha), va: vc((u, v_, b.zmax), va)}
                for ax in (ha, va):
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
            if pr.shape == "rect" and pr.data.get("corner_radius"):
                txt += f" 4×R{pr.data['corner_radius']:g}"
            pv = hole_view(pk["face"], vtypes) or views[0].type
            at_ = None
            if pv in VIEW_AXES and pk["face"] in ("top", "bottom"):
                pha, pva = VIEW_AXES[pv]
                z_ = b.zmax if pk["face"] == "top" else b.zmin
                pts_ = [(vc((x, y, z_), pha), vc((x, y, z_), pva)) for x, y in pr.drawn()]
                at_ = max(pts_, key=lambda q: q[0] + q[1])  # 型腔轮廓右上角（有圆角时在弧中点）
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
                # 写在折弯线中部：横、竖折弯线的说明分处各自中部，不在四角相撞
                at = ((fu0 + fu1) / 2, bd["flat_pos"]) if bd["flat_axis"] == "v" \
                    else (bd["flat_pos"], (fv0 + fv1) / 2)
                dims.append(Dim("flat_pattern", "callout", None, bd["angle"],
                                f"{word} {bd['angle']:g}° R{bd['radius']:g}", f"bend{i}", at))
    ht_rows, ht_view = ([], "") if spec.dimensions == "none" else _hole_table(dims, p, views, spec)
    notes, tb = _notes_and_title(spec, p, sheet, scale, today)
    plan = DrawingPlan(p.name, "part", sheet, SHEETS[sheet], scale, spec.projection, views, dims, notes, tb,
                       flat_pattern=p.flat_pattern)
    plan.hole_table, plan.hole_table_view = ht_rows, ht_view
    if not ht_rows:
        _balance_views(plan)
    if ht_rows:
        plan.hole_table_box = _place_table(plan, HOLE_TABLE_W, 5 * (len(ht_rows) + 1) + 8)
    plan.missing_views = sorted(missing_views)
    plan.extra_findings += view_infos
    plan.revisions = list(spec.revisions)
    _plan_sections(plan, spec, p)
    _hole_ra_to_section(plan, p)
    if spec.dimensions != "none":
        pitch_dim_sheet(plan, p)
        ordinate_dim_sheet(plan, p)  # 坐标值位置先定，引出标注避开它们
        place_labels(plan, p.bbox, part=p)
        precision_sheet(plan, p)
        overall_dim_sheet(plan, p)
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


def _balance_views(plan: "DrawingPlan") -> None:
    for v, m in _balance_moves(plan):
        v.origin_on_sheet = (v.origin_on_sheet[0], v.origin_on_sheet[1] - m)


def _balance_moves(plan: "DrawingPlan") -> list:
    """视图组整体下移，使图纸上下留白均匀（工程师不会把视图全挤在左上角）。
    视图外扩：下/左各 30 mm（总体尺寸、坐标尺寸带），上 12 mm（剖视名称），右 16 mm；
    与右侧标题栏/技术要求栏横向重叠的视图，下沿不得低于该栏顶部。"""
    W, H = plan.sheet_size
    if not plan.views:
        return []
    col_x = W - 5 - 180
    col_top = 5 + 56 + 14 + 5 * len(plan.notes) + 6 + 16 \
        + (7 * (len(plan.bom) + 1) if plan.bom else 0)  # 同 right_column_bottom（含"其余"粗糙度、明细表）
    proj = [v for v in plan.views if v.type != "iso"]  # 轴测图不标尺寸、不与投影对齐：原位不动
    if not proj:
        return []
    def box(v, dy, pad):
        cx, cy = v.origin_on_sheet
        vw, vh = v.size_mm
        return (cx - vw / 2 - pad, cy - dy - vh / 2 - pad, cx + vw / 2 + pad, cy - dy + vh / 2 + pad)
    isos = [v for v in plan.views if v.type == "iso"]
    top_gap, bot_gap = float("inf"), float("inf")
    for v in proj:
        cx, cy = v.origin_on_sheet
        vw, vh = v.size_mm
        x1, y0, y1 = cx + vw / 2 + 16, cy - vh / 2 - 30, cy + vh / 2 + 12
        top_gap = min(top_gap, H - MARGIN - 2 - y1)
        bot_gap = min(bot_gap, y0 - ((col_top + 4) if x1 > col_x else MARGIN + 2))
    m = (bot_gap - top_gap) / 2
    if bot_gap < 0 < top_gap:  # 压到右侧技术要求栏：视图组整体上移（轴测图同移，上方须有余量）
        up = min(-bot_gap, top_gap, min((H - MARGIN - 2 - box(v, 0, 0)[3] for v in isos), default=1e9))
        return [(v, -round(up, 1)) for v in proj + isos] if up > 0.5 else []
    if m <= 2:
        return []
    m = min(m, bot_gap)


    def iso_room(v):  # 轴测图自身下方可让出的距离（无尺寸，留 4 mm）
        x0_, y0_, x1_, _ = box(v, 0, 0)
        return y0_ - 4 - ((col_top + 4) if x1_ > col_x else MARGIN + 2)

    def moves(m):  # 轴测图：能跟着下移就一起移，否则原位
        return {id(v): (m if iso_room(v) >= m else 0.0) for v in isos}
    while m > 2:
        mv = moves(m)
        if not any(_box_hit(box(v, m, 16), box(w, mv[id(w)], 2)) > 0 for v in proj for w in isos):
            break
        m -= 2  # 下移后压到轴测图：少移一点
    if m <= 2:
        return []
    m = round(m, 1)
    mv = moves(m)
    return [(v, mv.get(id(v), m)) for v in proj + isos]


def notes_x(plan: "DrawingPlan") -> float:
    """技术要求文字块左边 x（图纸 mm）：缺省与标题栏左边对齐；与视图（含 8 mm 尺寸带）重叠时右移到视图右侧，
    文字仍须在图框内。SVG 与 CAD 共用。"""
    W, _ = plan.sheet_size
    x0 = W - 5 - 180
    if not plan.notes or plan.cut_list or plan.bom:
        return x0
    tw = max(text_width(n, 3.5) for n in plan.notes)
    y_lo, y_hi = 5 + 56 + 6 - 1, 5 + 56 + 6 + 5 * len(plan.notes) + 4
    boxes = []
    for v in plan.views:
        cx, cy = v.origin_on_sheet
        vw, vh = v.size_mm
        boxes.append((cx - vw / 2 - 8, cy - vh / 2 - 8, cx + vw / 2 + 8, cy + vh / 2 + 8))
    hit = [b for b in boxes if b[0] < x0 + tw and x0 < b[2] and b[1] < y_hi and y_lo < b[3]]
    if not hit:
        return x0
    x = max(b[2] for b in hit) + 2
    return x if x + tw <= W - 8 else x0


def notes_origin(plan: "DrawingPlan") -> tuple[float, float]:
    """技术要求文字块左下角（图纸 mm，同 render_svg：标题栏上方，最后一行基线）。"""
    return (notes_x(plan), 5 + 56 + 11)


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
        # 上方留 12 mm：断面图/剖视图名称（A-A）写在视图上方
        obstacles.append((cx - vw / 2 - 6, cy - vh / 2 - 6, cx + vw / 2 + 14, cy + vh / 2 + (12 if v.label else 6)))
    if hb:
        obstacles.append(tuple(hb))
    for d in plan.dims:  # 已定图纸坐标的尺寸线/数字（轴类主视图下方的长度尺寸带比视图框外 6 mm 更远）
        ex = d.extra or {}
        pts = [q for q in ex.get("sheet_pts") or [] if isinstance(q, (tuple, list)) and len(q) == 2]
        if len(pts) >= 2:
            xs_, ys_ = [q[0] for q in pts], [q[1] for q in pts]
            obstacles.append((min(xs_) - 1, min(ys_) - 1, max(xs_) + 1, max(ys_) + 1))
        sp = ex.get("sheet_pos")
        if isinstance(sp, (tuple, list)) and len(sp) == 2 and d.text:
            tw_ = text_width(d.text, 3.5)
            obstacles.append((sp[0] - tw_ / 2 - 1, sp[1] - 1.5, sp[0] + tw_ / 2 + 1, sp[1] + 4.5))
            if d.kind == "linear" and d.axis == "z" and d.at is not None:  # 轴向尺寸线：横贯其所在视图
                v_ = next((v for v in plan.views if v.type == d.view), None)
                if v_ is not None:
                    cx_, w_ = v_.origin_on_sheet[0], v_.size_mm[0]
                    obstacles.append((cx_ - w_ / 2 - 2, sp[1] - 3, cx_ + w_ / 2 + 2, sp[1] + 4.5))

    def free(x, y):
        box = (x, y - 2, x + GR_W, y + GR_H)
        return box[3] <= H - 6 and not any(_box_hit(box, o) > 0 for o in obstacles)

    cands = [(x0, y0), (W - 6 - GR_W, y0)] + [(x0, y0 + 4 * k) for k in range(1, 60)] + [(W - 6 - GR_W, H - 6 - GR_H)]
    hit = next(((round(x, 2), round(y, 2)) for x, y in cands if free(x, y)), None)
    if hit:
        return hit
    # 都不空：再试图纸左侧下部一列，仍不行取与视图/尺寸重叠面积最小的位置
    cands2 = cands + [(15.0, y0 + 4 * k) for k in range(0, 60)]

    def cost(x, y):
        box = (x, y - 2, x + GR_W, y + GR_H)
        return (box[3] > H - 6) * 1e6 + sum(_box_hit(box, o) for o in obstacles)
    x, y = min(cands2, key=lambda c: cost(*c))
    return (round(x, 2), round(y, 2))


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
            lo_d, hi_d = (min(depths), max(depths)) if depths else (0.0, h.depth)
            for pk in p.pockets:  # 孔穿进对面的型腔：孔壁只到型腔底
                if pk["face"] != h.face and pk["face"] in ("top", "bottom") and not pk.get("through"):
                    u0, u1, v0, v1 = pk["profile"].bbox2d
                    if u0 <= inst.xyz[0] <= u1 and v0 <= inst.xyz[1] <= v1:
                        hi_d = min(hi_d, (p.bbox.zmax - p.bbox.zmin) - pk["depth"])
            if (hi_d - lo_d) * vscale.get(sec.view, 1.0) < 12:  # 孔壁太矮放不下符号（√ 高 8.4 + 文字）
                continue
            zmid_depth = (lo_d + hi_d) / 2
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
    # 序号避开主视图总体尺寸数字（左侧竖直尺寸在 vx0−10，下方水平尺寸在 vy0−10 或坐标尺寸带外 vy0−22）
    mcx, mcy = main.origin_on_sheet
    mw, mh = main.size_mm
    vx0, vy0 = mcx - mw / 2, mcy - mh / 2
    tw_h, tw_v = text_width(f"{_extent(b, ha):g}", 3.5), text_width(f"{_extent(b, va):g}", 3.5)
    avoid = [(vx0 - 15, mcy - tw_v / 2 - 1, vx0 - 9, mcy + tw_v / 2 + 1)]
    avoid += [(mcx - tw_h / 2 - 1, vy0 - yy - 1, mcx + tw_h / 2 + 1, vy0 - yy + 5) for yy in (10, 22)]
    rows, plan.balloons = layout_balloons(rows, shim, main, plan.sheet_size, avoid=avoid)
    for r in rows:
        r["_r"]["item"] = r["item"]
    plan.cut_list.sort(key=lambda r: r["item"])
    _place_weld_symbols(plan, main)
    # 孔标注合并、切割清单与焊缝符号加入后重新排布引出标注，避开序号和焊缝符号
    reserved = [(b_["pos"][0] - 6, b_["pos"][1] - 6, b_["pos"][0] + 6, b_["pos"][1] + 6) for b_ in plan.balloons if "pos" in b_]
    reserved += [weld_symbol_box(d.extra["sheet_at"], d.extra["sheet_pos"]) for d in plan.dims if d.kind == "weld"]
    place_labels(plan, p.bbox, reserved, part=p)
    precision_sheet(plan, p)
    return plan


WELD_SYM_W = 36.0  # 焊缝符号基准线 26 + 尾部方法代号


def weld_symbol_box(at, pos) -> tuple[float, float, float, float]:
    """焊缝符号（基准线 + 文字 + 尾部）的图纸占位，与 render_svg._weld_symbol 一致。"""
    return (pos[0] - 2, pos[1] - 4.8, pos[0] + WELD_SYM_W, pos[1] + 4.6)


def _place_weld_symbols(plan: "DrawingPlan", main: "ViewPlan") -> None:
    """焊缝符号定位：在焊缝点四周的候选位置中选一个——不压序号圆、序号引线、别的焊缝符号；
    其次避开主视图及其尺寸带（左/下 30 mm、右 16 mm），再其次离焊缝点近。"""
    cx, cy = main.origin_on_sheet
    vw, vh = main.size_mm
    W, H = plan.sheet_size
    view_box = (cx - vw / 2 - 30, cy - vh / 2 - 30, cx + vw / 2 + 16, cy + vh / 2 + 2)
    others = []  # 各视图本体（含主视图；压上即硬冲突）
    for v in plan.views:
        ox, oy = v.origin_on_sheet
        ow, oh = v.size_mm
        others.append((ox - ow / 2 - 1, oy - oh / 2 - 1, ox + ow / 2 + 1, oy + oh / 2 + 1))
    balls = [(b["pos"][0] - 6, b["pos"][1] - 6, b["pos"][0] + 6, b["pos"][1] + 6) for b in plan.balloons if "pos" in b]
    leads = [(b["anchor"], b["pos"]) for b in plan.balloons if "pos" in b and "anchor" in b]
    taken: list = []
    wleads: list = []
    for d in plan.dims:
        if d.kind != "weld" or "sheet_at" not in (d.extra or {}):
            continue
        ax, ay = d.extra["sheet_at"]
        best = None
        cands = [(ax + dx, ay + dy) for dx in (10, 16, 24, 34, 46, -10 - WELD_SYM_W, -16 - WELD_SYM_W, -26 - WELD_SYM_W)
                 for dy in (12, 18, 26, 34, 42, 52, 62, -12, -18, -26, -34, -42, -52)]
        # 大视图里焊缝点四周都是构件：引到主视图外（上/下/左/右，逐行外推）
        t_, b_, l_, r_ = cy + vh / 2, cy - vh / 2, cx - vw / 2, cx + vw / 2
        for k in range(4):
            for dx in (10, -10 - WELD_SYM_W, 24, -24 - WELD_SYM_W):
                cands += [(ax + dx, t_ + 8 + 9 * k), (ax + dx, b_ - 34 - 9 * k)]
            for dy in (8, -8, 18, -18):
                cands += [(r_ + 22 + 12 * k, ay + dy), (l_ - 34 - WELD_SYM_W - 12 * k, ay + dy)]
        for pos in cands:
                dx, dy = pos[0] - ax, pos[1] - ay
                bx = weld_symbol_box((ax, ay), pos)
                if bx[0] < MARGIN + 2 or bx[2] > W - MARGIN - 2 or bx[1] < MARGIN + 2 or bx[3] > H - MARGIN - 2:
                    continue
                hard = sum(_box_hit(bx, o) > 0 for o in balls + taken + others)
                hard += sum(_seg_box(p1, p2, bx) for p1, p2 in leads)
                hard += sum(_seg_box((ax, ay), pos, o) for o in balls + taken)
                hard += sum(_seg_box(p1, p2, bx) for p1, p2 in wleads)  # 已放焊缝符号的引线不穿本符号
                L_ = math.hypot(dx, dy) or 1.0  # 引线不穿本符号左端上下的焊脚文字（K3）
                q_ = (pos[0] - dx / L_ * 1.2, pos[1] - dy / L_ * 1.2)
                hard += sum(_seg_box((ax, ay), q_, tb) for tb in ((pos[0] + 0.3, pos[1] + 0.6, pos[0] + 6, pos[1] + 4.4),
                                                                (pos[0] + 0.3, pos[1] - 4.4, pos[0] + 6, pos[1] - 0.6)))
                soft = _box_hit(bx, view_box) > 0
                key = (hard, soft, abs(dx) + abs(dy))
                if best is None or key < best[0]:
                    best = (key, pos, bx)
        if best is not None:
            d.extra["sheet_pos"] = (round(best[1][0], 2), round(best[1][1], 2))
            taken.append(best[2])
            wleads.append(((ax, ay), best[1]))


def _as_body(p: ResolvedPart) -> ResolvedPart:
    """以普通零件规划焊接件视图（避免递归进入焊接件分支）。"""
    import dataclasses
    return dataclasses.replace(p, base_type="weldment_body")


# ---------------- 轴类零件图 ----------------
def tol_text(v: float, code: str) -> str:
    from .standards import tol_text as _t
    return _t(v, code)


def _dia_text(d: float, tol: str | None, deviations: bool = False) -> str:
    if deviations and tol:
        from .standards import fit_text
        return fit_text(d, tol)
    return f"Φ{d:g}{tol or ''}"


_FIT_RE = re.compile(r"Φ(\d+(?:\.\d+)?)((?:[A-Z]{1,2}|[a-z]{1,2})\d{1,2})(?![\d(])")


def with_deviations(text: str) -> str:
    """孔标注等文字中的 Φ60H7 → Φ60H7(+0.030/0)（GB/T 1800.2 查表；表外不变）。"""
    from .standards import fit_text
    return _FIT_RE.sub(lambda m: fit_text(float(m.group(1)), m.group(2)), text)


def plan_shaft_drawing(spec: DrawingSpec, p: ResolvedPart, today: date | None = None) -> DrawingPlan:
    """轴类按工程师习惯：主视图轴线水平、键槽朝前；每个键槽一个移出断面图（A-A、B-B…）放在主视图下方。
    尺寸：各轴段长度（链式）+ 总长、各段直径（带公差带）、键槽长度与定位、断面上的键宽和 d-t1、环槽标注、端部倒角。"""
    b = p.bbox
    Ltot = b.zmax - b.zmin
    D = b.xmax - b.xmin
    kws = sorted((f for f in p.shaft_features if f["kind"] == "keyway"), key=lambda f: f["z0"])  # 断面字母从左到右
    sec_d = max((f["d"] for f in kws), default=0.0)
    brk = shaft_breaks(p) if spec.broken_view else []
    Lv = Ltot - sum(b_ - a_ for a_, b_ in brk) + len(brk) * BREAK_GAP  # 断开后的视图长（模型 mm，间隙按 1:1 估）
    # 右端中心孔标注向右伸出（文字约 40 mm，图纸尺寸，按 1:1 预留）；左端用左侧留白
    ch_r = max([text_width(f"GB/T 4459.5-{h['code']}", 3.0) + 12 for h in p.center_holes if h["end"] == "end"]
               + [text_width(h["callout"], 3.0) + 12 for h in p.end_holes if h["side"] == "end"], default=0.0)
    col_w = [Lv + max(92 if p.gear else 0, ch_r)]  # 齿轮轴：右上角参数表宽 80 + 间隙（按 1:1 预留）
    hub_kws = [b for b in p.shaft_bores if b.get("kw")]
    if hub_kws or p.flange_holes:  # 内键槽/法兰均布孔：主视图右侧加左视图（第一角：从左端看的视图放在右边）
        col_w.append(D + 30 + (30 if p.flange_holes else 0))  # 均布孔/分布圆引线标注向右伸出
    # 主视图上方：几何公差框格一行（轮廓上方 22–28 mm，比默认余量多约 3 mm）；
    # 同段还有粗糙度时可能标在框格上方（再高 ~12 mm，真出现时才整体下移，见下文）
    g_steps = {g["target"] for g in p.geotols if g["target"].startswith("step:")}
    extra_top = 3.0 if g_steps else 0.0
    dets = [f for f in p.shaft_features if f["kind"] == "groove" and f["type"] == "grinding"]  # 小结构 → 局部放大图
    row_h = [D + extra_top + (12.0 if g_steps & {r["target"] for r in p.roughness} else 0.0)] + \
        ([max(sec_d, 30.0 if dets else 0.0)] if kws or dets else [])
    min_feat = min((f["width"] for f in p.shaft_features if "width" in f), default=0.0)
    sheet, scale = _choose(b, col_w, row_h, spec.sheet, min_feat, "part")
    # 直径文字带极限偏差放不进该段（竖写长于直径）而只注公差带代号放得下 → 只注代号（GB/T 4458.5 允许）
    p._dia_code_only = set()
    for j_, (L_, d_) in enumerate(p.shaft_steps):
        z_ = p.step_z(j_)
        room_ = shaft_text_room(p, (z_[0] + z_[1]) / 2, d_) * scale - 2
        if text_width(_shaft_dia_label(p, j_), 3.5) > room_ >= text_width(_shaft_dia_label(p, j_, False), 3.5):
            p._dia_code_only.add(j_)
    for j_ in range(len(p.shaft_steps)):  # 轮廓内找不到位置（键槽占满）、文字须伸出轮廓：伸出越短越好 → 只注代号
        if shaft_dia_blocked(p, j_, scale):
            p._dia_code_only.add(j_)
    W, H = SHEETS[sheet]
    x0 = MARGIN + GAP + 8
    cy_main = H - MARGIN - GAP / 2 - D * scale / 2 - 8 - extra_top
    front = ViewPlan("front", scale, (Ltot * scale, D * scale), (0.0, cy_main), breaks=brk)
    wv = shaft_view_width(front, Ltot)
    front.size_mm = (wv, D * scale)
    front.origin_on_sheet = (x0 + wv / 2, cy_main)
    views = [front]
    if hub_kws or p.flange_holes:
        views.append(ViewPlan("left", scale, (D * scale, D * scale), (x0 + wv + GAP + 4 + D * scale / 2, cy_main)))
    dgeo = [detail_geom(f, scale) for f in dets]
    row_half = max([sec_d * scale / 2] + [ds_ * rm_ for ds_, rm_ in dgeo])
    cy_sec = cy_main - D * scale / 2 - 44 - row_half - 6
    # 剖切字母不得与基准字母重复（GB/T 4458.6），并跳过 I、O
    used = {d["name"] for d in p.datums} | {"I", "O"}
    letters = [c for c in "ABCDEFGHIJKLMNOPQRSTUVWXYZ" if c not in used]
    for i, f in enumerate(kws):
        label = f"{letters[i]}-{letters[i]}"
        zc = (f["z0"] + f["z1"]) / 2
        views.append(ViewPlan("section", scale, (f["d"] * scale, f["d"] * scale), (shaft_sx(front, Ltot, zc), cy_sec),
                              label=label, target=f["name"]))
    # 相邻键槽近、断面图互相重叠：从左到右推开（中心距 ≥ 两半宽 + 16 mm 尺寸带），整串以原位置均值为中心
    secs0 = sorted((v for v in views if v.type == "section"), key=lambda v: v.origin_on_sheet[0])
    if len(secs0) > 1:
        xs0 = [v.origin_on_sheet[0] for v in secs0]
        ps = [xs0[0]]
        for k in range(1, len(secs0)):
            need = (secs0[k - 1].size_mm[0] + secs0[k].size_mm[0]) / 2 + 16
            ps.append(max(xs0[k], ps[-1] + need))
        sh_ = sum(ps) / len(ps) - sum(xs0) / len(xs0)
        sh_ = min(sh_, ps[0] - secs0[0].size_mm[0] / 2 - (MARGIN + 12))  # 左端不出图框（留尺寸带）
        for v, x_ in zip(secs0, ps):
            v.origin_on_sheet = (round(x_ - max(sh_, 0.0), 2), v.origin_on_sheet[1])
    # 断面图伸进右侧技术要求/标题栏一列且低于该栏顶部：左移让开（移出剖切位置的断面图带 X-X 标注，GB/T 17452 允许）
    n_notes_ = len(_notes_and_title(spec, p, sheet, scale, today)[0])
    col_x_, col_top_ = W - 5 - 180, 5 + 56 + 14 + 5 * n_notes_ + 6 + 16
    secs_ = sorted((v for v in views if v.type == "section"), key=lambda v: -v.origin_on_sheet[0])
    for v in secs_:
        cx_, cy_ = v.origin_on_sheet
        w_, h_ = v.size_mm
        if cx_ + w_ / 2 + 16 <= col_x_ or cy_ - h_ / 2 - 10 >= col_top_ + 4:
            continue
        x_new = col_x_ - 16 - w_ / 2
        others = [(o.origin_on_sheet[0] - o.size_mm[0] / 2 - 16, o.origin_on_sheet[0] + o.size_mm[0] / 2 + 16)
                  for o in views[1:] if o is not v and o.type == "section"]
        if x_new - w_ / 2 > MARGIN + 4 and not any(a < x_new + w_ / 2 and x_new - w_ / 2 < b for a, b in others):
            v.origin_on_sheet = (round(x_new, 2), cy_)
    # 局部放大图（GB/T 4458.1）：放在被放大部位下方的一行，避开断面图；罗马数字从左到右
    taken = [(v.origin_on_sheet[0] - v.size_mm[0] / 2, v.origin_on_sheet[0] + v.size_mm[0] / 2) for v in views[1:]]
    for k, (f, (ds_, rm_)) in enumerate(sorted(zip(dets, dgeo), key=lambda t: t[0]["z0"])):
        zc = (f["z0"] + f["z1"]) / 2
        w = 2 * rm_ * ds_
        x0_ = shaft_sx(front, Ltot, zc)

        def slide(sg):  # 向右（sg=1）或向左（−1）让开已放的断面/放大图
            x_ = x0_
            for _ in range(len(taken) + 1):
                hit = [(a_, b_) for a_, b_ in taken if x_ - w / 2 - GAP / 2 < b_ and x_ + w / 2 + GAP / 2 > a_]
                if not hit:
                    break
                x_ = (max(b_ for _, b_ in hit) + GAP / 2 + w / 2 + 1) if sg > 0 else (min(a_ for a_, _ in hit) - GAP / 2 - w / 2 - 1)
            return x_
        # 右移出图框（含上方标号/比例、右侧 12 mm 尺寸带）则改向左；两边都不行取出界少的
        opts = [slide(1), slide(-1)]
        xc_ = min(max(x0_, MARGIN + 12 + w / 2), W - MARGIN - 12 - w / 2)  # 部位贴近图框：夹回框内
        if not any(xc_ - w / 2 - GAP / 2 < b_ and xc_ + w / 2 + GAP / 2 > a_ for a_, b_ in taken):
            opts.append(xc_)
        over = lambda x_: max(0.0, x_ + w / 2 + 12 - (W - MARGIN), (MARGIN + 12) - (x_ - w / 2))  # noqa: E731
        x = min(opts, key=lambda x_: (over(x_) > 0, over(x_), abs(x_ - x0_)))
        taken.append((x - w / 2, x + w / 2))
        views.append(ViewPlan("detail", ds_, (w, w), (x, cy_sec), label=ROMAN[k], target=f["name"],
                              detail={"zc": zc, "yc": f["d"] / 2, "rm": rm_}))
    dims: list[Dim] = []
    if spec.dimensions != "none":
        z = 0.0
        for i, (L, d) in enumerate(p.shaft_steps):
            tol = p.shaft_tolerances[i] if p.shaft_tolerances else None
            th = next((f for f in p.shaft_features if f["kind"] == "thread" and f["step"] == i), None)
            gr = next((f for f in p.shaft_features if f["kind"] == "gear" and f["step"] == i), None)
            if gr is not None:  # 齿轮段：直径 = 齿顶圆，按齿顶圆公差带注偏差
                tol = p.gear["tip_tolerance"]
            # 螺纹段：螺纹代号注在大径上替代 Φ（GB/T 4459.1）
            dims.append(Dim("front", "diameter", None, d, th["callout"] if th else _dia_text(
                d, tol, spec.fit_deviations and i not in getattr(p, "_dia_code_only", ())),
                            f"step{i}_dia",
                            (z + L / 2, d)))
            if len(p.shaft_steps) > 1:
                dims.append(Dim("front", "linear", "z", L, f"{L:g}", f"step{i}_len", (z, z + L)))
            z += L
        dims.append(Dim("front", "linear", "z", Ltot, f"{Ltot:g}", "overall_z", (0.0, Ltot)))
        for k, b in enumerate(p.shaft_bores):  # 内孔：直径（带公差带与偏差）；盲孔注孔深（从孔口端面起）
            if not b.get("kw"):  # 带键槽的孔：剖切面过键槽、主视图无上孔壁 → 直径改在左视图引线标注（见下）
                dims.append(Dim("front", "diameter", None, b["d"], _dia_text(b["d"], b["tol"], spec.fit_deviations),
                                f"bore{k}_dia", ((b["z0"] + b["z1"]) / 2, b["d"])))
            if not b["through"]:
                dims.append(Dim("front", "linear", "z", b["z1"] - b["z0"], f"{b['z1'] - b['z0']:g}", f"{b['name']}_depth",
                                (b["z0"], b["z1"])))
        if p.shaft_chamfer:
            dims.append(Dim("front", "callout", None, p.shaft_chamfer, f"2×C{p.shaft_chamfer:g}", "chamfer",
                            (0.0, p.shaft_steps[0][1] / 2)))
        for h in p.end_holes:  # 轴端螺孔：GB/T 4459.1 简化标注（螺纹代号↓螺纹深 孔↓底孔深），从端面轴线处引出
            dims.append(Dim("front", "callout", None, h["d"], h["callout"], f"eh:{h['side']}",
                            (0.0 if h["side"] == "start" else Ltot, 0.0)))
        for h in p.cross_holes:  # 径向横孔：主视图上是圆，引线标注 + 孔心到轴段起点的定位尺寸
            text = h["callout"]
            if not h["thread"]:
                text = _dia_text(h["d"], h["tol"] or None, spec.fit_deviations) + text[len(f"Φ{h['d']:g}{h['tol']}"):]
            dims.append(Dim("front", "callout", None, h["d"], text, f"xh:{h['name']}", (h["z"], 0.0)))
            if h["at_assembly"]:  # 配作孔：位置由装配确定，零件图不注定位尺寸
                continue
            sz0 = p.step_z(h["step"])[0]
            if h["step"] == len(p.shaft_steps) - 1 and len(p.shaft_steps) > 1:  # 轴端段：从轴端面定位
                dims.append(Dim("front", "linear", "z", Ltot - h["z"], f"{Ltot - h['z']:g}", f"{h['name']}_pos", (h["z"], Ltot)))
            else:
                dims.append(Dim("front", "linear", "z", h["z"] - sz0, f"{h['z'] - sz0:g}", f"{h['name']}_pos", (sz0, h["z"])))
        for h in p.center_holes:  # 中心孔：GB/T 4459.5 简化标注，从端面轴线处引出
            dims.append(Dim("front", "callout", None, h["d"], f"GB/T 4459.5-{h['code']}", f"ch:{h['end']}",
                            (0.0 if h["end"] == "start" else Ltot, 0.0), {"keep": h["keep"]}))
        for f in p.shaft_features:
            sz0 = p.step_z(f["step"])[0]
            if f["kind"] == "gear":  # 齿面粗糙度注在分度线延长线上（齿段右端外）
                ra = p.gear["flank_ra"]
                dims.append(Dim("front", "roughness", None, ra, f"Ra {ra:g}", f"ra:flank:{f['name']}",
                                (f["z1"] + 3 / scale, f["pd"] / 2), {"normal": (0, 1)}))
                continue
            if f["kind"] == "thread":  # 未满整段：注螺纹长度（自轴段端面起）
                if not f["full"]:
                    dims.append(Dim("front", "linear", "z", f["length"], f"{f['length']:g}", f"{f['name']}_len",
                                    (f["z0"], f["z1"])))
                continue
            if f["kind"] == "keyway":
                dims.append(Dim("front", "linear", "z", f["length"], f"{f['length']:g}", f"{f['name']}_len",
                                (f["z0"], f["z1"])))
                if f["step"] == len(p.shaft_steps) - 1 and len(p.shaft_steps) > 1:
                    # 轴端键槽从轴端面定位（工艺基准在端面；尺寸界线也远离相邻轴段上的符号）
                    if Ltot - f["z1"] > 1e-6:
                        dims.append(Dim("front", "linear", "z", Ltot - f["z1"], f"{Ltot - f['z1']:g}", f"{f['name']}_pos",
                                        (f["z1"], Ltot)))
                elif abs(f["z0"] - sz0) > 1e-6:
                    dims.append(Dim("front", "linear", "z", f["z0"] - sz0, f"{f['z0'] - sz0:g}", f"{f['name']}_pos",
                                    (sz0, f["z0"])))
                dims.append(Dim("section", "linear", "y", f["width"], (tol_text(f["width"], "N9") if spec.fit_deviations else f"{f['width']:g}N9"), f"{f['name']}_w",
                                (f["d"], f["width"])))
                dims.append(Dim("section", "linear", "x", f["d"] - f["depth"], f"{f['d'] - f['depth']:g}",
                                f"{f['name']}_t", (f["d"], f["depth"])))
            else:
                if f in dets:  # 越程槽：形状与尺寸见局部放大图；紧靠轴肩时位置不必再注
                    sz1 = p.step_z(f["step"])[1]
                    if f["z0"] - sz0 > 1e-6 and sz1 - f["z1"] > 1e-6:
                        dims.append(Dim("front", "linear", "z", f["z0"] - sz0, f"{f['z0'] - sz0:g}", f"{f['name']}_pos",
                                        (sz0, f["z0"])))
                    continue
                dims.append(Dim("front", "callout", None, None, f["callout"], f["name"], ((f["z0"] + f["z1"]) / 2, f["d"] / 2)))
                if f["z0"] - sz0 > 1e-6:
                    dims.append(Dim("front", "linear", "z", f["z0"] - sz0, f"{f['z0'] - sz0:g}", f"{f['name']}_pos",
                                    (sz0, f["z0"])))
    top = [d for d in dims if d.view == "front" and d.kind == "linear" and d.axis == "z"
           and not d.ref.startswith(("step", "overall"))]
    for d in top:  # 横孔定位尺寸与上方其它尺寸重叠：放到轮廓与尺寸行之间的低一道
        if d.ref.startswith("XH") and any(o is not d and min(d.at) < max(o.at) - 1e-6 and min(o.at) < max(d.at) - 1e-6
                                          and not (o.extra or {}).get("track") for o in top):
            d.extra = {**(d.extra or {}), "track": 1}

    def _tspan(o):  # 同一行尺寸数字的轴向占位（模型 z）：放得下居中，放不下按整段
        a_, b_ = sorted(o.at)
        tw_ = text_width(o.text, 3.5) / scale
        return ((a_ + b_) / 2 - tw_ / 2, (a_ + b_) / 2 + tw_ / 2) if tw_ <= b_ - a_ - 1 / scale else (a_, b_)
    for d in top:  # 很短的定位尺寸：数字须移到界线外，而左右两侧都被同行数字占着 → 放到低一道
        a, b = sorted(d.at)
        tw = text_width(d.text, 3.5) / scale
        if (d.extra or {}).get("track") or tw <= b - a - 1 / scale:
            continue
        row = [_tspan(o) for o in top if o is not d and not (o.extra or {}).get("track")]
        row += [(z_ - 0.3 / scale, z_ + 0.3 / scale) for o in top if o is not d and not (o.extra or {}).get("track")
                for z_ in o.at]  # 尺寸界线
        row += [(shaft_dia_z(p, j_, scale) - 4.5 / scale, shaft_dia_z(p, j_, scale) + 4.5 / scale)
                for j_ in range(len(p.shaft_steps))
                if shaft_dia_overflow(p, j_, scale, _shaft_dia_label(p, j_)) == "up"]  # 向上伸出的直径线
        hit_r = any(lo < b + (1.5 + tw + 1) / scale and hi > b + 0.5 / scale for lo, hi in row)
        hit_l = any(lo < a - 0.5 / scale and hi > a - (1.5 + tw + 1) / scale for lo, hi in row)
        low = [o for o in top if (o.extra or {}).get("track") == 1]
        if hit_r and hit_l and not any(min(o.at) < b + (2 + tw) / scale and a - (2 + tw) / scale < max(o.at) for o in low):
            d.extra = {**(d.extra or {}), "track": 1}
    if spec.dimensions != "none":
        dims += _precision_dims(p, views, {"front"}, True)
        nrow = max((r for _, r in shaft_gtol_layout(p, scale).values()), default=0)
        if nrow:  # 框格排到第二/三行：视图整体下移让出上方空间
            for v in views:
                v.origin_on_sheet = (v.origin_on_sheet[0], v.origin_on_sheet[1] - GTOL_ROW_GAP * nrow)
        if any(d.kind == "roughness" and (d.extra or {}).get("on_frame") for d in dims):
            for v in views:  # 粗糙度标在框格上方：所有视图整体下移让出上方空间（尺寸都是模型坐标，不受影响）
                v.origin_on_sheet = (v.origin_on_sheet[0], v.origin_on_sheet[1] - 12.0)
    # 上下留白均匀：上方已按最紧排（留 GAP/2），下方空余一半让给上方。
    # 下沿外扩：主视图/端视图 32 mm（分段尺寸 + 总长 + 基准符号），断面/放大图 10 mm；与右侧标题栏/技术要求栏横向重叠的
    # 视图不得低于该栏顶部。
    n_notes = len(_notes_and_title(spec, p, sheet, scale, today)[0])
    col_x, col_top = W - 5 - 180, 5 + 56 + 14 + 5 * n_notes + 6 + 16
    free = min(v.origin_on_sheet[1] - v.size_mm[1] / 2 - (32 if v.type in ("front", "left") else 10)
               - ((col_top + 4) if v.origin_on_sheet[0] + v.size_mm[0] / 2 + 16 > col_x else MARGIN + 2) for v in views)
    m = round((free - GAP / 2) / 2, 1)
    if m > 2:
        for v in views:
            v.origin_on_sheet = (v.origin_on_sheet[0], v.origin_on_sheet[1] - m)
    for lh in p.heat.get("local", []):  # 局部淬火：主视图轮廓下方 1.2 mm 的粗点画线（GB/T 4457.4），两端让开 0.5 mm
        r_ = p.shaft_steps[lh["step"]][1] / 2
        y_ = front.origin_on_sheet[1] - r_ * scale - 1.2
        xa_, xb_ = shaft_sx(front, Ltot, lh["z0"]) + 0.5, shaft_sx(front, Ltot, lh["z1"]) - 0.5
        dims.append(Dim("front", "chain", None, None, "", f"heat:{lh['step']}", None,
                        {"sheet_pts": [(round(xa_, 2), round(y_, 2)), (round(xb_, 2), round(y_, 2))]}))
    for v in views:  # 局部放大图：视图位置已定后算主视图圆圈/编号与放大图内尺寸的图纸坐标
        if v.type == "detail":
            dims += _detail_plan(p, front, v, Ltot, spec.dimensions != "none", dims)
    if p.flange_holes and spec.dimensions != "none":
        dims += _flange_hole_dims(p, next(v for v in views if v.type == "left"), D)
    if hub_kws and spec.dimensions != "none":  # 视图位置已定（含上方让位下移）后再算端视图尺寸的图纸坐标
        lv = next(v for v in views if v.type == "left")
        ex_, ey_ = lv.origin_on_sheet
        for b in hub_kws:
            k = b["kw"]
            hk = hub_keyway_dims("left", ex_, ey_, scale, b["d"] / 2, k["width"],
                                 k["top"], k["name"], lambda t: t if spec.fit_deviations else t.split("(")[0])
            dims += hk
            for g in dims:  # 轮毂键槽对称度：箭头从右侧与槽宽尺寸线对齐，框格在端视图外圆右侧
                if g.kind == "gtol" and (g.extra or {}).get("keyway") == k["name"]:
                    g.extra = {**g.extra, **hub_keyway_symmetry_extra(hk, ex_, ey_, D / 2 * scale)}
            # 孔径：左下 45° 引线，箭头在孔壁上（同齿轮轴孔）
            kb = p.shaft_bores.index(b)
            rb, kk = b["d"] / 2, 0.7071
            anchor = (ex_ - rb * scale * kk, ey_ - rb * scale * kk)
            rr = D / 2 * scale + 6
            elbow = (ex_ - rr * kk, ey_ - rr * kk)
            text = _dia_text(b["d"], b["tol"], spec.fit_deviations)
            tw = text_width(text, 3.5)
            dims.append(Dim("left", "callout", None, b["d"], text, f"bore{kb}_dia", None, {"label": {
                "anchor": (round(anchor[0], 2), round(anchor[1], 2)), "elbow": (round(elbow[0], 2), round(elbow[1], 2)),
                "end": (round(elbow[0] - tw - 1, 2), round(elbow[1], 2)),
                "text_at": (round(elbow[0] - tw - 0.5, 2), round(elbow[1] + 0.8, 2)), "on_edge": True, "dir": -1}}))
    if not (p.shaft_bores or p.flange_holes):  # 实心轴竖直横孔：主视图局部剖（剖到轴线平面）
        f0 = views[0]
        for h in p.cross_holes:
            if h["dir"] != "vertical":
                continue
            za, zb, y0, y1 = cross_hole_zone(p, h, scale)
            xa, xb = shaft_sx(f0, Ltot, za), shaft_sx(f0, Ltot, zb)
            cy_ = f0.origin_on_sheet[1]
            ya, yb = cy_ + (y0 - (1 if h["through"] else 0)) * scale, cy_ + (y1 + 1) * scale
            f0.local_sections.append({"name": h["name"], "zone": (za, zb, y0, y1), "depth": round(h["D"] / 2, 3),
                                      "boundary": [(round(xa, 2), round(ya, 2)), (round(xb, 2), round(ya, 2)),
                                                   (round(xb, 2), round(yb, 2)), (round(xa, 2), round(yb, 2))]})
    if p.shaft_bores or p.flange_holes:  # 空心轴/套类/盘盖：主视图过轴线原位全剖（GB/T 4458.6；剖切面过对称面且按投影配置，不标注）
        f0 = views[0]
        cx_, cy_ = f0.origin_on_sheet
        w_, h_ = f0.size_mm
        f0.full_section = {"boundary": [(round(cx_ - w_ / 2 - 3, 2), round(cy_ - h_ / 2 - 3, 2)),
                                        (round(cx_ + w_ / 2 + 3, 2), round(cy_ - h_ / 2 - 3, 2)),
                                        (round(cx_ + w_ / 2 + 3, 2), round(cy_ + h_ / 2 + 3, 2)),
                                        (round(cx_ - w_ / 2 - 3, 2), round(cy_ + h_ / 2 + 3, 2))],
                           "depth": round(D / 2, 3), "cut_points": [[0.0, 0.0, 0.0], [0.0, 0.0, round(Ltot, 3)]]}
    notes, tb = _notes_and_title(spec, p, sheet, scale, today)
    plan = DrawingPlan(p.name, "part", sheet, SHEETS[sheet], scale, spec.projection, views, dims, notes, tb,
                       layout="shaft", revisions=list(spec.revisions))
    precision_sheet(plan, p)
    if spec.dimensions != "none":
        shaft_dim_sheet(plan, p)
    if p.gear:  # 齿轮轴：参数表（右上角）
        from .gear_drawing import param_table
        plan.gear_table = param_table(plan, p)
    return plan

def _fit_iso_beside_column(plan: "DrawingPlan") -> None:
    """装配图轴测图伸进右侧栏（标题栏 + 技术要求 + 明细表）：轴测图是辅助视图，保持左上角单独缩小比例
    （不为它把整张图缩小），最小到原来的 0.4。"""
    W, _ = plan.sheet_size
    top = 5 + 56 + 12 + 5 * len(plan.notes) + (7 * (len(plan.bom) + 1) if plan.bom else 0) + 3
    col = (W - 5 - 180 - 3, 0.0, float(W), top)
    for v in plan.views:
        if v.type != "iso":
            continue
        cx, cy = v.origin_on_sheet
        w, h = v.size_mm
        x0, y1 = cx - w / 2, cy + h / 2
        for f in (1.0, 0.9, 0.8, 0.7, 0.6, 0.5, 0.4):
            if _box_hit((x0, y1 - h * f, x0 + w * f, y1), col) <= 0:
                if f < 1.0:
                    v.scale = round(v.scale * f, 4)
                    v.size_mm = (round(w * f, 2), round(h * f, 2))
                    v.origin_on_sheet = (round(x0 + w * f / 2, 2), round(y1 - h * f / 2, 2))
                break


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
    # 视图组上下居中（先定视图位置，序号气泡与安装尺寸再按新位置排）
    probe = DrawingPlan(a.name, "assembly", sheet, SHEETS[sheet], scale, spec.projection, views, [],
                        ["技术要求："] + ["x"] * (len(asm_notes(a)) + 1 + len(spec.notes)), {}, bom=bom if spec.bom_table else [])
    _balance_views(probe)
    _fit_iso_beside_column(probe)
    balloons: list[dict[str, Any]] = []
    fdims = fit_dims(a, views, None, SHEETS[sheet]) if spec.dimensions != "none" else []
    avoid = []
    for d in fdims:  # 配合尺寸先排，序号气泡让开：文字框 + 尺寸线（细长框）
        ex = d.extra
        (ax_, ay_), (bx_, by_) = ex["sheet_pts"]
        px_, py_ = ex["sheet_pos"]
        tw_ = text_width(d.text, 3.5)
        if ex["orient"] == "h":
            avoid += [(px_ - 1, py_ - 1, px_ + tw_ + 1, py_ + 4.5),
                      (min(ax_, px_), ay_ - 0.3, max(bx_, px_ + tw_), ay_ + 0.3)]
        else:
            avoid += [(px_ - 4.5, py_ - 1, px_ + 1, py_ + tw_ + 1),
                      (ax_ - 0.3, min(ay_, py_), ax_ + 0.3, max(by_, py_ + tw_))]
    if spec.balloons and bom:
        if spec.dimensions != "none" and views[0].type in VIEW_AXES:  # 总体尺寸（视图左侧竖直、下方水平，见 overall_dim_sheet）
            ha_, va_ = VIEW_AXES[views[0].type]
            (ul, uh), (wl, wh) = vrange(a.bbox, ha_), vrange(a.bbox, va_)
            (X0, Y0), (X1, Y1) = _uv_sheet(views[0], a.bbox, (ul, wl)), _uv_sheet(views[0], a.bbox, (uh, wh))
            avoid += [(X0 - 16, min(Y0, Y1), X0 - 4, max(Y0, Y1)), (min(X0, X1), Y0 - 16, max(X0, X1), Y0 - 4)]
        bom, balloons = layout_balloons(bom, a, views[0], SHEETS[sheet], avoid)
    if not spec.bom_table:
        bom = []
    base_notes = asm_notes(a)
    inst_dims, inst_note = install_dims(a, views) if spec.dimensions != "none" else ([], None)
    dims += inst_dims
    dims += fdims
    if inst_note:
        base_notes.append(inst_note)
    notes = ["技术要求："] + [f"{i}. {t}" for i, t in enumerate(base_notes, 1)]
    notes += [f"{i}. {t}" for i, t in enumerate(spec.notes, len(notes))]
    tb = {"name": a.title, "part_no": a.part_no, "material": "", "scale": scale_label(scale),
          "mass": f"{a.mass_kg:.2f} kg", "sheet": sheet,
          "projection": "第一角" if spec.projection == "first_angle" else "第三角",
          "designer": "", "checker": "", "approver": "", "date": (today or date.today()).isoformat()}
    tb.update(spec.title_block)
    plan = DrawingPlan(a.name, "assembly", sheet, SHEETS[sheet], scale, spec.projection, views, dims, notes, tb,
                       bom=bom, balloons=balloons, revisions=list(spec.revisions))
    plan.asm_section = plan_asm_section(a, views)
    plan.axes = asm_axes(a, views)
    overall_dim_sheet(plan, a)
    _push_overall_past_fits(plan)
    return plan


def _fit_text_box(d: "Dim") -> tuple[float, float, float, float]:
    px_, py_ = d.extra["sheet_pos"]
    tw_ = text_width(d.text, 3.5)
    return (px_ - 1, py_ - 1, px_ + tw_ + 1, py_ + 4.5) if d.extra["orient"] == "h" else \
        (px_ - 4.5, py_ - 1, px_ + 1, py_ + tw_ + 1)


def _push_overall_past_fits(plan: "DrawingPlan") -> None:
    """配合尺寸文字落在总体尺寸带上（薄件小比例时，向左/向下伸出的配合代号）→ 该总体尺寸整体外移到配合文字之外
    （extra["pushed"]，预览与 CAD 都按 sheet_pos 画）。外移后出图框则不动。"""
    W, H = plan.sheet_size
    for v in plan.views:
        fits = [_fit_text_box(d) for d in plan.dims if d.view == v.type and (d.extra or {}).get("fit")]
        if not fits:
            continue
        for d in plan.dims:
            ex = d.extra or {}
            if d.view != v.type or d.kind != "linear" or d.at is not None or "sheet_pos" not in ex \
                    or ex.get("fit") or ex.get("install") or ex.get("pitch"):
                continue
            (ax_, ay_), (bx_, by_) = ex["sheet_pts"]
            px_, py_ = ex["sheet_pos"]
            if ex["orient"] == "v":
                band = (px_ - 5, min(ay_, by_), px_ + 1.5, max(ay_, by_))
                hits = [b for b in fits if _box_hit(b, band) > 0]
                nx = min((b[0] for b in hits), default=px_) - 2
                if hits and nx - 5 > MARGIN + 2:
                    d.extra = {**ex, "sheet_pos": (round(nx, 2), py_), "pushed": True}
            else:
                band = (min(ax_, bx_), py_ - 1.5, max(ax_, bx_), py_ + 5)
                hits = [b for b in fits if _box_hit(b, band) > 0]
                ny = min((b[1] for b in hits), default=py_) - 6
                if hits and ny - 2 > MARGIN + 2:
                    d.extra = {**ex, "sheet_pos": (px_, round(ny, 2)), "pushed": True}


def asm_notes(a: ResolvedAssembly) -> list[str]:
    """装配图技术要求（按组件内容生成，不含序号）：清洗去毛刺；有螺钉 → 拧紧与防松；有滚动轴承 → 润滑与转动灵活；
    有配合 → 配合面清洗涂油、不得强行敲击。"""
    from .ir.assembly_resolve import assembly_fits
    srcs = [i.source for i in a.instances] + [s_ for s_, _, _ in a.extras]
    out = ["装配前零件清洗去毛刺，不得有磕碰、划伤和锈蚀。"]
    if any(s_.threads or s_.title.find("螺钉") >= 0 or s_.title.find("螺栓") >= 0 for s_ in srcs):
        out.append("螺钉按规定扭矩拧紧，重要连接涂螺纹胶防松。")
    if any(s_.kind in ("standard", "purchased") and s_.revolve and any(sg[3] > 0 for sg in s_.revolve)
           or "轴承" in s_.title for s_ in srcs):
        out.append("滚动轴承装配时加注锂基润滑脂（约占内部空间 1/3），装配后用手转动应灵活、无卡滞。")
    if assembly_fits(a):
        out.append("配合面装配前清洗并涂润滑油，不得强行敲击装入。")
    return out


BOM_COLS = [("序号", 12), ("代号", 44), ("名称", 54), ("数量", 14), ("材料", 56)]  # GB/T 10609.2 明细栏（简化）


def bom_display(r: dict[str, Any]) -> list[Any]:
    """明细栏一行（序号、代号、名称、数量、材料）。标准件按 GB/T 10609.2：代号栏填标准号，名称栏填名称+规格，
    材料栏填性能等级（螺钉 8.8 级等）；其余零件代号 = 图号，材料栏无材料时填规格。"""
    if r.get("standard"):
        grade = (r.get("properties") or {}).get("等级", "")
        return [r["item"], r["standard"], r["title"], r["qty"], f"{grade}级" if grade else ""]
    return [r["item"], r["part_no"], r["title"], r["qty"], r.get("material") or r.get("spec", "")]


def asm_axes(a: ResolvedAssembly, views: list[ViewPlan], ext: float = 3.0) -> list[dict[str, Any]]:
    """装配图各正投影视图中回转件（轴、轴承、齿轮、螺钉）的轴线（细点画线，伸出轮廓 ext mm）；
    轴线朝向观察者的视图画十字中心线（长度 = 外圆 + ext）。同一直线上相交/相接的轴线合并成一条。"""
    from .ir.assembly_resolve import apply
    out = []
    for v in views:
        if v.type not in VIEW_AXES:
            continue
        ha, va = VIEW_AXES[v.type]
        dep = next(c for c in "xyz" if c not in (ha[-1], va[-1]))

        def S(q):
            return _uv_sheet(v, a.bbox, (vc(q, ha), vc(q, va)))
        segs = []  # (方向 'h'|'v', 常坐标, lo, hi)
        for inst in a.instances:
            rev = inst.source.revolve
            if not rev:
                continue
            zs = [z for sg in rev for z in sg[:2]]
            R = max(sg[2] for sg in rev) / 2
            p0, p1 = apply(inst.transform, (0.0, 0.0, min(zs))), apply(inst.transform, (0.0, 0.0, max(zs)))
            a0, a1 = S(p0), S(p1)
            if math.hypot(a1[0] - a0[0], a1[1] - a0[1]) < 1e-6:  # 端视：十字中心线
                if abs(p1["xyz".index(dep)] - p0["xyz".index(dep)]) < 1e-9:
                    continue
                r = R * v.scale + ext
                segs += [("h", a0[1], a0[0] - r, a0[0] + r), ("v", a0[0], a0[1] - r, a0[1] + r)]
            elif abs(a0[1] - a1[1]) < 1e-6:
                segs.append(("h", a0[1], min(a0[0], a1[0]) - ext, max(a0[0], a1[0]) + ext))
            elif abs(a0[0] - a1[0]) < 1e-6:
                segs.append(("v", a0[0], min(a0[1], a1[1]) - ext, max(a0[1], a1[1]) + ext))
        segs.sort()
        merged: list[list[Any]] = []
        for o, c, lo, hi in segs:
            if merged and merged[-1][0] == o and abs(merged[-1][1] - c) < 1e-3 and lo <= merged[-1][3] + 1e-6:
                merged[-1][3] = max(merged[-1][3], hi)
            else:
                merged.append([o, c, lo, hi])
        for o, c, lo, hi in merged:
            pts = [(round(lo, 2), round(c, 2)), (round(hi, 2), round(c, 2))] if o == "h" else \
                [(round(c, 2), round(lo, 2)), (round(c, 2), round(hi, 2))]
            out.append({"view": v.type, "pts": pts})
    return out


def plan_asm_section(a: ResolvedAssembly, views: list[ViewPlan]) -> dict[str, Any]:
    """装配图主视图沿主轴线全剖（GB/T 4458.6）：有回转件（轴、轴承）且其轴线平行于主视图时，剖切面取过该轴线、
    平行于主视图的平面。实心轴与标准件（螺钉、销）纵向剖切按不剖绘制（exclude）；被剖零件剖面线方向/间隔相邻错开。
    剖切面过装配体对称面时省略标注，否则在俯视图上画剖切符号 A。"""
    from .ir.assembly_resolve import apply
    front = next((v for v in views if v.type == "front"), None)
    if front is None:
        return {}
    axis_pt = None
    for inst in a.instances:
        if not inst.source.revolve or inst.source.threads:  # 螺钉不决定剖切面
            continue
        o, z1 = apply(inst.transform, (0.0, 0.0, 0.0)), apply(inst.transform, (0.0, 0.0, 1.0))
        if abs(z1[1] - o[1]) < 1e-6:  # 轴线垂直于 Y（主视图视向）→ 平行于主视图
            axis_pt = o
            break
    if axis_pt is None:
        return {}
    yc = axis_pt[1]
    cut = [i for i in a.instances if i.world_bbox.ymin < yc - 1e-6 < yc + 1e-6 < i.world_bbox.ymax]
    exclude = [i.id for i in cut if i.source.kind == "standard" and not any(sg[3] > 0 for sg in i.source.revolve)
               or (i.source.revolve and all(sg[3] <= 0 for sg in i.source.revolve))]
    bearings = [i.id for i in cut if i.id not in exclude and i.source.kind in ("standard", "purchased")
                and i.source.revolve and any(sg[3] > 0 for sg in i.source.revolve)]
    hatch = {}
    for k, i in enumerate(x for x in cut if x.id not in exclude and x.id not in bearings):
        hatch[i.id] = (45 if k % 2 == 0 else 135, (2.5, 2.5, 3.5, 3.5)[k % 4])
    sym = abs(yc - (a.bbox.ymin + a.bbox.ymax) / 2) < 1e-6
    out = {"view": "front", "parent": "top", "axis": "y", "plane": yc, "exclude": exclude, "hatch": hatch,
           "bearings": bearings,
           "label": "" if sym else "A"}
    top = next((v for v in views if v.type == "top"), None)
    if top is not None:
        p0 = _uv_sheet(top, a.bbox, (a.bbox.xmin - 4, yc))
        p1 = _uv_sheet(top, a.bbox, (a.bbox.xmax + 4, yc))
        out["cut_line"] = [(round(p0[0], 2), round(p0[1], 2)), (round(p1[0], 2), round(p1[1], 2))]
    return out


def _rect_minus(base, voids):
    """矩形 base 减去若干矩形 voids → 网格单元（x0, y0, x1, y1）列表与边界线段（实体/空交界）。"""
    xs = sorted({base[0], base[2], *[min(max(v[k], base[0]), base[2]) for v in voids for k in (0, 2)]})
    ys = sorted({base[1], base[3], *[min(max(v[k], base[1]), base[3]) for v in voids for k in (1, 3)]})

    def solid(i, j):
        if i < 0 or j < 0 or i >= len(xs) - 1 or j >= len(ys) - 1:
            return False
        cx, cy = (xs[i] + xs[i + 1]) / 2, (ys[j] + ys[j + 1]) / 2
        return not any(v[0] < cx < v[2] and v[1] < cy < v[3] for v in voids)
    cells, edges = [], []
    for i in range(len(xs) - 1):
        for j in range(len(ys) - 1):
            if xs[i + 1] - xs[i] < 1e-6 or ys[j + 1] - ys[j] < 1e-6 or not solid(i, j):
                continue
            cells.append((xs[i], ys[j], xs[i + 1], ys[j + 1]))
            if not solid(i - 1, j):
                edges.append(((xs[i], ys[j]), (xs[i], ys[j + 1])))
            if not solid(i + 1, j):
                edges.append(((xs[i + 1], ys[j]), (xs[i + 1], ys[j + 1])))
            if not solid(i, j - 1):
                edges.append(((xs[i], ys[j]), (xs[i + 1], ys[j])))
            if not solid(i, j + 1):
                edges.append(((xs[i], ys[j + 1]), (xs[i + 1], ys[j + 1])))
    return cells, edges


def _radial_in_plane(inst) -> tuple[float, float]:
    """回转件零件坐标中、装配后与 Y（主视图视向）垂直的径向（取零件 X 或 Y）。"""
    from .ir.assembly_resolve import apply
    o, xt = apply(inst.transform, (0.0, 0.0, 0.0)), apply(inst.transform, (1.0, 0.0, 0.0))
    return (1.0, 0.0) if abs(xt[1] - o[1]) < 1e-6 else (0.0, 1.0)


def gear_pitch_lines(inst, S, ext: float = 2.5) -> list[tuple[tuple[float, float], tuple[float, float]]]:
    """齿轮段分度线（GB/T 4459.2 细点画线，两端伸出轮廓约 2.5 mm）：轴线平行于图面的视图中、轴线两侧各一条（图纸坐标）。
    S 为装配坐标 → 图纸坐标的投影；端视（轴线朝向观察者）返回空。"""
    from .ir.assembly_resolve import apply
    out = []
    if not inst.source.gears:
        return out
    o, zt = apply(inst.transform, (0.0, 0.0, 0.0)), apply(inst.transform, (0.0, 0.0, 1.0))
    if abs(zt[1] - o[1]) > 1e-6:
        return out
    rad = _radial_in_plane(inst)
    for g in inst.source.gears:
        r = g["d"] / 2
        for sg in (1, -1):
            a = S(apply(inst.transform, (rad[0] * r * sg, rad[1] * r * sg, g["z0"])))
            b = S(apply(inst.transform, (rad[0] * r * sg, rad[1] * r * sg, g["z1"])))
            L = ((b[0] - a[0]) ** 2 + (b[1] - a[1]) ** 2) ** 0.5 or 1.0
            ux, uy = (b[0] - a[0]) / L, (b[1] - a[1]) / L
            out.append(((a[0] - ux * ext, a[1] - uy * ext), (b[0] + ux * ext, b[1] + uy * ext)))
    return out


def asm_section_regions(a: ResolvedAssembly, view: ViewPlan, sec: dict[str, Any]) -> list[dict[str, Any]]:
    """全剖主视图中各零件的图纸几何：{id, state(cut|excluded|behind), cells, edges, rects}。
    cut：剖面区域单元（回转件 = 轴线两侧的环带；其它 = 包围盒减去剖切面切到的孔）与边界；
    excluded：不剖的轴/标准件，按段画外形；behind：剖切面后方的零件，按包围盒（由渲染按剖面区域消隐）。"""
    from .ir.assembly_resolve import apply, solid_boxes
    ha, va = VIEW_AXES[view.type]
    yc = sec["plane"]

    def S(q):
        return _uv_sheet(view, a.bbox, (vc(q, ha), vc(q, va)))

    def rect(p0, p1):
        (x0, y0), (x1, y1) = S(p0), S(p1)
        return (min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1))
    out = []
    for inst in a.instances:
        b = inst.world_bbox
        if b.ymax <= yc + 1e-6 and b.ymin < yc - 1e-6:
            continue  # 剖切面之前：移去
        if b.ymin >= yc - 1e-6 and not (b.ymin < yc < b.ymax):
            out.append({"id": inst.id, "state": "behind", "rects": [rect((b.xmin, b.ymin, b.zmin), (b.xmax, b.ymin, b.zmax))]})
            continue
        if inst.id in sec["exclude"]:
            rects = [rect((bb.xmin, yc, bb.zmin), (bb.xmax, yc, bb.zmax)) for bb, _ in solid_boxes(inst)]
            out.append({"id": inst.id, "state": "excluded", "rects": rects, "chain": gear_pitch_lines(inst, S)})
            continue
        cells, edges, thin, thick, cones, plain = [], [], [], [], [], []
        if inst.source.revolve:
            rad = _radial_in_plane(inst)
            for z0, z1, do, di in inst.source.revolve:
                ro, ri = do / 2, di / 2
                g = next((g for g in inst.source.gears if g["z0"] < z1 - 1e-6 and g["z1"] > z0 + 1e-6
                          and abs(g["da"] - do) < 1e-6), None)
                rf = g["df"] / 2 if g else ro
                for s0, s1 in ((-rf, -ri), (ri, rf)):  # 剖面区（齿轮：轮齿按不剖，剖面线只到齿根圆）
                    pa = apply(inst.transform, (rad[0] * s0, rad[1] * s0, z0))
                    pb = apply(inst.transform, (rad[0] * s1, rad[1] * s1, z1))
                    c_, e_ = _rect_minus(rect(pa, pb), [])
                    cells += c_
                    edges += e_
                if g:
                    for s0, s1 in ((-ro, -rf), (rf, ro)):  # 轮齿：只画轮廓（齿顶线、齿根线粗实线）
                        pa = apply(inst.transform, (rad[0] * s0, rad[1] * s0, z0))
                        pb = apply(inst.transform, (rad[0] * s1, rad[1] * s1, z1))
                        c_, e_ = _rect_minus(rect(pa, pb), [])
                        plain += c_
                        edges += e_
        else:
            voids, thin, thick, cones = [], [], [], []

            def band(q0, q1, d):
                """轴线 q0→q1（平行于剖切面、沿坐标轴）、直径 d 的圆柱被剖切面切出的矩形；切不到为 None。"""
                e = abs(q0[1] - yc)
                if abs(q1[1] - q0[1]) > 1e-6 or e >= d / 2:
                    return None
                hw = (d * d / 4 - e * e) ** 0.5
                ux = 1.0 if abs(q1[0] - q0[0]) < 1e-9 else 0.0  # 孔轴线竖直（沿 z）→ 孔宽沿 x
                lo = (min(q0[0], q1[0]) - hw * ux, yc, min(q0[2], q1[2]) - hw * (1 - ux))
                hi = (max(q0[0], q1[0]) + hw * ux, yc, max(q0[2], q1[2]) + hw * (1 - ux))
                return rect(lo, hi), hw, ux
            for f in inst.source.fits:
                if f["role"] not in ("hole", "tap", "bore"):
                    continue
                q0, q1 = apply(inst.transform, f["p0"]), apply(inst.transform, f["p1"])
                if f["role"] == "tap":  # 内螺纹（GB/T 4459.1）：小径（钻孔）挖空、剖面线画到小径；大径细实线；终止线粗实线；钻孔锥角 118°
                    q2 = apply(inst.transform, f["p2"])
                    r_ = band(q0, q2, f["drill"])
                    if r_ is None:
                        continue
                    voids.append(r_[0])
                    bm = band(q0, q1, f["d"])
                    ux = r_[2]
                    if bm is not None:
                        x0, y0, x1, y1 = bm[0]
                        if ux:  # 竖直孔：大径线竖直
                            thin += [((x0, y0), (x0, y1)), ((x1, y0), (x1, y1))]
                        else:
                            thin += [((x0, y0), (x1, y0)), ((x0, y1), (x1, y1))]
                        if not f["through"]:
                            pe = S(q1)
                            thick.append(((x0, pe[1]), (x1, pe[1])) if ux else ((pe[0], y0), (pe[0], y1)))
                    if not f["through"]:  # 锥底：底孔端点外伸 0.3×钻孔径（≈118° 锥角）
                        u = [q2[k] - q0[k] for k in range(3)]
                        L = sum(c * c for c in u) ** 0.5 or 1.0
                        tip = tuple(q2[k] + u[k] / L * 0.3 * f["drill"] for k in range(3))
                        x0, y0, x1, y1 = r_[0]
                        pe, pt = S(q2), S(tip)
                        base = ((x0, pe[1]), (x1, pe[1])) if ux else ((pe[0], y0), (pe[0], y1))
                        cones.append([base[0], base[1], pt])
                    continue
                r_ = band(q0, q1, f["d"])
                if r_ is not None:
                    voids.append(r_[0])
                if f["role"] == "bore" and f.get("cbore"):
                    D, dep = f["cbore"]
                    u = [q1[k] - q0[k] for k in range(3)]
                    L = sum(c * c for c in u) ** 0.5 or 1.0
                    rc = band(q0, tuple(q0[k] + u[k] / L * dep for k in range(3)), D)
                    if rc is not None:
                        voids.append(rc[0])
            cells, edges = _rect_minus(rect((b.xmin, yc, b.zmin), (b.xmax, yc, b.zmax)), voids)

            def on_base(e_):  # 锥底处没有轮廓线（钻孔圆柱直接过渡到锥面）
                for c_ in cones:
                    (ax_, ay_), (bx_, by_) = c_[0], c_[1]
                    if all(min(ax_, bx_) - 1e-6 <= q[0] <= max(ax_, bx_) + 1e-6 and
                           min(ay_, by_) - 1e-6 <= q[1] <= max(ay_, by_) + 1e-6 for q in e_):
                        return True
                return False
            edges = [e_ for e_ in edges if not on_base(e_)]
        if inst.id in sec.get("bearings", []):  # 滚动轴承通用画法（GB/T 4459.7）：矩形线框 + 中央正十字，不画剖面线
            crosses = []
            for x0, y0, x1, y1 in cells:
                cx_, cy_, h_ = (x0 + x1) / 2, (y0 + y1) / 2, min(x1 - x0, y1 - y0) / 3
                crosses += [((cx_ - h_, cy_), (cx_ + h_, cy_)), ((cx_, cy_ - h_), (cx_, cy_ + h_))]
            out.append({"id": inst.id, "state": "bearing", "cells": cells, "edges": edges, "crosses": crosses})
            continue
        out.append({"id": inst.id, "state": "cut", "cells": cells, "edges": edges, "hatch": sec["hatch"].get(inst.id, (45, 2.5)),
                    "thin": thin, "thick": thick, "cones": cones, "plain": plain, "chain": gear_pitch_lines(inst, S)})
    return out


def fit_dims(a: ResolvedAssembly, views: list[ViewPlan], balloons: list[dict[str, Any]] | None = None,
             sheet_size: tuple[float, float] = (420.0, 297.0)) -> list[Dim]:
    """两种候选顺序（中点优先 / 两端优先）各排一遍，取总冲突少的（多个配合挤在同一段时两端优先才排得开）。"""
    best = None
    for ts in ((0.5, 0.2, 0.8, 0.35, 0.65, 0.1, 0.9), (0.15, 0.85, 0.5, 0.3, 0.7)):
        dims, cost = _fit_dims(a, views, balloons, sheet_size, ts)
        if best is None or cost < best[1]:
            best = (dims, cost)
    return best[0]


def _fit_dims(a, views, balloons, sheet_size, ts) -> tuple[list[Dim], int]:
    """配合尺寸（GB/T 4458.5，装配图注 Φd 孔公差带/轴公差带）：在轴线平行于图面的第一个视图上，
    于重叠段中点横跨直径画尺寸线（箭头抵两侧轮廓），尺寸线延伸出视图包络后写配合代号。"""
    from .ir.assembly_resolve import assembly_fits
    out: list[Dim] = []
    obst = [(b_["pos"][0] - 5, b_["pos"][1] - 5, b_["pos"][0] + 5, b_["pos"][1] + 5) for b_ in (balloons or []) if "pos" in b_]
    total = 0
    segs: list[tuple[tuple[float, float], tuple[float, float]]] = []  # 已排配合尺寸的尺寸线
    texts: list[tuple[float, float, float, float]] = []

    def seg_box(a_, b_, bx):  # 线段与矩形是否相交（取样）
        return any(bx[0] <= a_[0] + (b_[0] - a_[0]) * k_ / 40 <= bx[2] and bx[1] <= a_[1] + (b_[1] - a_[1]) * k_ / 40 <= bx[3]
                   for k_ in range(41))
    all_fits = assembly_fits(a)
    for k, f in enumerate(all_fits):
        u = [f["p1"][i] - f["p0"][i] for i in range(3)]
        for v in views:
            if v.type not in VIEW_AXES:
                continue
            ha, va = VIEW_AXES[v.type]
            n_ax = ({"x", "y", "z"} - {ha[-1], va[-1]}).pop()
            if abs(u["xyz".index(n_ax)]) > 1e-6:
                continue  # 轴线不平行于图面
            p0 = _uv_sheet(v, a.bbox, (vc(f["p0"], ha), vc(f["p0"], va)))
            p1 = _uv_sheet(v, a.bbox, (vc(f["p1"], ha), vc(f["p1"], va)))
            r = f["d"] / 2 * v.scale
            cx, cy = v.origin_on_sheet
            vw, vh = v.size_mm
            vertical_axis = abs(p1[0] - p0[0]) < abs(p1[1] - p0[1])
            tw = text_width(f["text"], 3.5)
            best = None
            # 候选：沿重叠段取点 × 文字再外移；文字框不压已排的配合文字与序号气泡
            others = [(o.origin_on_sheet[0] - o.size_mm[0] / 2 - 2, o.origin_on_sheet[1] - o.size_mm[1] / 2 - 2,
                       o.origin_on_sheet[0] + o.size_mm[0] / 2 + 2, o.origin_on_sheet[1] + o.size_mm[1] / 2 + 2)
                      for o in views if o is not v]
            for o in views:  # 总体尺寸带：视图左侧（竖直）与下方（水平），见 overall_dim_sheet
                ox, oy = o.origin_on_sheet
                ow, oh = o.size_mm
                others.append((ox - ow / 2 - 16, oy - oh / 2, ox - ow / 2 - 2, oy + oh / 2))
                others.append((ox - ow / 2, oy - oh / 2 - 16, ox + ow / 2, oy - oh / 2 - 2))
            # 别的配合处（销、轴）的投影区：引出线横穿过去不好看（尺寸线应从就近一侧引出）
            parts_ = []
            for g in all_fits:
                if g is f or (g["inst_hole"], g["inst_shaft"]) == (f["inst_hole"], f["inst_shaft"]):
                    continue
                q0 = _uv_sheet(v, a.bbox, (vc(g["p0"], ha), vc(g["p0"], va)))
                q1 = _uv_sheet(v, a.bbox, (vc(g["p1"], ha), vc(g["p1"], va)))
                if (abs(q0[0] - p0[0]) < 1e-6 and abs(q1[0] - p1[0]) < 1e-6) or \
                        (abs(q0[1] - p0[1]) < 1e-6 and abs(q1[1] - p1[1]) < 1e-6):
                    continue  # 同轴（轴承内外圈）：不算
                rg = g["d"] / 2 * v.scale
                parts_.append((min(q0[0], q1[0]) - rg, min(q0[1], q1[1]) - rg, max(q0[0], q1[0]) + rg, max(q0[1], q1[1]) + rg))
            W_, H_ = sheet_size
            # 先试离配合处近的一侧（左半边的孔往左引，右半边往右），免得尺寸线横穿别的零件
            near = (p0[0] + p1[0]) / 2 < cx if vertical_axis else (p0[1] + p1[1]) / 2 < cy
            sides = (-1, 1) if near else (1, -1)
            cands = [(t_, push, side) for push in (0.0, 1.0, 2.0) for side in sides
                     for t_ in ts]
            for t_, push, side in cands:
                mx, my = p0[0] + (p1[0] - p0[0]) * t_, p0[1] + (p1[1] - p0[1]) * t_
                if vertical_axis:  # 尺寸线水平，伸出视图右侧（或左侧）写代号
                    pts = [(round(mx - r, 2), round(my, 2)), (round(mx + r, 2), round(my, 2))]
                    x_ = max(cx + vw / 2, mx + r) + 3 + push * 10 if side > 0 else min(cx - vw / 2, mx - r) - 3 - push * 10 - tw
                    pos = (round(x_, 2), round(my, 2))
                    box = (pos[0] - 1, my - 1, pos[0] + tw + 1, my + 4.5)
                    line = ((pts[0] if side > 0 else pts[1]), (pos[0] + tw, my) if side > 0 else (pos[0], my))
                    orient, axis = "h", ha
                else:  # 尺寸线竖直，伸出视图上方（或下方）
                    pts = [(round(mx, 2), round(my - r, 2)), (round(mx, 2), round(my + r, 2))]
                    y_ = max(cy + vh / 2, my + r) + 3 + push * 10 if side > 0 else min(cy - vh / 2, my - r) - 3 - push * 10 - tw
                    pos = (round(mx, 2), round(y_, 2))
                    box = (mx - 4.5, pos[1] - 1, mx + 1, pos[1] + tw + 1)
                    line = ((pts[0] if side > 0 else pts[1]), (mx, pos[1] + tw) if side > 0 else (mx, pos[1]))
                    orient, axis = "v", va
                out_ = box[0] < 7 or box[1] < 7 or box[2] > W_ - 7 or box[3] > H_ - 7
                hit = 10 * out_ + 3 * sum(_box_hit(box, o) > 0 for o in obst + texts + others) \
                    + 3 * sum(seg_box(*sg, box) for sg in segs) + 3 * sum(seg_box(*line, t__) for t__ in texts) \
                    + sum(seg_box(*line, t__) for t__ in obst + others) \
                    + 3 * sum(seg_box(*line, t__) for t__ in parts_)
                if best is None or hit < best[0]:
                    best = (hit, pts, pos, box, orient, axis, line, side)
                if hit == 0:
                    break
            total += best[0]
            _, pts, pos, box, orient, axis, line, side = best
            texts.append(box)
            segs.append(line)
            out.append(Dim(v.type, "linear", axis, f["d"], f["text"], f"fit:{f['inst_hole']}.{f['hole']}",
                           extra={"fit": True, "sheet_pts": pts, "sheet_pos": pos, "orient": orient, "side": side,
                                  "prefix": "Φ", "suffix": f["text"].split(f"{f['d']:g}", 1)[1],  # CAD：Φ<DIM>H7/k6
                                  "pair": [f["inst_hole"], f["inst_shaft"]]}))
            break
    return out, total


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


def layout_balloons(bom: list[dict[str, Any]], a, view, sheet_size, avoid: list | None = None
                    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
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
        void = None  # 空心回转件（轴承、齿轮、套）：内孔在图上的范围，引出点不落在孔里
        src_ = getattr(inst, "source", None)
        ri = max((sg[3] for sg in src_.revolve), default=0.0) / 2 if src_ is not None else 0.0
        if ri > 0:
            from .ir.assembly_resolve import apply
            zs = [z for sg in inst.source.revolve for z in sg[:2]]
            void = (_project(view, a, apply(inst.transform, (0.0, 0.0, min(zs)))),
                    _project(view, a, apply(inst.transform, (0.0, 0.0, max(zs)))), (ri + 0.5) * view.scale)

        def in_void(q):
            if void is None:
                return False
            (ax_, ay_), (bx_, by_), rr_ = void
            L = math.hypot(bx_ - ax_, by_ - ay_)
            if L < 1e-6:
                return math.hypot(q[0] - ax_, q[1] - ay_) < rr_
            return abs((q[0] - ax_) * (by_ - ay_) - (q[1] - ay_) * (bx_ - ax_)) / L < rr_
        for fu in (0.5, 0.3, 0.7, 0.15, 0.85):
            for fv in (0.5, 0.3, 0.7, 0.15, 0.85):
                q = (x0 + (x1 - x0) * fu, y0 + (y1 - y0) * fv)
                cover = sum(1 for k, rr in rects.items() if k != inst.id and area[k] <= area[inst.id] * 1.5
                            and rr[0] - 1 <= q[0] <= rr[1] + 1 and rr[2] - 1 <= q[1] <= rr[3] + 1)
                near = min((math.hypot(q[0] - o[0], q[1] - o[1]) for o in chosen), default=99.0)
                edge = min(q[0] - (cx - w / 2), (cx + w / 2) - q[0], q[1] - (cy - h / 2), (cy + h / 2) - q[1])
                score = cover * 100 + in_void(q) * 300 + max(0.0, 8 - near) * 10 + edge * 0.05
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
        start0 = min(max(mid - span / 2 if side == "top" else mid + span / 2, lo_lim + (0 if side == "top" else span)),
                     hi_lim - (span if side == "top" else 0))

        def place(start):
            out_ = []
            for k, p in enumerate(group):
                if side == "top":
                    pos = (start + k * BALLOON_GAP, min(cy + h / 2 + BALLOON_OFF, H - 12))
                elif side == "right":
                    pos = (min(cx + w / 2 + BALLOON_OFF, W - 12), start - k * BALLOON_GAP)
                else:
                    pos = (max(cx - w / 2 - BALLOON_OFF, 12), start - span + k * BALLOON_GAP)
                out_.append((round(pos[0], 2), round(pos[1], 2)))
            return out_

        def clash(poss):  # 与配合尺寸文字/尺寸线（avoid）冲突：气泡压上，或引线穿过文字
            n_ = 0
            for p, q in zip(group, poss):
                bb = (q[0] - 5, q[1] - 5, q[0] + 5, q[1] + 5)
                for o in avoid or []:
                    n_ += _box_hit(bb, o) > 0
                    a_ = p["anchor"]
                    n_ += any(o[0] <= a_[0] + (q[0] - a_[0]) * t / 30 <= o[2] and o[1] <= a_[1] + (q[1] - a_[1]) * t / 30 <= o[3]
                              for t in range(31)) and (o[3] - o[1]) > 1 and (o[2] - o[0]) > 1
            return n_
        start, poss = start0, place(start0)
        if avoid:  # 整列平移让开配合尺寸
            for d_ in (6, -6, 12, -12, 18, -18, 24, -24):
                if not clash(poss):
                    break
                st = min(max(start0 + d_, lo_lim + (0 if side == "top" else span)), hi_lim - (span if side == "top" else 0))
                cand = place(st)
                if clash(cand) < clash(poss):
                    start, poss = st, cand
        for p, q in zip(group, poss):
            p["pos"] = q
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


_DEV_RE = re.compile(r"^(.*?)\(([+-]?\d+(?:\.\d+)?)/([+-]?\d+(?:\.\d+)?)\)(.*)$")
DEV_RATIO = 0.7  # 极限偏差字高比尺寸数字小一号（GB/T 4458.5）
DEV_GAP = 0.35  # 代号与偏差之间的间隙（× 字高）：字宽估算偏小时也不贴字


def split_deviation(s: str):
    """'Φ20k6(+0.015/+0.002) 通' → ('Φ20k6', '+0.015', '+0.002', ' 通')；无偏差返回 None。"""
    m = _DEV_RE.match(s)
    return m.groups() if m else None


def _plain_w(s: str, size: float) -> float:
    return sum(size if ord(ch) > 0x2E80 else 0.6 * size for ch in s)


def text_width(s: str, size: float) -> float:
    """文字宽度估算；带极限偏差的按上下叠写计（偏差小一号）。"""
    sp = split_deviation(s)
    if not sp:
        return _plain_w(s, size)
    main, up, lo, tail = sp
    return (_plain_w(main, size) + DEV_GAP * size + max(_plain_w(up, size * DEV_RATIO), _plain_w(lo, size * DEV_RATIO))
            + _plain_w(tail, size))


def _tw(s: str, size: float = LABEL_H) -> float:
    return text_width(s, size)


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
    for v in plan.views:  # 轴测图等非正投影视图：整块不放文字
        if v.type not in VIEW_AXES and v.type in ("iso", "flat_pattern"):
            cx, cy = v.origin_on_sheet
            w, h = v.size_mm
            hard.append((cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2))
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
        if ex.get("sheet_pos") and (ex.get("pitch") or ex.get("install") or ex.get("fit")):
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
                if d.kind == "datum":  # 三角 + 7 mm 引线 + 方框（沿法线）
                    bx_, by_ = x + nx * (7 + 3.5), y + ny * (7 + 3.5)
                    hard.append((min(x, bx_ - 4.5), min(y, by_ - 4.5), max(x, bx_ + 4.5), max(y, by_ + 4.5)))
                else:
                    hard.append((x - 4, y - 14, x + 26, y + 2))
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
        for k_, q in enumerate(sec.cut_points):
            x, y = _uv_sheet(pv, bbox, (vc(q, ha_), vc(q, va_)))
            r_ = 6 if k_ in (0, len(sec.cut_points) - 1) else 10  # 转折处：字母在斜角 4.5–6.5 mm 处
            hard.append((x - r_, y - r_, x + r_, y + r_))
        if sec.cut_points:  # 起止处箭头 + 字母（与 render_svg._draw_cut_line 同算法：沿投射方向 9 mm）
            from .section import AX, FRAME
            q0 = sec.cut_points[0]
            q1 = list(q0)
            q1[AX[FRAME[sec.view][1]]] += 1.0
            p0 = _uv_sheet(pv, bbox, (vc(q0, ha_), vc(q0, va_)))
            p1 = _uv_sheet(pv, bbox, (vc(tuple(q1), ha_), vc(tuple(q1), va_)))
            n_ = math.dist(p0, p1) or 1.0
            ddx, ddy = (p1[0] - p0[0]) / n_, (p1[1] - p0[1]) / n_
            for q in (sec.cut_points[0], sec.cut_points[-1]):
                x, y = _uv_sheet(pv, bbox, (vc(q, ha_), vc(q, va_)))
                tx_, ty_ = x + ddx * 9, y + ddy * 9 - 1.5
                hard.append((min(x, tx_ - 3) - 0.5, min(y, ty_ - 1.0) - 0.5, max(x, tx_ + 3) + 0.5, max(y, ty_ + 4.0) + 0.5))
    for d in plan.dims:  # 坐标值数字（ordinate_dim_sheet 已定位；靠近剖切符号时外移 8 mm，按外移后的位置占位）
        ex = d.extra or {}
        if d.kind != "ordinate" or "sheet_pos" not in ex or d.view not in vmap:
            continue
        px_, py_ = ex["sheet_pos"]
        tw_ = len(d.text) * 1.5 + 0.5
        o_ = 0.0
        for sec in plan.sections:
            pv = vmap.get(sec.parent)
            if pv is None or pv.type != d.view or not sec.cut_points:
                continue
            ha_, va_ = VIEW_AXES[pv.type]
            for q in (sec.cut_points[0], sec.cut_points[-1]):
                cx_, cy_ = _uv_sheet(pv, bbox, (vc(q, ha_), vc(q, va_)))
                if (ex["orient"] == "h" and abs(cx_ - px_) < 13) or (ex["orient"] == "v" and abs(cy_ - py_) < 13):
                    o_ = 8.0
        if ex["orient"] == "h":
            hard.append((px_ - 2.2, py_ - o_ - tw_ / 2 - 0.3, px_ + 0.5, py_ - o_ + tw_ / 2 + 0.3))  # 竖排文字以该点居中
        else:
            hard.append((px_ + o_ - 0.5, py_ - 1.5, px_ + o_ + tw_, py_ + 1.5))
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
        pk_ = next((pk for pk in (part.pockets if part is not None else []) if pk["name"] == d.ref), None)
        if d.kind == "callout" and pk_ is not None and d.at is not None and v.type in ("top", "bottom") \
                and pk_["face"] in ("top", "bottom") and "×R" not in d.text:  # 型腔：引出点可取任一边中点（注圆角的须指在圆弧上，不换）
            ha_, va_ = VIEW_AXES[v.type]
            ol = list(pk_["profile"].outline)
            # 边中点总在轮廓上（圆角型腔的角点不在），圆形等多点轮廓取均布的 8 点
            cand_ = [((ol[k][0] + ol[(k + 1) % len(ol)][0]) / 2, (ol[k][1] + ol[(k + 1) % len(ol)][1]) / 2)
                     for k in range(len(ol))] if len(ol) <= 8 else ol[::max(1, len(ol) // 8)]
            anchors += [(vc((x, y, 0.0), ha_), vc((x, y, 0.0), va_)) for x, y in cand_
                        if abs(vc((x, y, 0.0), ha_) - d.at[0]) > 1e-6 or abs(vc((x, y, 0.0), va_) - d.at[1]) > 1e-6]
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
            for sb in soft:  # 压视图：120；骑在视图轮廓线上（部分在内）更差：+600
                a_ = _box_hit(box, sb)
                if a_ > 0:
                    cost += 120 + (600 if a_ < (box[2] - box[0]) * (box[3] - box[1]) - 1e-6 else 0)
            cost += sum(400 for fb in feat if _box_hit(box, fb) > 0)
            cost += sum(300 for l1, l2 in leaders if _seg_cross((ax, ay), (ex, ey), l1, l2))
            cost += sum(300 for l1, l2 in leaders if _seg_box(l1, l2, box))  # 文字不压先放的引线
            sx_, sy_ = ax + 1.5 * math.cos(math.radians(ang)), ay + 1.5 * math.sin(math.radians(ang))  # 锚点本身可能落在别的框内
            cost += sum(300 for hb in hard if _seg_box((sx_, sy_), (ex, ey), hb))
            cost += sum(150 for fb in feat if _seg_box((sx_, sy_), (ex, ey), fb)  # 引线穿过别的孔/型腔
                        and not (fb[0] <= ax0 <= fb[2] and fb[1] <= ay0 <= fb[3]))
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
            ax, ay = shaft_sx(v, L, d.at[0]), cy + d.at[1] * v.scale
        elif d.view in VIEW_AXES:
            ax, ay = _uv_sheet(v, p.bbox, d.at)
        else:
            continue
        nx, ny = (d.extra or {}).get("normal", (0, 1))
        if plan.layout == "shaft" and d.kind in ("gtol", "roughness") and (d.kind == "gtol" or (d.extra or {}).get("on_frame")):
            R = max(dd for _, dd in p.shaft_steps) / 2
            lay = shaft_gtol_layout(p, v.scale)
            gk = d.ref[5:] if d.kind == "gtol" else (d.extra or {}).get("on_frame")
            row = cy + R * v.scale + GTOL_ROW + GTOL_ROW_GAP * lay.get(gk, (0, 0))[1]  # 框格行（与 render_shaft 一致）
            if d.kind == "gtol" and (d.extra or {}).get("shoulder") is not None:
                g = shoulder_gtol_geom(p, d.extra["shoulder"], v.scale)
                xf = shaft_sx(v, L, g["z"])
                xl = shaft_sx(v, L, g["z_leader"])
                ym = cy + g["r_at"] * v.scale
                d.extra = {**d.extra, "sheet_at": (round(xf, 2), round(ym, 2)), "sheet_pos": (round(xl, 2), round(row, 2)),
                           "elbow": {"x_face": round(xf, 2), "y": round(ym, 2), "x_leader": round(xl, 2), "dir": g["from"]}}
            elif d.kind == "gtol":
                d.extra = {**(d.extra or {}), "sheet_at": (round(ax, 2), round(ay, 2)), "sheet_pos": (round(ax, 2), round(row, 2))}
            else:  # 标在框格上方：√ 尖端在框格顶边、距左端 2.5
                zl = lay.get(gk, (d.at[0], 0))[0]
                at = (round(shaft_sx(v, L, zl) - 3.5 + 2.5, 2), round(row + 6.0, 2))
                d.extra = {**(d.extra or {}), "sheet_at": at, "sheet_pos": at}
            continue
        reach = {"datum": (d.extra or {}).get("reach", 7.0) + 3.0, "gtol": 12.0 if plan.layout != "shaft" else 18.0, "roughness": 0.0 if (nx, ny) == (0, 1) else (d.extra or {}).get("reach") or ra_reach(nx, d.text)}[d.kind]
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

def _shaft_dia_label(p: ResolvedPart, j: int, deviations: bool = True) -> str:
    th = next((f for f in p.shaft_features if f["kind"] == "thread" and f["step"] == j), None)
    if th:
        return th["callout"]
    tj = p.shaft_tolerances[j] if p.shaft_tolerances else None
    if p.gear and any(f["kind"] == "gear" and f["step"] == j for f in p.shaft_features):
        tj = p.gear["tip_tolerance"]
    return _dia_text(p.shaft_steps[j][1], tj, deviations and j not in getattr(p, "_dia_code_only", ()))


def _detail_plan(p: ResolvedPart, front: "ViewPlan", v: "ViewPlan", Ltot: float, with_dims: bool,
                 others: list[Dim] | None = None) -> list[Dim]:
    """局部放大图：主视图上细实线圆 + 引线罗马数字（引向未被轴肩占据的一侧上方）；放大图内注槽宽 b（槽底下方）
    与槽深 h（自由侧，箭头在外，数字在尺寸线延长线上）。"""
    f = next(x for x in p.shaft_features if x["name"] == v.target)
    dt = v.detail
    zc, yc, rm = dt["zc"], dt["yc"], dt["rm"]
    sc = front.scale
    cx, cy, r = shaft_sx(front, Ltot, zc), front.origin_on_sheet[1] + yc * sc, rm * sc
    sz0, sz1 = p.step_z(f["step"])
    i = f["step"]
    d_next = p.shaft_steps[i + 1][1] if i + 1 < len(p.shaft_steps) else 0.0
    shoulder_right = abs(f["z1"] - sz1) < 1e-6 and d_next > f["d"]
    sx = -1 if shoulder_right else 1  # 自由侧方向（−1 = 左）
    k = 0.7071
    # 标号避开主视图上方的竖线（键槽/环槽等定位尺寸的界线、轴肩线）：引线长 5/9/13 mm，自由侧优先
    vlines = [shaft_sx(front, Ltot, z) for d in (others or []) if d.view == "front" and d.kind == "linear"
              and d.axis == "z" and d.at is not None for z in d.at]
    tw_l = text_width(v.label, 3.5)
    best = None
    for n_, (sg, L_) in enumerate([(sx, 5), (sx, 9), (sx, 13), (-sx, 5), (-sx, 9)]):
        b_ = (cx + sg * (r + L_) * k, cy + (r + L_) * k)
        lx = b_[0] + sg * 1.2 + (0 if sg > 0 else -tw_l)
        hit = sum(1 for x in vlines if lx - 0.6 <= x <= lx + tw_l + 0.6)
        if best is None or (hit, n_) < best[0]:
            best = ((hit, n_), sg, b_, lx)
    _, sg, b, lx = best
    a = (cx + sg * r * k, cy + r * k)
    dt.update({"circle": (round(cx, 2), round(cy, 2), round(r, 2)), "leader": [(round(a[0], 2), round(a[1], 2)), (round(b[0], 2), round(b[1], 2))],
               "label_at": (round(lx, 2), round(b[1] - 1.2, 2)),
               "free_side": sx})
    if not with_dims:
        return []
    S = detail_sheet(v)
    h = (f["d"] - f["d2"]) / 2
    y2 = f["d2"] / 2
    (xa, ya), (xb, _) = S(f["z0"], y2), S(f["z1"], y2)
    ys = S(zc, yc)[1]
    out = [Dim("detail", "linear", "z", f["width"], f"{f['width']:g}", f"{f['name']}_w", None, {
        "detail": v.label, "sheet_pts": [(round(xa, 2), round(ya, 2)), (round(xb, 2), round(ya, 2))],
        "sheet_pos": (round((xa + xb) / 2, 2), round(ya - 7, 2)), "orient": "h", "below": True,
        "pick_pts": [(round(xa, 2), round((ya + ys) / 2, 2)), (round(xb, 2), round((ya + ys) / 2, 2))]})]
    zf = f["z0"] if sx < 0 else f["z1"]  # 自由侧槽壁
    xf = S(zf, y2)[0]
    xl = xf + sx * 6
    out.append(Dim("detail", "linear", "y", h, f"{h:g}", f"{f['name']}_h", None, {
        "detail": v.label, "sheet_pts": [(round(xf, 2), round(ya, 2)), (round(xf, 2), round(ys, 2))],
        "sheet_pos": (round(xl, 2), round(ys + 4 + text_width(f"{h:g}", 3.5) / 2, 2)), "orient": "v", "small": True,
        "pick_pts": [(round((xa + xb) / 2, 2), round(ya, 2)), (round(xf + sx * 3, 2), round(ys, 2))]}))
    return out


def flange_hole_sheet(v: "ViewPlan", x: float, y: float) -> tuple[float, float]:
    """左视图（从 z=0 端看，模型 −Y 朝上、+X 朝右）：模型 (x, y) → 图纸。"""
    ex, ey = v.origin_on_sheet
    return ex + x * v.scale, ey - y * v.scale


def _flange_hole_dims(p: ResolvedPart, lv: "ViewPlan", D: float) -> list[Dim]:
    """左视图：均布孔 n×Φd EQS（引线从最靠右上的孔引出，向右上）与分布圆直径（引线从右下方分布圆上引出，向右下）。"""
    out = []
    ex, ey = lv.origin_on_sheet
    sc = lv.scale
    R = D / 2 * sc
    for k, fh in enumerate(p.flange_holes):
        sp = [flange_hole_sheet(lv, *q) for q in fh["pts"]]
        hx, hy = max(sp, key=lambda q: (q[0] - ex) + (q[1] - ey))  # 最靠右上的孔
        up = 1 if k % 2 == 0 else -1  # 多组孔：交替上/下引出
        elbow = (ex + (R + 6) * 0.7071 + 4 * k, ey + up * ((R + 6) * 0.7071 + 4 * k))
        dx, dy = elbow[0] - hx, elbow[1] - hy
        n = math.hypot(dx, dy) or 1
        rh = fh["d"] / 2 * sc
        anchor = (hx + dx / n * rh, hy + dy / n * rh)
        tw = text_width(fh["callout"], 3.5)
        out.append(Dim("left", "callout", None, fh["d"], fh["callout"], fh["name"], None, {"label": {
            "anchor": (round(anchor[0], 2), round(anchor[1], 2)), "elbow": (round(elbow[0], 2), round(elbow[1], 2)),
            "end": (round(elbow[0] + tw + 1, 2), round(elbow[1], 2)),
            "text_at": (round(elbow[0] + 0.5, 2), round(elbow[1] + 0.8, 2)), "on_edge": True, "dir": 1}}))
        # 分布圆引线落点：图纸方向最接近右下 45°，且不落在孔上、不贴着水平/竖直中心线
        half_a = math.degrees(math.asin(min(1.0, (fh["d"] / 2 + 1.5) / (fh["pcd"] / 2))))
        hole_sheet_angs = [math.degrees(math.atan2(ey - flange_hole_sheet(lv, *q)[1] - 0, flange_hole_sheet(lv, *q)[0] - ex))
                           for q in fh["pts"]]
        hole_sheet_angs = [-a for a in hole_sheet_angs]  # 图纸角（y 向上）

        def bad(a):
            return any(abs((a - h + 180) % 360 - 180) < half_a for h in hole_sheet_angs) or \
                any(abs((a - ax + 180) % 360 - 180) < 6 for ax in (0, 90, 180, 270))
        cand = [a for a in range(0, 360, 5) if not bad(a)] or [315]
        a_best = min(cand, key=lambda a: abs((a - (-45 if up > 0 else 45) + 180) % 360 - 180))
        rp = fh["pcd"] / 2 * sc
        qa = (ex + rp * math.cos(math.radians(a_best)), ey + rp * math.sin(math.radians(a_best)))
        el2 = (ex + (R + 6) * 0.7071 + 4 * k, ey - up * ((R + 6) * 0.7071 + 4 * k))
        t2 = f"Φ{fh['pcd']:g}"
        tw2 = text_width(t2, 3.5)
        out.append(Dim("left", "callout", None, fh["pcd"], t2, f"{fh['name']}.pcd", None, {"label": {
            "anchor": (round(qa[0], 2), round(qa[1], 2)), "elbow": (round(el2[0], 2), round(el2[1], 2)),
            "end": (round(el2[0] + tw2 + 1, 2), round(el2[1], 2)),
            "text_at": (round(el2[0] + 0.5, 2), round(el2[1] + 0.8, 2)), "on_edge": True, "dir": 1}}))
    return out


def hub_keyway_dims(view: str, ex: float, cy: float, sc: float, rb: float, w: float, top: float, name: str, dev) -> list[Dim]:
    """端视图上的轮毂键槽尺寸（GB/T 1095）：槽宽 b（JS9）与 d + t2（上偏差按 b）。键槽在端视图上方。
    ex, cy = 端视图中心（图纸 mm）；top = 槽底到轴线（模型 mm）；dev(text) 决定是否保留偏差。齿轮盘与套类共用。"""
    from .standards import tol_text
    yk = cy + top * sc
    tw_ = dev(tol_text(w, "JS9"))
    tx = ex - w / 2 * sc - 1.5 - text_width(tw_, 3.5) / 2  # 文字放在左尺寸界线之外，不压中心线与界线
    yw = (yk + cy + (rb * rb - w * w / 4) ** 0.5 * sc) / 2  # 键槽侧壁中点
    out = [Dim(view, "linear", "x", w, tw_, f"{name}_w", None, {
        "sheet_pts": [(round(ex - w / 2 * sc, 2), round(yk, 2)), (round(ex + w / 2 * sc, 2), round(yk, 2))],
        "pick_pts": [(round(ex - w / 2 * sc, 2), round(yw, 2)), (round(ex + w / 2 * sc, 2), round(yw, 2))],
        "sheet_pos": (round(tx, 2), round(yk + 8, 2)), "orient": "h"})]
    t = 0.1 if w <= 6 else 0.2  # GB/T 1095 轮毂槽深 t2 上偏差
    tt = dev(f"{top + rb:g}(+{t:g}/0)")
    half = text_width(tt, 3.5) / 2
    ty = max(cy + (top - rb) / 2 * sc, cy + half + 1.5)  # 文字不压水平中心线
    if ty + half > yk - 1:  # 也不压槽底的尺寸界线：放不下就移到尺寸界线之外（上方），尺寸线延长
        ty = yk + 1.5 + half
    out.append(Dim(view, "linear", "y", top + rb, tt, f"{name}_t", None, {
        "sheet_pts": [(round(ex, 2), round(cy - rb * sc, 2)), (round(ex + w / 2 * sc, 2), round(yk, 2))],
        "pick_pts": [(round(ex, 2), round(cy - rb * sc, 2)), (round(ex, 2), round(yk, 2))],  # 孔底象限点、槽底中点
        "sheet_pos": (round(ex + rb * sc + 8, 2), round(ty, 2)), "orient": "v"}))
    return out


def cross_hole_zone(p: ResolvedPart, h: dict, sc: float) -> tuple[float, float, float, float]:
    """竖直横孔局部剖范围（模型 mm）：z 向孔两侧各留 3 mm 图纸（不越出所在轴段），y 向通孔全径、盲孔到孔底以下 3 mm 图纸。"""
    z0, z1 = p.step_z(h["step"])
    m = 3.0 / sc
    R = h["D"] / 2
    ext = (h["thread"]["d"] if h["thread"] else h["hole"]) / 2
    za, zb = max(h["z"] - ext - m, z0), min(h["z"] + ext + m, z1)
    if h["through"]:
        return za, zb, -R, R
    cone = h["hole"] / 2 * math.tan(math.radians(31))
    return za, zb, max(R - h["drill"] - cone - m, -R), R


def hub_keyway_symmetry_extra(hk: list, ex: float, ey: float, r_out: float) -> dict:
    """轮毂键槽对称度框格的引线（端视图，图纸 mm）：箭头在槽宽右尺寸界线上、与尺寸线对齐，水平向右 4 mm 后折向上，
    框格在端视图外圆上方（避开 d+t2 竖直尺寸及其外移的文字）。hk = hub_keyway_dims 的结果；r_out = 外圆图上半径。"""
    xr, yw = hk[0].extra["sheet_pts"][1][0], hk[0].extra["sheet_pos"][1]
    xk = round(xr + 4, 2)
    y_frame = round(max(ey + r_out + 6, yw + 6), 2)
    return {"sheet_at": (xr, yw), "normal": (0, 1), "elbow_h": {"from": (xr, yw), "to": (xk, yw)},
            "reach": round(y_frame - yw, 2), "sheet_pos": (xk, y_frame)}


def draw_elbow_fcf(s, ex_: dict) -> None:
    """按 hub_keyway_symmetry_extra 画：水平段 + 箭头（指向尺寸界线）+ 竖直段上到框格。"""
    from . import render_gdt
    from .render_shaft import _arrow
    (xa, ya), (xb, yb) = ex_["elbow_h"]["from"], ex_["elbow_h"]["to"]
    s.line(xa, ya, xb, yb, 0.25)
    _arrow(s, xa, ya, -1, 0)
    render_gdt.fcf(s, xb, yb, (0, 1), ex_["cells"], reach=ex_["reach"], arrow=False)


def _bore_anchor(p: ResolvedPart, k: int, kind: str, sc: float):
    """套类内孔的精度符号锚点（主视图全剖）：
    基准 —— 三角形贴在下孔壁、与孔径尺寸线对齐（表示以孔轴线为基准），引线穿出剖面，方框在零件下方；
    框格 —— 引线指向上孔壁（render_shaft 统一放到框格行）；
    粗糙度 —— 孔够大时立在下孔壁上（孔内空白），避开竖直尺寸线；否则从下孔壁向下引出。"""
    b = p.shaft_bores[k]
    rb = b["d"] / 2
    zd = bore_dia_place(p, k, sc)
    z0, z1 = b["z0"], b["z1"]

    def r_out(z):
        return next((d / 2 for i, (L, d) in enumerate(p.shaft_steps) if p.step_z(i)[0] - 1e-9 <= z <= p.step_z(i)[1] + 1e-9), rb)
    if kind == "datum":
        return "front", (zd, -rb), (0, -1), {"reach": (r_out(zd) - rb) * sc + 7}
    if kind == "gtol":
        zg = z0 + 0.3 * (z1 - z0)
        if abs(zg - zd) * sc < 3:
            zg = z0 + 0.7 * (z1 - z0)
        return "front", (zg, rb), (0, 1)
    vlines = [shaft_dia_z(p, j, sc) for j in range(len(p.shaft_steps))] + \
             [bore_dia_place(p, i, sc) for i in range(len(p.shaft_bores))] + bore_steps(p)
    if b["d"] * sc >= 14:
        cands = [z0 + (z1 - z0) * t / 20 for t in range(1, 20)]
        best = min(cands, key=lambda z: (sum(z - 3.5 / sc <= x <= z + 22 / sc for x in vlines), abs(z - (z0 + z1) / 2)))
        if not any(best - 3.5 / sc <= x <= best + 22 / sc for x in vlines):  # √ 左笔画伸出约 2.4 mm
            return "front", (best, -rb), (0, 1)
    zr = z0 + 0.5 * (z1 - z0)
    return "front", (zr, -rb), (0, -1), {"reach": (r_out(zr) - rb) * sc + 6}


def _step_bounds(p: ResolvedPart) -> list[float]:
    zs, z = [0.0], 0.0
    for L, _ in p.shaft_steps:
        z += L
        zs.append(z)
    return zs


def bore_steps(p: ResolvedPart) -> list[float]:
    """内孔台阶/孔底的轴向位置（剖视中贯穿孔径的竖线），不含两端面。"""
    Ltot = p.bbox.zmax - p.bbox.zmin
    zs = {round(b[k], 6) for b in p.shaft_bores for k in ("z0", "z1")}
    return sorted(z for z in zs if 1e-6 < z < Ltot - 1e-6
                  and abs(bore_radius_at(p, z - 1e-6) - bore_radius_at(p, z + 1e-6)) > 1e-9)


def bore_radius_at(p: ResolvedPart, z: float) -> float:
    """z 处内孔半径（无孔 0）。"""
    return max([b["d"] / 2 for b in p.shaft_bores if b["z0"] - 1e-9 <= z <= b["z1"] + 1e-9] or [0.0])


def shaft_text_room(p: ResolvedPart, z: float, d: float) -> float:
    """直径尺寸文字（竖写、居中在轴线上）可用的长度（模型 mm）：实心 = 直径；剖开的空心段 = 孔径（文字在孔的空白里，
    不压剖面线和孔壁轮廓）。"""
    rb = bore_radius_at(p, z)
    return 2 * rb if rb > 0 else d


def bore_dia_place(p: ResolvedPart, k: int, sc: float) -> float:
    """第 k 个内孔直径尺寸线的轴向位置：文字不压内台阶线、离外径尺寸线 ≥ 5 mm（图纸）、离孔口/孔底 ≥ 3 mm，尽量居中。"""
    b = p.shaft_bores[k]
    outer = [shaft_dia_z(p, j, sc) for j in range(len(p.shaft_steps))]
    others = [bore_dia_place(p, i, sc) for i in range(k)]
    z0, z1 = b["z0"], b["z1"]
    cands = [z0 + (z1 - z0) * t / 40 for t in range(2, 39)]

    steps_ = bore_steps(p)

    def key(z):
        clear = min([abs(z - c) * sc for c in outer + others] or [1e9])
        text_ok = all(not (z - 4.7 / sc <= x <= z + 0.3 / sc) for x in steps_ + outer + others)  # 文字竖写在线左侧
        want = 7.0 if (z1 - z0) * sc >= 16 else 4.0  # 短孔（沉孔）只求不压线
        return (text_ok, min(clear, want), min(z - z0, z1 - z) * sc >= 3, -abs(z - (z0 + z1) / 2))
    return max(cands, key=key)


def bore_dia_overflow(p: ResolvedPart, k: int, sc: float, text: str) -> str | None:
    """内孔直径文字放不进孔时伸出到外轮廓之外（down 优先，下方是链式尺寸行之前的空白）。"""
    b = p.shaft_bores[k]
    tw = text_width(text, 3.5)
    if tw <= b["d"] * sc - 2:
        return None
    z = bore_dia_place(p, k, sc)
    R = max(dd for _, dd in p.shaft_steps) / 2
    d_out = next(d for (L, d), (z0, z1) in zip(p.shaft_steps, (p.step_z(i) for i in range(len(p.shaft_steps))))
                 if z0 - 1e-9 <= z <= z1 + 1e-9)
    return "down" if tw + 3 <= (R - d_out / 2) * sc + 15 - 1.5 else "up"


def shaft_dia_overflow(p: ResolvedPart, j: int, sc: float, text: str) -> str | None:
    """轴段直径文字长于尺寸线时伸出轮廓的方向：下方（到链式长度尺寸行之间）放得下、且该段下方没有基准符号 → down，
    否则 up（上方是粗糙度、几何公差的位置，尽量留给它们）。不溢出返回 None。与 render_shaft._vdim 一致。"""
    d = p.shaft_steps[j][1]
    tw = text_width(text, 3.5)
    if tw <= shaft_text_room(p, shaft_dia_z(p, j, sc), d) * sc - 2 and not shaft_dia_blocked(p, j, sc):
        return None
    R = max(dd for _, dd in p.shaft_steps) / 2
    room = (R - d / 2) * sc + 15 - 1.5  # 轮廓下沿到链式尺寸线（y_bot − 15）；该行文字在段中点，不在直径尺寸线处
    has_datum = any(x["target"] == f"step:{j}" for x in p.datums)
    return "down" if tw + 3 <= room and not has_datum else "up"


def shaft_top_obstacles(p: ResolvedPart, sc: float) -> list[float]:
    """轴类主视图轮廓上方的竖线位置（模型 z）：向上伸出的直径文字、键槽/环槽/螺纹尺寸界线、几何公差框格引线。
    上方的粗糙度符号（文字向右约 21 mm）要避开它们。"""
    Ltot = p.bbox.zmax - p.bbox.zmin
    zs = [shaft_dia_z(p, j, sc) for j in range(len(p.shaft_steps))
          if shaft_dia_overflow(p, j, sc, _shaft_dia_label(p, j)) == "up"]
    for f in p.shaft_features:
        if f["kind"] == "gear":
            continue
        zs += [f["z0"], f["z1"], p.step_z(f["step"])[0]]
        if f["kind"] == "keyway" and f["step"] == len(p.shaft_steps) - 1:
            zs.append(Ltot)
    fr = shaft_gtol_fracs(p, sc)
    for g in p.geotols:
        if g["target"].startswith("step:"):
            z0, z1 = p.step_z(int(g["target"][5:]))
            zs.append(z0 + fr.get(f"{g['type']}@{g['target']}", 0.3) * (z1 - z0))
        elif g["target"].startswith("shoulder:"):
            sg = shoulder_gtol_geom(p, int(g["target"][9:]), sc)
            zs += [sg["z_leader"]]
    return zs


def shaft_gtol_frac(p: ResolvedPart, i: int, sc: float) -> float:
    """轴段 i 上第一个几何公差框格引线的位置（比例）。"""
    fr = shaft_gtol_fracs(p, sc)
    return next((fr[k] for k in fr if k.endswith(f"@step:{i}")), 0.3)


GTOL_ROW = 22.0     # 框格第一行底边：主视图最大轮廓上方（图纸 mm）
GTOL_ROW_GAP = 9.0  # 第二行比第一行高（框格 6 + 间隙 3）


def shoulder_gtol_geom(p: ResolvedPart, i: int, sc: float) -> dict[str, float]:
    """轴肩 i（第 i、i+1 段之间）端面的几何公差引线：在小端一侧、端面中部高度（零件外的空白处）水平引向端面，
    箭头沿轴向；竖段（z_leader，由 shaft_gtol_layout 选）上到框格行。from = +1 箭头自左向右（小端在左）。"""
    z = p.step_z(i)[1]
    da, db = p.shaft_steps[i][1], p.shaft_steps[i + 1][1]
    sgn = 1 if da < db else -1
    lay = shaft_gtol_layout(p, sc)
    key = next((k for k in lay if k.endswith(f"@shoulder:{i}")), None)
    zl = lay[key][0] if key else z - sgn * 6 / sc
    rs, rl = min(da, db) / 2, max(da, db) / 2
    big = i + 1 if da < db else i
    gaps = [(rs, rl)]  # 端面上被法兰孔（剖视中上半剖到孔）断开的部分不可作箭头落点
    for fh in getattr(p, "flange_holes", None) or []:
        if fh["step"] == big:
            ha, hb = fh["pcd"] / 2 - fh["d"] / 2, fh["pcd"] / 2 + fh["d"] / 2
            gaps = [seg for a, b in gaps for seg in ((a, min(b, ha)), (max(a, hb), b)) if seg[1] - seg[0] > 1e-6]
    a, b = max(gaps, key=lambda t: t[1] - t[0]) if gaps else (rs, rl)
    return {"z": z, "r_small": rs, "r_large": rl, "from": sgn, "z_leader": zl,
            "small": i if da < db else i + 1, "r_at": (a + b) / 2}


_LAYOUT_CACHE: dict = {}


def shaft_gtol_layout(p: ResolvedPart, sc: float) -> dict[str, tuple[float, int]]:
    """轴类主视图上方几何公差框格的联合排布：每个框格 → (引线 z, 行号 0–2)。
    轴段框格引线在段内取位；轴肩端面框格的引线在小端一侧离端面 5–30 mm 内取位（水平引到端面）。
    代价：同一行框格重叠、竖引线穿过下面行的框格或齿面 Ra 文字（重罚），竖引线相贴、穿过轴肩水平引线、
    靠近尺寸界线/上方尺寸文字（轻罚），行越高越差；带 Ra 的轴段在框格上方一行预留 Ra 的位置。
    贪心按两种顺序（轴肩先 / 从左到右）各排一次，取总代价小的。"""
    key = (id(p), round(sc, 6), len(p.geotols))
    if key in _LAYOUT_CACHE and _LAYOUT_CACHE[key][0] is p:
        return _LAYOUT_CACHE[key][1]
    from .render_gdt import _widths
    flank = [(f["z1"] - 3 / sc, f["z1"] + 19 / sc) for f in p.shaft_features if f["kind"] == "gear"]
    lines = []
    texts = []  # 上方尺寸文字（键槽/环槽长度、定位）的中点与半宽（模型 mm）
    Ltot = p.bbox.zmax - p.bbox.zmin
    for f in p.shaft_features:
        if f["kind"] != "gear":
            lines += [f["z0"], f["z1"]]
            texts.append(((f["z0"] + f["z1"]) / 2, (text_width(f"{f['z1'] - f['z0']:g}", 3.5) / 2 + 1.5) / sc))
            sz0 = p.step_z(f["step"])[0]
            a_, b_ = (f["z1"], Ltot) if f["kind"] == "keyway" and f["step"] == len(p.shaft_steps) - 1 else (sz0, f["z0"])
            if b_ - a_ > 1e-6:
                texts.append(((a_ + b_) / 2, (text_width(f"{b_ - a_:g}", 3.5) / 2 + 1.5) / sc))
    for h in p.cross_holes:  # 横孔定位尺寸的界线与文字
        lines.append(h["z"])
        sz0 = p.step_z(h["step"])[0]
        texts.append(((sz0 + h["z"]) / 2, (text_width(f"{h['z'] - sz0:g}", 3.5) / 2 + 1.5) / sc))
    shoulder_faces = [p.step_z(int(g["target"][9:]))[1] for g in p.geotols if g["target"].startswith("shoulder:")]
    ra_steps = {r["target"] for r in p.roughness}  # 这些段的框格上方可能还要标 Ra（占上一行）

    def cands(g):
        k, _, ref = g["target"].partition(":")
        i = int(ref)
        if k == "step":
            z0, z1 = p.step_z(i)
            return [(z0 + t * (z1 - z0), abs(t - 0.3)) for t in (0.3, 0.5, 0.7, 0.15, 0.85)]
        z = p.step_z(i)[1]
        da, db = p.shaft_steps[i][1], p.shaft_steps[i + 1][1]
        sgn = 1 if da < db else -1
        Ls = p.shaft_steps[i if da < db else i + 1][0]
        out_ = [(z - sgn * k / sc, k / 30) for k in (5, 8, 12, 16, 20, 25, 30) if k / sc <= Ls - 2 / sc]
        return out_ or [(z - sgn * min(5 / sc, Ls / 2), 0.0)]

    gs_all = [g for g in p.geotols if g["target"].startswith(("step:", "shoulder:"))]
    info = []
    for g in gs_all:
        w = sum(_widths([g["symbol"], g["zone"], *g["datums"]]))
        sh = g["target"].startswith("shoulder:")
        info.append({"key": f"{g['type']}@{g['target']}", "w": w / sc, "ra": g["target"] in ra_steps,
                     "zf": p.step_z(int(g["target"][9:]))[1] if sh else None, "cands": cands(g)})

    def solo(it, z, row, pref):  # 与其它框格无关的代价
        c = 4 * row + pref
        c += sum(20 for fa, fb in flank if fa <= z <= fb)
        c += sum(3 for x in lines + shoulder_faces if abs(x - z) < 2.5 / sc)
        c += sum(3 for m, hw in texts if abs(m - z) < hw)
        return c

    def pair(i1, s1, i2, s2):  # 两个框格之间的代价（对称）
        (z1, r1), (z2, r2) = s1, s2
        c = 0.0
        a1, b1 = z1 - 3.5 / sc, z1 + i1["w"] - 3.5 / sc
        a2, b2 = z2 - 3.5 / sc, z2 + i2["w"] - 3.5 / sc
        if r1 == r2 and a1 < b2 + 1 / sc and a2 < b1 + 1 / sc:
            c += 100
        for (ia, za, ra_, aa, ba), (ib, zb, rb_, ab, bb) in (((i1, z1, r1, a1, b1), (i2, z2, r2, a2, b2)),
                                                             ((i2, z2, r2, a2, b2), (i1, z1, r1, a1, b1))):
            if ia["ra"] and rb_ == ra_ + 1 and za - 1.5 / sc < bb and ab < za + 25 / sc:
                c += 100  # 框格压在别人的 Ra 预留位
            if rb_ < ra_ and ab - 0.5 / sc <= za <= bb + 0.5 / sc:
                c += 20   # a 的竖引线穿过下面行 b 的框格
            if ib["zf"] is not None and min(zb, ib["zf"]) - 1 / sc <= za <= max(zb, ib["zf"]) + 1 / sc:
                c += 8    # a 的竖引线穿过 b 的轴肩水平引线
        if abs(z1 - z2) < 4 / sc:
            c += 8
        return c

    def total(st):
        t = sum(solo(info[k], *st[k][:2], st[k][2]) for k in range(len(info)))
        return t + sum(pair(info[i], st[i][:2], info[j], st[j][:2]) for i in range(len(info)) for j in range(i))

    def improve(st):
        for _ in range(4):
            changed = False
            for k, it in enumerate(info):
                best = None
                for z, pref in it["cands"]:
                    for row in (0, 1, 2):
                        c = solo(it, z, row, pref) + sum(pair(it, (z, row), info[j], st[j][:2])
                                                         for j in range(len(info)) if j != k)
                        if best is None or c < best[0] - 1e-9:
                            best = (c, (z, row, pref))
                if best[1] != st[k]:
                    cur = solo(it, *st[k][:2], st[k][2]) + sum(pair(it, st[k][:2], info[j], st[j][:2])
                                                               for j in range(len(info)) if j != k)
                    if best[0] < cur - 1e-9:
                        st[k] = best[1]
                        changed = True
            n = len(info)  # 成对联合移动（两个框格交换行/位置，单个移动跳不出来）
            for i in range(n):
                for j in range(i):
                    base_c = total(st)
                    best = None
                    for zi, pi in info[i]["cands"]:
                        for ri in (0, 1, 2):
                            for zj, pj in info[j]["cands"]:
                                for rj in (0, 1, 2):
                                    t = dict(st)
                                    t[i], t[j] = (zi, ri, pi), (zj, rj, pj)
                                    c = total(t)
                                    if best is None or c < best[0]:
                                        best = (c, t)
                    if best[0] < base_c - 1e-9:
                        st.update(best[1])
                        changed = True
            if not changed:
                break
        return st

    def run(order):
        st: dict[int, tuple] = {}
        for k in order:
            it = info[k]
            best = None
            for z, pref in it["cands"]:
                for row in (0, 1, 2):
                    c = solo(it, z, row, pref) + sum(pair(it, (z, row), info[j], st[j][:2]) for j in st)
                    if best is None or c < best[0]:
                        best = (c, (z, row, pref))
            st[k] = best[1]
        st = improve(st)
        return total(st), st

    def zkey(k):
        g = gs_all[k]
        kind, _, ref = g["target"].partition(":")
        return p.step_z(int(ref))[0] if kind == "step" else p.step_z(int(ref))[1]
    idx = list(range(len(info)))
    orders = [sorted(idx, key=lambda k: (info[k]["zf"] is None, zkey(k))), sorted(idx, key=zkey),
              sorted(idx, key=lambda k: (info[k]["zf"] is not None, zkey(k)))]
    _, st = min((run(o) for o in orders), key=lambda t: t[0]) if info else (0, {})
    res = {info[k]["key"]: (st[k][0], st[k][1]) for k in st}
    _LAYOUT_CACHE[key] = (p, res)
    return res


def shaft_gtol_fracs(p: ResolvedPart, sc: float) -> dict[str, float]:
    """轴段框格引线在段内的比例（由 shaft_gtol_layout 换算；轴肩框格不在其中）。"""
    out = {}
    for k, (z, _) in shaft_gtol_layout(p, sc).items():
        if "@step:" in k:
            z0, z1 = p.step_z(int(k.split("@step:")[1]))
            out[k] = (z - z0) / (z1 - z0)
    return out


def shaft_bottom_obstacles(p: ResolvedPart, sc: float) -> list[float]:
    """轴类主视图轮廓下方的占位（模型 z）：基准符号、键槽剖切符号、向下伸出的直径文字。"""
    zs = [shaft_dia_z(p, j, sc) for j in range(len(p.shaft_steps))
          if shaft_dia_overflow(p, j, sc, _shaft_dia_label(p, j)) == "down"]
    zs += [(f["z0"] + f["z1"]) / 2 for f in p.shaft_features if f["kind"] == "keyway"]
    z = 0.0
    zs.append(z)
    for L, _ in p.shaft_steps:  # 各段分界：链式长度尺寸的尺寸界线向下引出
        z += L
        zs.append(z)
    for x in p.datums:
        if x["target"].startswith("step:"):
            z0, z1 = p.step_z(int(x["target"][5:]))
            zs += [z0 + t * (z1 - z0) for t in (0.2, 0.5, 0.8)]  # 基准位置随键槽避让在这三处之一
    return zs


BREAK_GAP = 8.0  # 断开处图上间隙（mm）


def shaft_breaks(p: ResolvedPart) -> list[tuple[float, float]]:
    """细长轴的断开区间（模型 z）：轴长 ≥ 8 倍最大直径时，无键槽/环槽/螺纹/齿轮、无基准与几何公差的长光轴段
    （去掉部分 > 2 倍最大直径）中段断开，两端各保留 max(该段直径, 10 mm)。GB/T 4458.1：较长且沿长度方向形状一致的
    机件可断开缩短绘制，尺寸仍注实长。"""
    if not p.shaft_steps:
        return []
    Ltot = p.bbox.zmax - p.bbox.zmin
    Dm = max(d for _, d in p.shaft_steps)
    if Ltot < 8 * Dm:
        return []
    busy = {f["step"] for f in p.shaft_features}
    busy |= {int(x["target"][5:]) for x in (*p.datums, *p.geotols) if x["target"].startswith("step:")}
    for zb in bore_steps(p):  # 内孔台阶/孔底所在轴段不断开
        busy |= {i for i in range(len(p.shaft_steps)) if p.step_z(i)[0] - 1e-6 <= zb <= p.step_z(i)[1] + 1e-6}
    out = []
    for i, (L, d) in enumerate(p.shaft_steps):
        if i in busy:
            continue
        z0, z1 = p.step_z(i)
        keep = max(d, 10.0)
        if (z1 - z0) - 2 * keep > 2 * Dm:
            out.append((round(z0 + keep, 6), round(z1 - keep, 6)))
    return out


def unbreak_z(p: ResolvedPart, z: float) -> float:
    """落在断开区间内的模型 z 移到左侧保留段中部（符号、尺寸线不能放在断开处）。"""
    for a, b in shaft_breaks(p):
        if a - 1e-9 < z < b + 1e-9:
            i = next(j for j in range(len(p.shaft_steps)) if p.step_z(j)[0] <= a <= p.step_z(j)[1])
            return (p.step_z(i)[0] + a) / 2
    return z


def shaft_view_width(v: "ViewPlan", Ltot: float) -> float:
    return (Ltot - sum(b - a for a, b in v.breaks)) * v.scale + len(v.breaks) * BREAK_GAP


DETAIL_SCALES = (2.0, 2.5, 4.0, 5.0, 10.0)  # GB/T 14690 放大比例
ROMAN = ("I", "II", "III", "IV", "V", "VI", "VII", "VIII")


def detail_geom(f: dict, scale: float) -> tuple[float, float]:
    """局部放大图（环槽）：放大比例 ds（槽深放大后 ≥ 2 mm，且至少为主视图 2 倍）与区域模型半径 rm（图上半径 ≤ 28 mm）。"""
    h = (f["d"] - f["d2"]) / 2
    ds = next((k for k in DETAIL_SCALES if h * k >= 2.0 and k >= 2 * scale), DETAIL_SCALES[-1])
    rm = min(f["width"] / 2 + 3.0, 28.0 / ds, 0.8 * f["d"] / 2)
    return ds, rm


def detail_sheet(v: "ViewPlan"):
    """放大图内模型 (z, y) → 图纸 (x, y)。"""
    xd, yd = v.origin_on_sheet
    zc, yc, ds = v.detail["zc"], v.detail["yc"], v.scale
    return lambda z, y: (xd + (z - zc) * ds, yd + (y - yc) * ds)


def shaft_sx(v: "ViewPlan", Ltot: float | None, z: float) -> float:
    """轴类主视图：模型 z → 图纸 x（考虑断开）。断开区间内线性映射到间隙。Ltot=None 时由视图宽度反推。"""
    sc = v.scale
    if Ltot is None:
        Ltot = (v.size_mm[0] - len(v.breaks) * BREAK_GAP) / sc + sum(b - a for a, b in v.breaks)
    x = v.origin_on_sheet[0] - shaft_view_width(v, Ltot) / 2 + z * sc
    for a, b in v.breaks:
        if z >= b:
            x -= (b - a) * sc - BREAK_GAP
        elif z > a:
            x -= (z - a) * sc - (z - a) / (b - a) * BREAK_GAP
    return x


def shaft_dia_z(p: ResolvedPart, i: int, sc: float = 1.0) -> float:
    """第 i 段直径尺寸线的轴向位置（SVG 与原生标注共用），见 shaft_dia_place。"""
    return shaft_dia_place(p, i, sc)[0]


def shaft_dia_place(p: ResolvedPart, i: int, sc: float = 1.0) -> tuple[float, str]:
    """见 _shaft_dia_choice。"""
    z, side, _ = _shaft_dia_choice(p, i, sc)
    return z, side


def shaft_dia_blocked(p: ResolvedPart, i: int, sc: float = 1.0) -> bool:
    """第 i 段轮廓内找不到不压线的直径文字位置（如键槽占满轴段）→ 文字须伸到轮廓外。"""
    return _shaft_dia_choice(p, i, sc)[2] < 0.2


def _shaft_dia_choice(p: ResolvedPart, i: int, sc: float = 1.0) -> tuple[float, str, float]:
    """第 i 段直径尺寸线的轴向位置与文字侧（left | right，文字竖写在尺寸线左/右）：
    尺寸线离键槽/环槽越远越好；文字不压轴肩线、倒角线、键槽轮廓（文字厚约 4.5 mm 图纸）；优先文字在左、居中；
    避开断开处。"""
    Ltot = p.bbox.zmax - p.bbox.zmin
    c = p.shaft_chamfer
    z0, z1 = p.step_z(i)
    busy = [(f["z0"], f["z1"]) for f in p.shaft_features if f["step"] == i and f["kind"] != "gear"]
    lines = [z0, z1] + ([c] if c and z0 == 0 else []) + ([Ltot - c] if c and abs(z1 - Ltot) < 1e-6 else [])
    # 文字的障碍：键槽/环槽轮廓、各竖线、轴端螺孔虚线；螺纹只有终止线（小径细线是水平的）
    obst = [(f["z0"], f["z1"]) for f in p.shaft_features if f["step"] == i and f["kind"] not in ("gear", "thread")]
    obst += [(h["z0"], h["z1"] + 0.3 * h["tap"]) for h in p.end_holes]
    for ch in getattr(p, "center_holes", None) or []:  # 轴端中心孔（主视图画出锥孔轮廓，深约 D）
        obst.append((0.0, ch["D"] + 0.5) if ch["end"] == "start" else (Ltot - ch["D"] - 0.5, Ltot))
    obst += [(h["z"] - h["d"] / 2 - 0.5, h["z"] + h["d"] / 2 + 0.5) if h["dir"] == "view"
             else cross_hole_zone(p, h, sc)[:2] for h in p.cross_holes]
    busy += [(h["z"] - h["d"] / 2, h["z"] + h["d"] / 2) for h in p.cross_holes if h["step"] == i]
    for f in p.shaft_features:
        if f["step"] == i and f["kind"] == "thread":
            zt = f["z0"] if f["side"] == "end" else f["z1"]
            obst.append((zt, zt))
    # 剖视（空心）：外轴肩/端面线只在孔壁之外，轴线附近的竖写文字碰不到；内孔台阶线贯穿孔径，是障碍
    lines = [x for x in lines if max(bore_radius_at(p, x - 1e-6), bore_radius_at(p, x + 1e-6)) < 6]
    obst += [(x, x) for x in lines + bore_steps(p)]
    brk = shaft_breaks(p)
    cands = [z0 + (z1 - z0) * k / 40 for k in range(2, 39)]
    cands = [z for z in cands if not any(a - 2 <= z <= b + 2 for a, b in brk)] or [unbreak_z(p, (z0 + z1) / 2)]
    tw = 4.5 / sc  # 文字厚度（模型 mm）

    def gap(lo, hi):  # 区间 [lo, hi] 到障碍的最小净距（模型 mm；重叠为负）
        return min((max(a - hi, lo - b) for a, b in obst), default=1e9)

    # 文字长于该段直径（公差文字）→ 会伸到轮廓外；此时尺寸线避开轴段中点（下方链式长度数字、上方键槽尺寸都在中点附近）
    d_i = p.shaft_steps[i][1]
    long_txt = text_width(_shaft_dia_label(p, i), 3.5) > shaft_text_room(p, (z0 + z1) / 2, d_i) * sc - 2
    # 要避开的数字区：链式长度数字（段中点）、键槽长度数字（键槽中点，在轮廓上方）
    zones = [((z0 + z1) / 2, (text_width(f"{z1 - z0:g}", 3.5) / 2 + 2.0) / sc)]
    zones += [((f["z0"] + f["z1"]) / 2, (text_width(f"{f['z1'] - f['z0']:g}", 3.5) / 2 + 2.0) / sc)
              for f in p.shaft_features if f["step"] == i and f["kind"] == "keyway"]

    def key(zs):
        z, side = zs
        span = (z - tw, z - 0.2 / sc) if side == "left" else (z + 0.2 / sc, z + tw + 0.5 / sc)
        if long_txt:
            lo_, hi_ = span
            edge_ = min(max(a - hi_, lo_ - b) for a, b in [(z0, z0), (z1, z1)] + busy) * sc
            return (any(not (hi_ < zc - zr or lo_ > zc + zr) for zc, zr in zones),
                    edge_ < 2.5, abs(z - (z0 + z1) / 2), side == "right")
        line_clear = min([max(a - z, z - b) for a, b in busy] or [1e9])
        # 空心段：优先放在孔径较小（实壁较厚）处，把孔口/沉孔段留给内孔尺寸
        return (min(gap(*span) * sc, 1.0), min(line_clear * sc, 3.0), -round(bore_radius_at(p, z), 3), side == "left",
                -abs(z - (z0 + z1) / 2))
    if long_txt:  # 段中点放不下（键槽处文字不能跨键槽轮廓）：先找轮廓内放得下的位置（键槽两端到轴肩的整直径区）
        tw_lab = text_width(_shaft_dia_label(p, i), 3.5)
        inside = []
        for z in cands:
            if tw_lab > shaft_text_room(p, z, d_i) * sc - 2:
                continue
            for sd in ("left", "right"):
                span = (z - tw, z - 0.2 / sc) if sd == "left" else (z + 0.2 / sc, z + tw + 0.5 / sc)
                g_ = gap(*span) * sc
                if g_ >= 0.3:
                    inside.append((min(g_, 1.0), sd == "left", -abs(z - (z0 + z1) / 2), z, sd, g_))
        if inside:
            best_in = max(inside)
            return best_in[3], best_in[4], best_in[5]
    if long_txt:  # 文字在轮廓外：尺寸线离开中点数字区（离不开的短段照常取中）
        z, sd = min(((z, sd) for z in cands for sd in ("left", "right")), key=key)
        if not key((z, sd))[0] and not key((z, sd))[1]:
            span = (z - tw, z - 0.2 / sc) if sd == "left" else (z + 0.2 / sc, z + tw + 0.5 / sc)
            return z, sd, gap(*span) * sc
        long_txt = False
    z, sd = max(((z, sd) for z in cands for sd in ("left", "right")), key=key)
    span = (z - tw, z - 0.2 / sc) if sd == "left" else (z + 0.2 / sc, z + tw + 0.5 / sc)
    g = gap(*span) * sc
    if g < 0.2:  # 轮廓内放不下：文字伸到轮廓外，尺寸线取轴段中点附近（避开相邻粗段的轴肩线）
        z = min(cands, key=lambda z_: abs(z_ - (z0 + z1) / 2))
        sd = "left"
    return z, sd, g


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
    X = lambda z: shaft_sx(main, Ltot, z)  # noqa: E731
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
            return (round(X(z), 2), round(cy, 2))
        rs = [r for r in (radius_at(z - 1e-6), radius_at(z + 1e-6)) if r > 0]
        r = min(rs)
        off = min(0.85 * r, max(r - c - 0.5, 0.3 * r))
        bores_end = [b for b in p.shaft_bores if any(abs(z - b[k]) < 1e-6 and 1e-6 < z < Ltot - 1e-6 for k in ("z0", "z1"))]
        if bores_end and all(abs(z - zz) > 1e-6 for zz in _step_bounds(p)):  # 盲孔孔底（剖视中只在孔径内有线）
            return (round(X(z), 2), round(cy - bores_end[0]["d"] / 4 * sc, 2))
        rb = max(bore_radius_at(p, z - 1e-6), bore_radius_at(p, z + 1e-6))
        if rb > 0:  # 剖视：端面/轴肩线只在孔壁与外轮廓之间
            off = (min(rb, r) + r) / 2 if r > rb else rb
        return (round(X(z), 2), round(cy - off * sc, 2))

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
                y = y_top + 14 - 7 * (d.extra or {}).get("track", 0)
            kw = not d.ref.startswith(("step", "overall"))
            xh = {round(h["z"], 6): h for h in p.cross_holes} if d.ref.startswith("XH") else {}
            def xh_pt(h):  # 横孔：view 向取孔圆顶点；竖直孔取左孔壁线上一点（TODO(实机核对) 原生需量到孔轴线）
                if h["dir"] == "vertical":
                    return (round(X(h["z"] - h["hole"] / 2), 2), round(cy + h["D"] / 2 * 0.6 * sc, 2))
                return (round(X(h["z"]), 2), round(cy + h["hole"] / 2 * sc, 2))
            pts = [xh_pt(xh[round(z, 6)]) if round(z, 6) in xh
                   else edge_pt(z, kw and round(z, 6) in kw_ends) for z in (za, zb)]
            d.extra = {**(d.extra or {}), "sheet_pts": pts, "sheet_pos": (round((pts[0][0] + pts[1][0]) / 2, 2), round(y, 2)),
                       "orient": "h"}
        elif d.kind == "diameter" and d.ref.startswith("bore"):
            k = int(d.ref[4:].split("_")[0])
            x = X(bore_dia_place(p, k, sc))
            r = d.at[1] / 2
            d.extra = {**(d.extra or {}), "sheet_pts": [(round(x, 2), round(cy - r * sc, 2)), (round(x, 2), round(cy + r * sc, 2))],
                       "sheet_pos": (round(x + 6, 2), round(cy, 2)), "orient": "v"}
        elif d.kind == "diameter" and d.ref.startswith("step"):
            i = int(d.ref[4:].split("_")[0])
            x = X(shaft_dia_z(p, i, sc))
            r = d.at[1] / 2
            d.extra = {**(d.extra or {}), "sheet_pts": [(round(x, 2), round(cy - r * sc, 2)), (round(x, 2), round(cy + r * sc, 2))],
                       "sheet_pos": (round(x + 6, 2), round(cy, 2)), "orient": "v"}
        elif d.kind == "callout" and d.ref.startswith(("ch:", "eh:")):
            # 中心孔：从端面轴线处向外下方引出，文字沿水平线向外（左端向左、右端向右）；保留/不允许保留符号在文字前
            z, _ = d.at
            ax, ay = X(z), cy
            sgn = -1 if d.ref.endswith(":start") else 1
            sym = 4.0 if (d.extra or {}).get("keep") in ("required", "forbidden") else 0.0
            w = text_width(d.text, 3.0) + sym + 1
            ex_, ey_ = ax + sgn * 5, cy - R * sc - 5
            end = (ex_ + sgn * w, ey_)
            sx = min(ex_, end[0]) + 0.5  # 符号在文字之前（左侧）
            d.extra = {**(d.extra or {}), "label": {"anchor": (round(ax, 2), round(ay, 2)), "elbow": (round(ex_, 2), round(ey_, 2)),
                                                     "text_at": (round(sx + sym, 2), round(ey_ + 0.8, 2)),
                                                     "end": (round(end[0], 2), round(end[1], 2)), "dir": sgn,
                                                     "sym_at": (round(sx, 2), round(ey_, 2))}}
        elif d.kind == "callout" and d.ref.startswith("xh:"):
            # 横孔：箭头在孔圆周右上 45°，引线斜向上到轮廓上方，文字向右（同环槽标注行）
            h = next(h for h in p.cross_holes if h["name"] == d.ref[3:])
            rh = h["hole"] / 2 * sc
            ax, ay = X(h["z"]) + rh * 0.7071, cy + rh * 0.7071
            if h["dir"] == "vertical":  # 竖直孔：箭头在孔口（上轮廓处孔壁）
                ax, ay = X(h["z"]) + rh, cy + h["D"] / 2 * sc
            ty = y_top + 18
            ex_ = ax + (ty - ay)
            w = text_width(d.text, 3.5) + 1
            d.extra = {**(d.extra or {}), "label": {"anchor": (round(ax, 2), round(ay, 2)), "elbow": (round(ex_, 2), round(ty, 2)),
                                                     "text_at": (round(ex_ + 0.5, 2), round(ty + 0.8, 2)),
                                                     "end": (round(ex_ + w, 2), round(ty, 2)), "dir": 1}}
        elif d.kind == "callout" and d.ref in ({"chamfer"} | {f["name"] for f in p.shaft_features}):
            # 位置同 render_shaft：引线从轮廓点斜向上到主视图上方的水平线
            z, r = d.at
            ax, ay = X(z), cy + r * sc
            ty = y_top + 4 + (4 if d.ref == "chamfer" else 14)
            w = text_width(d.text, 3) + 1
            tx = ax - 4 - w if d.ref == "chamfer" else ax + 6
            dir_ = 1
            if tx + w > plan.sheet_size[0] - MARGIN - 2:  # 右端伸出图框 → 改向左引出（引线竖直向上，不斜穿尺寸行）
                tx, dir_ = ax - w, -1
            elif tx < MARGIN + 2:
                tx = ax + 6
            d.extra = {**(d.extra or {}), "label": {"anchor": (round(ax, 2), round(ay, 2)),
                                                     "elbow": (round(tx + (w if dir_ < 0 else 0), 2), round(ty, 2)),
                                                     "text_at": (round(tx + 0.5, 2), round(ty + 0.8, 2)),
                                                     "end": (round(tx if dir_ < 0 else tx + w, 2), round(ty, 2)), "dir": dir_}}
    _shaft_label_rows(plan, p)
    _xh_callout_flip(plan)
    _shaft_section_dim_sheet(plan, p)
    _shaft_num_dodge(plan, p, X, y_top, y_bot, sc)


def _shaft_label_rows(plan: "DrawingPlan", p: ResolvedPart) -> None:
    """环槽/退刀槽/倒角引线标注同在主视图上方一行：相邻两个文字相压时，左边那个上移一行（5.5 mm）。"""
    names = {"chamfer"} | {f["name"] for f in p.shaft_features}
    ds = sorted((d for d in plan.dims if d.kind == "callout" and d.ref in names and "elbow" in (d.extra or {}).get("label", {})),
                key=lambda d: -d.extra["label"]["anchor"][0])  # 从右往左：上移的是左边那个，其引线在右边文字的左侧
    placed: list = []
    for d in ds:
        lb = d.extra["label"]
        x0, x1 = sorted((lb["elbow"][0], lb["end"][0]))
        dy = 0.0
        while any(x0 < b[2] + 1 and b[0] - 1 < x1 and lb["elbow"][1] + dy < b[3] and b[1] < lb["elbow"][1] + dy + 4.5
                  for b in placed) and dy < 30:
            dy += 5.5
        if dy:
            lb = {**lb, "elbow": (lb["elbow"][0], round(lb["elbow"][1] + dy, 2)), "end": (lb["end"][0], round(lb["end"][1] + dy, 2)),
                  "text_at": (lb["text_at"][0], round(lb["text_at"][1] + dy, 2))}
            d.extra = {**d.extra, "label": lb}
        placed.append((x0, lb["elbow"][1], x1, lb["elbow"][1] + 4.5))


def _shaft_num_dodge(plan: "DrawingPlan", p: ResolvedPart, X, y_top: float, y_bot: float, sc: float) -> None:
    """伸出轮廓的直径尺寸线（竖直）正好穿过上方键槽/环槽尺寸行或下方链式尺寸行的数字时，
    该数字在自己的尺寸线上偏到直径线一侧（GB/T 4458.4 允许数字不居中），写入 extra["text_x"]。"""
    lines = []  # (x, 方向)
    for j, (_, d_) in enumerate(p.shaft_steps):
        ov = shaft_dia_overflow(p, j, sc, _shaft_dia_label(p, j))
        if ov:
            lines.append((X(shaft_dia_z(p, j, sc)), ov))
    if not lines:
        return
    for d in plan.dims:
        ex = d.extra or {}
        if d.kind != "linear" or d.axis != "z" or "sheet_pts" not in ex or d.ref == "overall_z":
            continue
        below = d.ref.startswith("step")
        xa, xb = sorted(q[0] for q in ex["sheet_pts"])
        tw = text_width(d.text, 3.5)
        if tw > xb - xa - 1:
            continue  # 放不下的数字已移到界线外
        xc = (xa + xb) / 2
        hit = [x for x, ov in lines if (ov == "down") == below and abs(x - xc) < tw / 2 + 1]
        if not hit:
            continue
        x = hit[0]
        cands = [x - tw / 2 - 1.5, x + tw / 2 + 1.5]
        ok = [c for c in cands if xa + tw / 2 + 0.8 <= c <= xb - tw / 2 - 0.8
              and not any(abs(c - x2) < tw / 2 + 1 for x2, ov2 in lines if (ov2 == "down") == below)]
        if not ok:  # 界线之间偏不开：移到界线外（不压同行别的数字/界线、不压伸出的直径线）
            row = []
            for o in plan.dims:
                eo = o.extra or {}
                if o is d or o.kind != "linear" or o.axis != "z" or "sheet_pts" not in eo or o.ref == "overall_z" \
                        or o.ref.startswith("step") != below:
                    continue
                oa, ob = sorted(q[0] for q in eo["sheet_pts"])
                ot = text_width(o.text, 3.5)
                oc = eo.get("text_x", (oa + ob) / 2)
                row += [(oc - ot / 2, oc + ot / 2), (oa - 0.3, oa + 0.3), (ob - 0.3, ob + 0.3)]
            row += [(x2 - 4.5, x2 + 4.5) for x2, ov2 in lines if (ov2 == "down") == below]
            for c in (xb + 1.5 + tw / 2, xa - 1.5 - tw / 2):
                if not any(lo < c + tw / 2 + 0.5 and c - tw / 2 - 0.5 < hi for lo, hi in row):
                    ok = [c]
                    break
        if ok:
            c = min(ok, key=lambda c_: abs(c_ - xc))
            d.extra = {**ex, "text_x": round(c, 2), "sheet_pos": (round(c, 2), ex["sheet_pos"][1])}


def _seg_cross(p1, p2, q1, q2) -> bool:
    def o(a, b, c):
        return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])
    return o(p1, p2, q1) * o(p1, p2, q2) < 0 and o(q1, q2, p1) * o(q1, q2, p2) < 0


def _xh_callout_flip(plan: "DrawingPlan") -> None:
    """横孔引线布置：候选 右上 45° / 左上 45° / 再高一行（右、左），取第一个引线不穿尺寸数字、文字不压别的引线标注的。"""
    boxes = []
    for d in plan.dims:
        ex = d.extra or {}
        if d.view == "front" and d.kind == "linear" and ex.get("orient") == "h" and ex.get("sheet_pos"):
            px, py = ex["sheet_pos"]
            hw = text_width(d.text, 3.5) / 2 + 0.5
            boxes.append((px - hw, py, px + hw, py + 4.5))
        elif d.view == "front" and d.kind == "diameter" and ex.get("orient") == "v" and ex.get("sheet_pts"):
            (x_, ya_), (_, yb_) = ex["sheet_pts"]  # 轴段直径：竖写文字在尺寸线旁（左右侧都算），居中
            ym, hh = (ya_ + yb_) / 2, min(text_width(d.text, 3.5), abs(yb_ - ya_)) / 2
            boxes.append((x_ - 4.5, ym - hh, x_ + 4.5, ym + hh))
    for v in plan.views:  # 局部放大图在主视图上的罗马数字标号
        dt = getattr(v, "detail", None) or {}
        if v.type == "detail" and "label_at" in dt:
            lx, ly = dt["label_at"]
            boxes.append((lx - 0.5, ly - 1, lx + text_width(v.label, 3.5) + 0.5, ly + 4))
    placed = [(lb["text_at"][0], lb["elbow"][1], lb["text_at"][0] + text_width(d.text, 3.5), lb["elbow"][1] + 4.5)
              for d in plan.dims if d.kind == "callout" and not d.ref.startswith("xh:") and d.view == "front"
              and (lb := (d.extra or {}).get("label"))]

    def hits(a, b, bxs):
        return sum(1 for x0, y0, x1, y1 in bxs
                   if any(x0 <= a[0] + (b[0] - a[0]) * t / 20 <= x1 and y0 <= a[1] + (b[1] - a[1]) * t / 20 <= y1
                          for t in range(21)))
    xs = sorted((d for d in plan.dims if d.kind == "callout" and d.ref.startswith("xh:") and (d.extra or {}).get("label")),
                key=lambda d: d.extra["label"]["anchor"][0])
    lines_: list = []
    for d in xs:
        lb = d.extra["label"]
        (ax, ay), (ex0, ty0) = lb["anchor"], lb["elbow"]
        w = lb["end"][0] - ex0
        best = None
        cands = [(sg, dy, f) for f in (1.0, 0.577) for dy in (0, 7) for sg in (1, -1)]  # 45° / 60° 引线
        for k, (sg, dy, f) in enumerate(cands):
            ty = ty0 + dy
            ex_ = ax + sg * (ty - ay) * f
            tb = (ex_ + 0.5, ty, ex_ + w, ty) if sg > 0 else (ex_ - w + 0.5, ty, ex_, ty)
            tbox = (tb[0], ty, tb[2], ty + 4.5)
            over = sum(1 for x0, y0, x1, y1 in placed if tbox[0] < x1 and x0 < tbox[2] and tbox[1] < y1 and y0 < tbox[3])
            segs_ = [((ax, ay), (ex_, ty)), ((ex_, ty), (ex_ + sg * w, ty))]
            xing = sum(1 for p1, p2 in segs_ for q1, q2 in lines_ if _seg_cross(p1, p2, q1, q2))
            cost = 10 * (hits((ax, ay), (ex_, ty), boxes + placed) + over) + 5 * xing + k
            if best is None or cost < best[0]:
                best = (cost, sg, ty, ex_, tbox)
        _, sg, ty, ex_, tbox = best
        end = ex_ + sg * w
        lines_ += [((ax, ay), (ex_, ty)), ((ex_, ty), (end, ty))]
        d.extra["label"] = {**lb, "elbow": (round(ex_, 2), round(ty, 2)), "end": (round(end, 2), round(ty, 2)),
                            "text_at": (round(min(ex_, end) + 0.5, 2), round(ty + 0.8, 2)), "dir": sg}
        placed.append(tbox)


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
        for d in plan.dims:  # 键槽对称度：箭头与槽宽尺寸线对齐（GB/T 1182 中心要素），框格在断面左侧
            if d.view == "section" and d.kind == "gtol" and (d.extra or {}).get("keyway") == f["name"]:
                y_dim = vy + r + 5
                d.extra = {**d.extra, "sheet_at": (round(vx - hb, 2), round(y_dim, 2)), "normal": (-1, 0),
                           "reach": round(r - hb + 3, 2), "sheet_pos": (round(vx - r - 3, 2), round(y_dim, 2)),
                           "section": v.label}
        for d in plan.dims:
            if d.view != "section" or not d.ref.startswith(f["name"] + "_"):
                continue
            if d.ref.endswith("_w"):
                ym = (y_rim + y_floor) / 2
                ex = {"sheet_pts": [(round(vx - hb, 2), round(ym, 2)), (round(vx + hb, 2), round(ym, 2))],
                      "sheet_pos": (round(vx, 2), round(vy + r + 5, 2)), "orient": "h"}
            elif d.ref.endswith("_t"):  # 尺寸线默认在断面右侧；右侧出图框（断面贴着右框）时放左侧
                xs = vx + r + 6
                if xs + 6 > plan.sheet_size[0] - MARGIN - 2:
                    xs = vx - r - 6
                ex = {"sheet_pts": [(round(vx, 2), round(vy - r, 2)), (round(vx, 2), round(y_floor, 2))],
                      "sheet_pos": (round(xs, 2), round((vy - r + y_floor) / 2, 2)), "orient": "v",
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
        elif d.kind == "callout" and d.at is not None and d.ref.startswith("bend"):
            d.extra = {**(d.extra or {}), **_bend_note(p, int(d.ref[4:]), d.text, F(*d.at), (x0, y0, x1, y1))}
        elif d.kind == "callout" and d.at is not None:
            px, py = F(*d.at)
            d.extra = {**(d.extra or {}), "sheet_pos": (round(px - _tw(d.text) / 2, 2), round(py + 1, 2))}


def _bend_note(p: ResolvedPart, i: int, text: str, at, box) -> dict:
    """展开图折弯说明（上折/下折 角度 R）的图纸位置：写在折弯线中部旁；离外轮廓太近换到线的另一侧；
    展开图内放不下时移到轮廓外——水平折弯线移到右侧（越过展开高度尺寸），竖直折弯线沿线引到上方
    （越过折弯定位尺寸行 y1+10+8k），左半边文字向左写、右半边向右写，配细线引出。
    返回 text_at（文字锚点）、anchor（start/middle/end）、rot（0 / 90 竖写）、leader（细线折线）、
    sheet_pos（文字左下角，供 CAD 注释）。"""
    x0, y0, x1, y1 = box
    bd = p.bends[i]
    tw = text_width(text, 3)
    px, py = at
    lead: list = []
    if bd["flat_axis"] == "v":  # 水平折弯线 y = py
        if tw <= (x1 - x0) - 4:
            tx = min(max(px, x0 + 2 + tw / 2), x1 - 2 - tw / 2)
            ty = py + 1 if y1 - py > 4.6 else py - 3.2
            out = {"text_at": (tx, ty), "anchor": "middle", "rot": 0, "sheet_pos": (tx - tw / 2, ty)}
        else:
            lead = [[(x1 + 1, py), (x1 + 18, py)]]
            out = {"text_at": (x1 + 18.5, py - 1), "anchor": "start", "rot": 0, "sheet_pos": (x1 + 18.5, py - 1)}
    else:  # 竖直折弯线 x = px
        if tw <= (y1 - y0) - 4:
            ty = min(max(py, y0 + 2 + tw / 2), y1 - 2 - tw / 2)
            tx = px - 1 if px - x0 > 4.6 else px + 3.2
            out = {"text_at": (tx, ty), "anchor": "middle", "rot": 90, "sheet_pos": (tx, ty - tw / 2)}
        else:
            n_u = sum(1 for b in p.bends if b["flat_axis"] != "v")
            yn = y1 + 10 + 8 * (n_u - 1) + 7 if n_u else y1 + 5
            left = px < (x0 + x1) / 2
            lead = [[(px, y1 + 1), (px, yn)], [(px - 1 - tw, yn), (px, yn)] if left else [(px, yn), (px + 1 + tw, yn)]]
            tx = px - 1 if left else px + 1
            out = {"text_at": (tx, yn + 1.2), "anchor": "end" if left else "start", "rot": 0,
                   "sheet_pos": (tx - tw if left else tx, yn + 1.2)}
    r2 = lambda q: (round(q[0], 2), round(q[1], 2))  # noqa: E731
    return {**out, "text_at": r2(out["text_at"]), "sheet_pos": r2(out["sheet_pos"]),
            "leader": [[r2(q) for q in seg] for seg in lead]}


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
            ex_ = d.extra or {}
            if d.kind != "linear" or d.at is not None or ex_.get("install") or ex_.get("pitch") or ex_.get("fit"):
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


def view_overlaps(plan: "DrawingPlan", pad: float = 1.0) -> list[tuple[str, str]]:
    """视图本体（包围框）两两重叠的列表：排布回归用（文字冲突检测查不到视图叠在一起）。"""
    bx = []
    for v in plan.views:
        cx, cy = v.origin_on_sheet
        w, h = v.size_mm
        bx.append((f"{v.type}:{v.label or v.target}", (cx - w / 2 + pad, cy - h / 2 + pad, cx + w / 2 - pad, cy + h / 2 - pad)))
    return [(a, b) for i, (a, ba) in enumerate(bx) for b, bb in bx[i + 1:] if _box_hit(ba, bb) > 0]


def spread_1d(xs: list[float], gap: float) -> list[float]:
    """一维摊开：保持顺序，相邻间距 ≥ gap，每串挤在一起的整体以原位置均值为中心。"""
    n = len(xs)
    if n == 0:
        return []
    order = sorted(range(n), key=lambda i: xs[i])
    groups: list[list[int]] = []
    for i in order:
        groups.append([i])
        while len(groups) > 1:  # 与前一串重叠则合并
            g0, g1 = groups[-2], groups[-1]
            c0 = sum(xs[j] for j in g0) / len(g0)
            c1 = sum(xs[j] for j in g1) / len(g1)
            end0 = c0 + gap * (len(g0) - 1) / 2
            start1 = c1 - gap * (len(g1) - 1) / 2
            if start1 - end0 >= gap - 1e-9:
                break
            groups[-2:] = [g0 + g1]
    out = [0.0] * n
    for g in groups:
        c = sum(xs[j] for j in g) / len(g)
        for k, j in enumerate(g):
            out[j] = c + gap * (k - (len(g) - 1) / 2)
    return out


def ordinate_layout(plan: "DrawingPlan", view: ViewPlan, bbox) -> dict[int, float]:
    """坐标尺寸数字沿边线的位置（图纸坐标：横向标注为 x，纵向为 y）：按要素位置摊开，相邻 ≥ 3 mm，
    摊开后的数字用折线引线接到要素位置。返回 id(Dim) → 数字位置；预览与 CAD 共用。"""
    if view.type not in VIEW_AXES:
        return {}
    ha, va = VIEW_AXES[view.type]
    out: dict[int, float] = {}
    for axis, k in ((ha, 0), (va, 1)):
        ds = [d for d in plan.dims if d.view == view.type and d.kind == "ordinate" and d.at is not None and d.axis == axis]
        xs = [_uv_sheet(view, bbox, d.at)[k] for d in ds]
        for d, x in zip(ds, spread_1d(xs, 3.0)):
            out[id(d)] = x
    return out


def ordinate_dim_sheet(plan: "DrawingPlan", p: ResolvedPart) -> None:
    """坐标尺寸的图纸坐标（mm），与 render_svg 的坐标尺寸带一致，供 CAD 原生坐标尺寸选边：
    extra["sheet_pts"] = [基准轮廓边上一点（横向取左边线、纵向取下边线），被标注要素边上一点（孔取圆周最近点）]，
    extra["sheet_pos"] = 数字位置（横向在视图下方、纵向在视图右侧，挤在一起的沿边线摊开），extra["orient"] = h | v。"""
    for v in plan.views:
        if v.type not in VIEW_AXES:
            continue
        ha, va = VIEW_AXES[v.type]
        (ulo, uhi), (wlo, whi) = vrange(p.bbox, ha), vrange(p.bbox, va)
        (x0, y0), (x1, y1) = _uv_sheet(v, p.bbox, (ulo, wlo)), _uv_sheet(v, p.bbox, (uhi, whi))
        mid = _uv_sheet(v, p.bbox, ((ulo + uhi) / 2, (wlo + whi) / 2))
        lay = ordinate_layout(plan, v, p.bbox)
        for d in plan.dims:
            if d.view != v.type or d.kind != "ordinate" or d.at is None:
                continue
            px, py = _uv_sheet(v, p.bbox, d.at)
            r = next((h.max_dia / 2 * v.scale for h in getattr(p, "holes", []) for i in h.instances
                      if abs(vc(i.xyz, ha) - d.at[0]) < 1e-6 and abs(vc(i.xyz, va) - d.at[1]) < 1e-6), 0.0)
            if d.axis == ha:
                pts = [(x0, mid[1]), (px, py + r)]  # 圆周最上点；r=0 时要素点本身就在边上
                pos, orient = (lay.get(id(d), px), y0 - 6.5), "h"
            elif d.axis == va:
                pts = [(mid[0], y0), (px + r, py)]
                pos, orient = (x1 + 5, lay.get(id(d), py)), "v"
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
