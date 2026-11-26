"""出图规划（与 CAD 无关）：把"工程师出图时的决定"程序化。

输入 DrawingSpec + 解析后的零件/装配，输出 DrawingPlan：图幅、比例、视图布局、
尺寸方案（总体尺寸 + 孔坐标尺寸 + 孔标注）、技术要求、标题栏、明细表。
适配器只负责把 DrawingPlan 渲染成原生工程图（SW .SLDDRW / Creo .drw），
并在渲染后由 check_drawing 校验"该标的都标了"。
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date
from typing import Any

from .ir.assembly_resolve import ResolvedAssembly
from .ir.drawing import DrawingSpec
from .ir.resolve import Box, ResolvedPart

SHEETS = {"A4": (297, 210), "A3": (420, 297), "A2": (594, 420), "A1": (841, 594), "A0": (1189, 841)}
# GB/T 14690 优先比例
SCALES = [5.0, 2.0, 1.0, 0.5, 0.4, 0.25, 0.2, 0.1, 0.05, 0.02]  # 含允许比例 1:2.5、1:4
PRIMARY_SCALES = [5.0, 2.0, 1.0, 0.5, 0.2, 0.1, 0.05, 0.02]  # GB/T 14690 优先比例（零件图）
TITLE_BLOCK_H = 56  # 标题栏占用高度（mm，GB/T 10609.1 简化）
MARGIN = 10

# 视图 → (水平轴, 竖直轴)，基于零件坐标系
VIEW_AXES = {
    "front": ("x", "z"), "back": ("x", "z"),
    "top": ("x", "y"), "bottom": ("x", "y"),
    "left": ("y", "z"), "right": ("y", "z"),
}
HOLE_FACE_VIEW = {"top": "top", "bottom": "bottom", "front": "front", "back": "back", "left": "left", "right": "right"}


def scale_label(s: float) -> str:
    return f"{int(s)}:1" if s >= 1 else f"1:{int(round(1 / s))}"


def _extent(b: Box, axis: str) -> float:
    return {"x": b.xmax - b.xmin, "y": b.ymax - b.ymin, "z": b.zmax - b.zmin}[axis]


@dataclass
class Dim:
    view: str
    kind: str  # linear | ordinate | diameter | callout
    axis: str | None
    value: float | None
    text: str
    ref: str  # 被标注对象，用于校验
    at: tuple[float, float] | None = None  # 模型坐标（视图平面内）


@dataclass
class ViewPlan:
    type: str
    scale: float
    size_mm: tuple[float, float]  # 图纸上的尺寸
    origin_on_sheet: tuple[float, float]  # 视图中心在图纸上的位置


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
    flat_pattern: tuple[float, float] | None = None
    name: str = ""  # 图纸文件名（默认 <model>_DRW）

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["scale_label"] = scale_label(self.scale)
        return d


ISO_FACTOR = 0.5  # 轴测图按半比例放置（辅助视图）
MIN_FILL = {"part": 0.28, "assembly": 0.15}  # 视图占可用区域的最低比例（装配图需给明细表留位）
MIN_FEATURE_ON_PAPER = 2.0  # 最小孔在图纸上至少 2 mm


def _view_size(b: Box, vtype: str, flat: tuple[float, float] | None) -> tuple[float, float]:
    if vtype == "flat_pattern" and flat:
        return flat
    if vtype == "iso":
        sx, sy, sz = b.size
        return (0.87 * (sx + sy) * ISO_FACTOR, (0.5 * (sx + sy) + sz) * ISO_FACTOR)
    h, v = VIEW_AXES[vtype]
    return (_extent(b, h), _extent(b, v))


# 投影布局（GB 第一角）：主视图左上，俯视图在其正下方，侧视图在其右侧，轴测/展开图右下
PREFERRED_CELL = {"front": (0, 0), "back": (0, 0), "top": (0, 1), "bottom": (0, 1),
                  "left": (1, 0), "right": (1, 0), "iso": (1, 1), "flat_pattern": (1, 1)}


def _assign_cells(types: list[str]) -> list[tuple[int, int]]:
    taken: set[tuple[int, int]] = set()
    out = []
    for t in types:
        cell = PREFERRED_CELL.get(t, (0, 0))
        if cell in taken:
            n = 0
            while (n % 2, n // 2) in taken:
                n += 1
            cell = (n % 2, n // 2)
        taken.add(cell)
        out.append(cell)
    return out


def _layout(b: Box, spec: DrawingSpec, flat, sheet: str | None, min_feature: float = 0.0, kind: str = "part"):
    """选图幅和比例：2 列 × N 行网格，视图间留 30mm 标注空间。
    策略：图幅从小到大，每个图幅取能放下的最大优先比例；填充率和最小特征可读性都达标才采用，
    否则取所有候选中比例最大的（同比例取小图幅）。"""
    types = [v.type for v in spec.views]
    sizes = [_view_size(b, t, flat) for t in types]
    cells = _assign_cells(types)
    cols = max(c for c, _ in cells) + 1
    rows = max(r for _, r in cells) + 1
    col_w = [max((sizes[i][0] for i in range(len(types)) if cells[i][0] == c), default=0) for c in range(cols)]
    row_h = [max((sizes[i][1] for i in range(len(types)) if cells[i][1] == r), default=0) for r in range(rows)]
    gap = 30
    sheets = [sheet] if sheet and sheet != "auto" else list(SHEETS)
    candidates: list[tuple[str, float]] = []
    chosen: tuple[str, float] | None = None
    for sh in sheets:
        W, H = SHEETS[sh]
        avail_w, avail_h = W - 2 * MARGIN - gap * cols, H - 2 * MARGIN - TITLE_BLOCK_H - gap * rows
        scales = PRIMARY_SCALES if kind == "part" else SCALES
        s = next((s for s in scales if sum(col_w) * s <= avail_w and sum(row_h) * s <= avail_h), None)
        if s is None:
            continue
        candidates.append((sh, s))
        fill = (sum(col_w) * s * sum(row_h) * s) / (avail_w * avail_h)
        if fill >= MIN_FILL[kind] and (min_feature <= 0 or min_feature * s >= MIN_FEATURE_ON_PAPER):
            chosen = (sh, s)
            break
    if chosen is None:
        if not candidates:
            sh, s = sheets[-1], SCALES[-1]
            return sh, s, [ViewPlan(t, s, (sz[0] * s, sz[1] * s), (0, 0)) for t, sz in zip(types, sizes)]
        chosen = max(candidates, key=lambda c: (c[1], -list(SHEETS).index(c[0])))
    sh, s = chosen
    W, H = SHEETS[sh]
    views = []
    for i, t in enumerate(types):
        c, r = cells[i]
        cx = MARGIN + gap / 2 + sum(col_w[:c]) * s + gap * c + col_w[c] * s / 2
        cy = H - MARGIN - gap / 2 - sum(row_h[:r]) * s - gap * r - row_h[r] * s / 2
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


def plan_part_drawing(spec: DrawingSpec, p: ResolvedPart, today: date | None = None) -> DrawingPlan:
    b = p.bbox
    min_feat = min((h.drill for h in p.holes), default=0.0)
    sheet, scale, views = _layout(b, spec, p.flat_pattern, spec.sheet, min_feat)
    vtypes = {v.type for v in views}
    dims: list[Dim] = []
    if spec.dimensions != "none":
        # 总体尺寸：每个轴在第一个能看到它的视图里标一次
        done: set[str] = set()
        for v in views:
            if v.type not in VIEW_AXES:
                continue
            for axis in VIEW_AXES[v.type]:
                if axis in done:
                    continue
                if p.base_type == "shaft" and axis in ("x", "y"):
                    continue
                dims.append(Dim(v.type, "linear", axis, round(_extent(b, axis), 3), f"{_extent(b, axis):g}", f"overall_{axis}"))
                done.add(axis)
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
        ordinates: dict[tuple[str, str, float], Dim] = {}
        located: set[str] = set()
        for h in p.holes:
            view = HOLE_FACE_VIEW[h.face]
            if view not in vtypes:
                view = next((v.type for v in views if v.type in VIEW_AXES), views[0].type)
            ha, va = VIEW_AXES.get(view, ("x", "y"))
            origin = {"x": b.xmin, "y": b.ymin, "z": b.zmin}
            if h.circular:  # 圆周均布：标分布圆中心坐标 + 分布圆直径，不逐孔标坐标
                c = h.circular
                from .ir.resolve import face_point
                cxyz = face_point(h.face, (c["center_u"], c["center_v"]), b)
                ptc = {"x": cxyz[0], "y": cxyz[1], "z": cxyz[2]}
                for ax in (ha, va):
                    val = round(ptc[ax] - origin[ax], 3)
                    ordinates.setdefault((view, ax, val), Dim(view, "ordinate", ax, val, f"{val:g}",
                                                              f"ord:{view}.{ax}={val:g}", (ptc[ha], ptc[va])))
                dims.append(Dim(view, "diameter", None, c["pcd"], f"Φ{c['pcd']:g} 分布圆", f"{h.name}.pcd",
                                (ptc[ha] + c["pcd"] / 2 * 0.7071, ptc[va] - c["pcd"] / 2 * 0.7071)))
                for inst in h.instances:
                    located.update({f"{h.name}[{inst.index}].{ha}", f"{h.name}[{inst.index}].{va}"})
            for inst in ([] if h.circular else h.instances):
                pt = {"x": inst.xyz[0], "y": inst.xyz[1], "z": inst.xyz[2]}
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
                                ({"x": first.xyz[0], "y": first.xyz[1], "z": first.xyz[2]}[ha],
                                 {"x": first.xyz[0], "y": first.xyz[1], "z": first.xyz[2]}[va])))
        if ordinates:
            for view in {k[0] for k in ordinates}:
                ha, va = VIEW_AXES.get(view, ("x", "y"))
                lo = {"x": b.xmin, "y": b.ymin, "z": b.zmin}
                for ax in (ha, va):
                    ordinates.setdefault((view, ax, 0.0), Dim(view, "ordinate", ax, 0.0, "0", f"ord:{view}.{ax}=0",
                                                              (lo[ha], lo[va])))
            dims += sorted(ordinates.values(), key=lambda d: (d.view, d.axis, d.value))
        for pk in p.pockets:
            pr = pk["profile"]
            txt = f"{pr.bbox2d[1] - pr.bbox2d[0]:g}×{pr.bbox2d[3] - pr.bbox2d[2]:g}" + ("" if pk["through"] else f" 深{pk['depth']:g}")
            dims.append(Dim(HOLE_FACE_VIEW[pk["face"]], "callout", None, None, txt, pk["name"]))
        for f in p.finishes:
            txt = f"C{f['size']:g}" if f["type"] == "chamfer" else f"R{f['size']:g}"
            dims.append(Dim("front" if "front" in vtypes else views[0].type, "callout", None, None, txt, f["name"]))

    notes = ["技术要求："]
    notes.append(f"1. 未注尺寸公差按 {spec.general_tolerance}。")
    notes.append("2. 锐边倒钝，去毛刺。")
    n = 3
    if p.surface_treatment:
        notes.append(f"{n}. 表面处理：{p.surface_treatment}。")
        n += 1
    if p.base_type == "sheet_metal" and p.bends:
        notes.append(f"{n}. 未注折弯半径 R{p.bends[0]['radius']:g}，K 因子按工艺卡。")
        n += 1
    for extra in _dedupe_notes(spec.notes, auto=notes, treatment=p.surface_treatment):
        notes.append(f"{n}. {extra}")
        n += 1

    tb = {
        "name": p.title, "part_no": p.part_no, "material": p.material, "scale": scale_label(scale),
        "mass": f"{p.mass_kg:.3f} kg", "sheet": sheet, "projection": "第一角" if spec.projection == "first_angle" else "第三角",
        "surface_finish": spec.surface_finish or "", "designer": "", "checker": "", "approver": "",
        "date": (today or date.today()).isoformat(),
    }
    tb.update(spec.title_block)
    return DrawingPlan(p.name, "part", sheet, SHEETS[sheet], scale, spec.projection, views, dims, notes, tb,
                       flat_pattern=p.flat_pattern)


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
    bom = a.bom() if spec.bom_table else []
    balloons = [{"item": r["item"], "instance": r["instances"][0]} for r in bom] if spec.balloons else []
    notes = ["技术要求：", "1. 装配前零件清洗去毛刺。", "2. 螺钉按规定扭矩拧紧，重要连接涂螺纹胶。"]
    notes += [f"{i}. {t}" for i, t in enumerate(spec.notes, 3)]
    tb = {"name": a.title, "part_no": a.part_no, "material": "", "scale": scale_label(scale),
          "mass": f"{a.mass_kg:.2f} kg", "sheet": sheet,
          "projection": "第一角" if spec.projection == "first_angle" else "第三角",
          "designer": "", "checker": "", "approver": "", "date": (today or date.today()).isoformat()}
    tb.update(spec.title_block)
    return DrawingPlan(a.name, "assembly", sheet, SHEETS[sheet], scale, spec.projection, views, dims, notes, tb,
                       bom=bom, balloons=balloons)


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
    if plan.model_kind == "part" and not any(d.kind == "linear" for d in plan.dims):
        f("D006", "error", "缺少总体尺寸")
    if plan.model_kind == "assembly" and not plan.bom:
        f("D007", "warning", "装配图没有明细表")
    if plan.model_kind == "part" and not any("公差" in n for n in plan.notes):
        f("D008", "warning", "缺少未注公差说明")
    return out
