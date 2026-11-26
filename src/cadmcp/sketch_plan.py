"""草图驱动尺寸规划（CAD 无关）：让外形草图完全定义，并把尺寸与 spec 的全局变量关联。

输出给适配器逐条执行：先加几何关系，再加尺寸；坐标为草图平面内 (u, v)，mm。
- 拾取项 pick：`[u, v, "seg"]` 草图线段/圆弧上一点，`[u, v, "pt"]` 草图点，`[0, 0, "origin"]` 草图原点。
- relation.kind：`coincident` | `same_x`（两点竖直对齐）| `same_y`（两点水平对齐）| `horizontal` | `vertical`（线段）。
- dim.kind：`length`（线段长）| `diameter` | `radius` | `dx` / `dy`（两点水平/竖直距离）；`place` 为尺寸文字位置。
- dim.link：SW 方程式右侧（含全局变量时才给，如 `"W" - 10`），纯数值尺寸不建方程式。

自由度核算（每种外形都恰好完全定义、不过定义）：
- 矩形（中心矩形自带水平/竖直/中点关系）：宽、高 + 中心定位 2 个（为 0 时用关系代替尺寸）；圆角矩形另加 1 个半径（四角圆角等半径关系由倒圆命令建立）。
- 圆：直径 + 圆心定位 2 个。
- 腰型槽（自带相切/平行/等半径）：两端圆心各定位 2 个 + 端部半径。
- 多边形：轴对齐边加水平/竖直关系；按"竖直边连通的顶点共享 x、水平边连通的顶点共享 y"分组，每组标一个坐标（为 0 时用关系对齐原点）。
"""
from __future__ import annotations

import math
from typing import Any

OFF = 10.0  # 尺寸文字离开轮廓的距离（mm）
EPS = 1e-6


def _link(links: dict[str, Any], key: str, scale: str = "") -> str | None:
    v = links.get(key)
    if v is None or '"' not in v:
        return None
    return f"({v}){scale}" if scale else v


def _locate(pt: tuple[float, float], name: str, place_off: tuple[float, float] = (0.0, 0.0)) -> tuple[list, list]:
    """一个点相对草图原点完全定位：坐标非 0 → dx/dy 尺寸；为 0 → 与原点竖直/水平对齐关系。"""
    u, v = pt
    rels: list[dict[str, Any]] = []
    dims: list[dict[str, Any]] = []
    picks = [[0.0, 0.0, "origin"], [u, v, "pt"]]
    if abs(u) < EPS and abs(v) < EPS:
        return [{"kind": "coincident", "pick": picks}], []
    if abs(u) < EPS:
        rels.append({"kind": "same_x", "pick": picks})
    else:
        dims.append({"name": f"{name}_x", "kind": "dx", "pick": picks, "value": round(abs(u), 6), "link": None,
                     "place": [round(u / 2, 6), round(max(v, 0.0) + OFF + place_off[1], 6)]})
    if abs(v) < EPS:
        rels.append({"kind": "same_y", "pick": picks})
    else:
        dims.append({"name": f"{name}_y", "kind": "dy", "pick": picks, "value": round(abs(v), 6), "link": None,
                     "place": [round(max(u, 0.0) + OFF + place_off[0], 6), round(v / 2, 6)]})
    return rels, dims


def profile_dimension_plan(pr, links: dict[str, Any] | None = None) -> dict[str, list[dict[str, Any]]]:
    """ResolvedProfile → {"relations": [...], "dims": [...]}（见模块说明）。"""
    links = links or {}
    d = pr.data
    rels: list[dict[str, Any]] = []
    dims: list[dict[str, Any]] = []
    if pr.shape == "rect":
        (cx, cy), w, h, r = d["center"], d["width"], d["height"], d["corner_radius"]
        dims.append({"name": "width", "kind": "length", "pick": [[cx, cy + h / 2, "seg"]], "value": w,
                     "link": _link(links, "width"), "place": [cx, cy + h / 2 + OFF]})
        dims.append({"name": "height", "kind": "length", "pick": [[cx + w / 2, cy, "seg"]], "value": h,
                     "link": _link(links, "height"), "place": [cx + w / 2 + OFF, cy]})
        if r > EPS:  # 右上角圆弧中点
            a = math.radians(45)
            ac = (cx + w / 2 - r, cy + h / 2 - r)
            pm = [ac[0] + r * math.cos(a), ac[1] + r * math.sin(a), "seg"]
            dims.append({"name": "corner_radius", "kind": "radius", "pick": [pm], "value": r,
                         "link": _link(links, "corner_radius"), "place": [pm[0] + OFF * 0.7, pm[1] + OFF * 0.7]})
        rr, dd = _locate((cx, cy), "center", (w / 2, h / 2))
    elif pr.shape == "circle":
        (cx, cy), dia = d["center"], d["diameter"]
        a = math.radians(45)
        pm = [cx + dia / 2 * math.cos(a), cy + dia / 2 * math.sin(a), "seg"]
        dims.append({"name": "diameter", "kind": "diameter", "pick": [pm], "value": dia,
                     "link": _link(links, "diameter"), "place": [pm[0] + OFF * 0.7, pm[1] + OFF * 0.7]})
        rr, dd = _locate((cx, cy), "center", (dia / 2, dia / 2))
    elif pr.shape == "slot":
        (cx, cy), L, W, ang = d["center"], d["length"], d["width"], math.radians(d["angle"])
        hx, hy = (L - W) / 2 * math.cos(ang), (L - W) / 2 * math.sin(ang)
        c1, c2 = (cx - hx, cy - hy), (cx + hx, cy + hy)
        pe = [c2[0] + W / 2 * math.cos(ang), c2[1] + W / 2 * math.sin(ang), "seg"]  # 末端圆弧顶点
        dims.append({"name": "width", "kind": "radius", "pick": [pe], "value": W / 2,
                     "link": _link(links, "width", "/2"), "place": [pe[0] + OFF * 0.7, pe[1] + OFF * 0.7]})
        rr, dd = _locate(c1, "end1")
        r2, d2 = _locate(c2, "end2", (0.0, 6.0))
        rr += r2
        dd += d2
    else:  # polygon
        pts = [tuple(p) for p in d["points"]]
        n = len(pts)
        rr, dd = [], []
        xs, ys = list(range(n)), list(range(n))  # 并查集：共享 x / 共享 y 的顶点组

        def find(par, i):
            while par[i] != i:
                par[i] = par[par[i]]
                i = par[i]
            return i

        for i in range(n):
            a, b = pts[i], pts[(i + 1) % n]
            mid = [(a[0] + b[0]) / 2, (a[1] + b[1]) / 2, "seg"]
            if abs(a[1] - b[1]) < EPS:
                rels.append({"kind": "horizontal", "pick": [mid]})
                ys[find(ys, i)] = find(ys, (i + 1) % n)
            elif abs(a[0] - b[0]) < EPS:
                rels.append({"kind": "vertical", "pick": [mid]})
                xs[find(xs, i)] = find(xs, (i + 1) % n)
        seen_x, seen_y = set(), set()
        exprs = links.get("points") or []  # 每个顶点的 [u 方程式, v 方程式]（含全局变量时链接）

        def plink(i: int, k: int, val: float) -> str | None:
            e = exprs[i][k] if i < len(exprs) else None
            if not e or '"' not in e:
                return None
            return e if val > 0 else f"abs({e})"  # 尺寸值恒为正

        for i, (u, v) in enumerate(pts):
            gx, gy = find(xs, i), find(ys, i)
            picks = [[0.0, 0.0, "origin"], [u, v, "pt"]]
            if gx not in seen_x:
                seen_x.add(gx)
                if abs(u) < EPS:
                    rr.append({"kind": "same_x", "pick": picks})
                else:
                    dd.append({"name": f"p{i}_x", "kind": "dx", "pick": picks, "value": round(abs(u), 6), "link": plink(i, 0, u),
                               "place": [round(u / 2, 6), round(min(v, 0.0) - OFF - 6 * (len(seen_x) % 3), 6)]})
            if gy not in seen_y:
                seen_y.add(gy)
                if abs(v) < EPS:
                    rr.append({"kind": "same_y", "pick": picks})
                else:
                    dd.append({"name": f"p{i}_y", "kind": "dy", "pick": picks, "value": round(abs(v), 6), "link": plink(i, 1, v),
                               "place": [round(min(u, 0.0) - OFF - 6 * (len(seen_y) % 3), 6), round(v / 2, 6)]})
    rels += rr
    dims += dd
    for x in dims:
        x["pick"] = [[round(p[0], 6), round(p[1], 6), p[2]] for p in x["pick"]]
        x["place"] = [round(x["place"][0], 6), round(x["place"][1], 6)]
        x["value"] = round(x["value"], 6)
    for x in rels:
        x["pick"] = [[round(p[0], 6), round(p[1], 6), p[2]] for p in x["pick"]]
    return {"relations": rels, "dims": dims}


def dof_check(pr, plan: dict[str, list]) -> int:
    """剩余自由度估算（测试用）：草图实体自由度 − 自带约束 − 规划的关系与尺寸。0 = 完全定义。"""
    shape = pr.shape
    if shape == "rect":
        # 中心矩形：4 线 + 中心点；自带 4 个水平/竖直 + 对角构造线中点 → 剩宽、高、中心 x/y（+ 圆角半径）
        free = 4 + (1 if pr.data["corner_radius"] > EPS else 0)
    elif shape == "circle":
        free = 3
    elif shape == "slot":
        free = 5  # 两个端部圆心 4 + 宽度 1（相切/等半径/平行自带）
    else:
        free = 2 * len(pr.data["points"])
    used = len(plan["dims"])
    for r in plan["relations"]:
        used += 2 if r["kind"] == "coincident" else 1
    return free - used
