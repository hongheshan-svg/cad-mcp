"""企业设计规则（与 CAD 无关，在 IR 解析结果上运行）。

规则来源应由资深工程师维护；这里是首批通用机加工/钣金规则。每条结果：
{rule, severity: error|warning|info, target, message, fix}
error 会阻止 cad_change_commit（由 policy 决定）。
"""
from __future__ import annotations

import math
from typing import Any

from .ir.resolve import ResolvedPart, dist_to_outline, face_depth, face_outline


def _pt_seg(p, a, b) -> float:
    dx, dy = b[0] - a[0], b[1] - a[1]
    L2 = dx * dx + dy * dy
    t = 0.0 if L2 == 0 else max(0.0, min(1.0, ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / L2))
    return math.dist(p, (a[0] + t * dx, a[1] + t * dy))


def _seg_dist(a1, a2, b1, b2) -> float:
    """两线段最短距离（不相交时；平面内孔/腰型孔足够）。"""
    def cross(o, p, q):
        return (p[0] - o[0]) * (q[1] - o[1]) - (p[1] - o[1]) * (q[0] - o[0])
    d1, d2, d3, d4 = cross(b1, b2, a1), cross(b1, b2, a2), cross(a1, a2, b1), cross(a1, a2, b2)
    if ((d1 > 0) != (d2 > 0)) and ((d3 > 0) != (d4 > 0)):
        return 0.0
    return min(_pt_seg(a1, b1, b2), _pt_seg(a2, b1, b2), _pt_seg(b1, a1, a2), _pt_seg(b2, a1, a2))

Finding = dict[str, Any]


def _f(rule: str, severity: str, target: str, message: str, fix: str | None = None) -> Finding:
    d = {"rule": rule, "severity": severity, "target": target, "message": message}
    if fix:
        d["fix"] = fix
    return d


def check_part(p: ResolvedPart) -> list[Finding]:
    out: list[Finding] = []
    # R001 属性完整性
    for key, val in (("part_no", p.part_no), ("title", p.title), ("material", p.material)):
        if not val:
            out.append(_f("R001", "error", p.name, f"缺少 {key}", f"在 spec 中填写 {key}"))
    if p.category in ("machined", "sheet_metal", "weldment") and not p.surface_treatment:
        out.append(_f("R001", "warning", p.name, "未指定表面处理", "填写 surface_treatment，如 '阳极氧化黑'/'镀锌'/'无'"))

    out += [dict(f) for f in p.weld_findings]  # W001–W005 焊接件规则（解析时生成）
    # R002/R003 孔边距、孔是否破边
    for h in p.holes:
        outline = face_outline(h.face, h.plate_box or p.bbox, None if h.plate_box else p.profile, h.at,
                               extrude=p.base_type == "extrude")
        min_wall = max(2.0, 0.5 * h.nominal)
        for inst in h.instances:
            p1, p2, rad = h.capsule(inst)
            # 凸轮廓内，到边界距离沿线段是凹函数 → 最小值在端点
            wall = min(dist_to_outline(p1, outline), dist_to_outline(p2, outline)) - rad
            tgt = f"{h.name}[{inst.index}]"
            if wall < 0:
                out.append(_f("R003", "error", tgt, f"孔超出零件轮廓 (壁厚 {wall:.2f} mm)", "调整孔位置或零件外形"))
            elif wall < min_wall:
                out.append(_f("R002", "warning", tgt, f"孔边距不足：壁厚 {wall:.2f} mm < {min_wall:.1f} mm",
                              f"孔中心离边至少 {h.max_dia / 2 + min_wall:.1f} mm"))
        # R004 盲孔深度
        if not h.through:
            extent = face_depth(h.face, h.plate_box or p.bbox, h.at)
            if h.depth >= extent - 0.5:
                out.append(_f("R004", "error", h.name, f"盲孔深度 {h.depth} 接近/超过料厚 {extent}",
                              "改为 depth='through' 或减小深度"))
        # R005 螺纹有效长度
        if h.type == "tapped" and h.thread_depth is not None:
            k = 2.0 if p.material.startswith("AL") or p.material == "POM" else 1.5
            if h.thread_depth < k * h.nominal * 0.75:
                out.append(_f("R005", "warning", h.name,
                              f"螺纹深度 {h.thread_depth:g} 偏浅（{p.material} 建议 ≥ {k * h.nominal * 0.75:g}）",
                              "加深螺纹或改用通孔+螺母"))
    # R006 孔间距
    all_inst = [(h, i) for h in p.holes for i in h.instances]
    for a in range(len(all_inst)):
        for b in range(a + 1, len(all_inst)):
            (ha, ia), (hb, ib) = all_inst[a], all_inst[b]
            if ia.face != ib.face or ha.at != hb.at:
                continue
            a1, a2, ra = ha.capsule(ia)
            b1, b2, rb = hb.capsule(ib)
            wall = _seg_dist(a1, a2, b1, b2) - ra - rb
            tgt = f"{ha.name}[{ia.index}]~{hb.name}[{ib.index}]"
            if wall < 0:
                out.append(_f("R006", "error", tgt, "两孔重叠", "调整孔位"))
            elif wall < 2.0:
                out.append(_f("R006", "warning", tgt, f"孔间壁厚 {wall:.2f} mm < 2 mm"))
    # R007 型腔底厚
    for pk in p.pockets:
        if not pk["through"]:
            floor = face_depth(pk["face"], p.bbox, pk.get("at")) - pk["depth"]
            if floor < 0:
                out.append(_f("R007", "error", pk["name"], "型腔深度超过料厚", "改为 through 或减小深度"))
            elif floor < 1.5:
                out.append(_f("R007", "warning", pk["name"], f"型腔底厚 {floor:.2f} mm < 1.5 mm"))
    # R008 钣金：孔到折弯边距离 ≥ 2t + r
    if p.base_type == "sheet_metal" and p.thickness:
        for bend in p.bends:
            limit = 2 * p.thickness + bend["radius"]
            for h in p.holes:
                if h.face == bend["edge"]:  # 翻边上的孔：孔边到底板内表面 ≥ 2t + r
                    for inst in h.instances:
                        hgt = inst.xyz[2] if bend["direction"] == "up" else p.thickness - inst.xyz[2]
                        d = hgt - p.thickness - h.max_dia / 2
                        if d < limit:
                            out.append(_f("R008", "warning", f"{h.name}[{inst.index}]",
                                          f"翻边孔离折弯 {d:.2f} mm < 2t+r={limit:.2f} mm，折弯会拉变形",
                                          "提高孔位或先折弯后加工"))
                    continue
                if h.face not in ("top", "bottom"):
                    continue
                for inst in h.instances:
                    u, v = inst.uv
                    d = {"front": v - p.bbox.ymin, "back": p.bbox.ymax - v,
                         "left": u - p.bbox.xmin, "right": p.bbox.xmax - u}[bend["edge"]] - h.max_dia / 2
                    if d < limit:
                        out.append(_f("R008", "warning", f"{h.name}[{inst.index}]",
                                      f"孔离 {bend['edge']} 折弯 {d:.2f} mm < 2t+r={limit:.2f} mm，折弯会拉变形"))
    out += _check_shaft(p)
    out += _check_precision(p)
    return out


IT_GRADE_RA = {5: 0.4, 6: 0.8, 7: 1.6, 8: 1.6, 9: 3.2}  # 配合精度 → 建议最大 Ra（μm）


def _check_precision(p: ResolvedPart) -> list[Finding]:
    from .ir.part import FORM_TOLS
    out: list[Finding] = []
    letters = [d["name"] for d in p.datums]
    for x in {n for n in letters if letters.count(n) > 1}:
        out.append(_f("G003", "error", f"datum {x}", "基准字母重复", "每个基准用不同字母"))
    for g in p.geotols:
        tgt = f"{g['type']}@{g['target']}"
        for x in g["datums"]:
            if x not in letters:
                out.append(_f("G001", "error", tgt, f"引用了未定义的基准 {x}", f"在 datums 中定义 {x}"))
        if g["type"] in FORM_TOLS and g["datums"]:
            out.append(_f("G002", "warning", tgt, "形状公差不应带基准", "去掉 datums"))
        elif g["type"] not in FORM_TOLS and not g["datums"]:
            out.append(_f("G002", "error", tgt, "方向/位置/跳动公差必须指定基准", "填写 datums"))
        if any(d["target"] == g["target"] for d in p.datums if d["name"] in g["datums"]):
            out.append(_f("G005", "error", tgt, "公差以自身为基准", "改用其他基准"))
    # 配合轴段/孔的粗糙度：IT6/IT7 配合面必须标细粗糙度
    ra_of = {r["target"]: r["ra"] for r in p.roughness}
    for h in p.holes:  # 配合孔（H7 等）
        digits = "".join(ch for ch in (h.tolerance or "") if ch.isdigit())
        need = IT_GRADE_RA.get(int(digits)) if digits else None
        got = ra_of.get(f"hole:{h.name}")
        if need and (got is None or got > need):
            out.append(_f("G004", "warning", f"hole:{h.name}",
                          f"{h.name} 为 {h.tolerance} 配合孔，粗糙度{'未标注' if got is None else f' Ra{got:g} 偏粗'}",
                          f"在 roughness 中加 {{'target': 'hole:{h.name}', 'ra': {need:g}}}"))
    for i, tol in enumerate(p.shaft_tolerances):
        if not tol:
            continue
        digits = "".join(ch for ch in tol if ch.isdigit())
        if not digits:
            continue
        need = IT_GRADE_RA.get(int(digits))
        got = ra_of.get(f"step:{i}")
        if need and (got is None or got > need):
            out.append(_f("G004", "warning", f"step:{i}",
                          f"轴段 {i} 为 {tol} 配合，粗糙度{'未标注' if got is None else f' Ra{got:g} 偏粗'}",
                          f"在 roughness 中加 {{'target': 'step:{i}', 'ra': {need:g}}}"))
    return out


def _check_shaft(p: ResolvedPart) -> list[Finding]:
    from .standards import KEY_LENGTHS
    out: list[Finding] = []
    for f in p.shaft_features:
        z0, z1 = p.step_z(f["step"])
        if f["z0"] < z0 - 1e-6 or f["z1"] > z1 + 1e-6:
            out.append(_f("S002", "error", f["name"], f"{f.get('callout', f['name'])} 超出所在轴段 {f['step']}（{z0:g}–{z1:g}）",
                          "缩短长度或调整 offset/position"))
            continue
        if f["kind"] == "keyway":
            if f["length"] not in KEY_LENGTHS:
                near = min(KEY_LENGTHS, key=lambda x: abs(x - f["length"]))
                out.append(_f("S001", "warning", f["name"], f"键槽长 {f['length']:g} 不在 GB/T 1096 长度系列",
                              f"改为 {near}"))
            margin = min(f["z0"] - z0, z1 - f["z1"])
            if margin < 2 and not (f["z0"] - z0 < 2 and f["step"] == 0 or z1 - f["z1"] < 2 and f["step"] == len(p.shaft_steps) - 1):
                out.append(_f("S003", "warning", f["name"], f"键槽距轴肩 {margin:g} mm < 2 mm，铣刀会碰轴肩",
                              "键槽两端距轴肩留 2–5 mm"))
    feats = sorted(p.shaft_features, key=lambda f: f["z0"])
    for a, b in zip(feats, feats[1:]):
        if b["z0"] < a["z1"] - 1e-6:
            out.append(_f("S004", "error", f"{a['name']}~{b['name']}", "轴上特征沿轴向重叠", "调整位置"))
    for i, ((L, d), (L2, d2)) in enumerate(zip(p.shaft_steps, p.shaft_steps[1:])):
        tol = p.shaft_tolerances
        if tol and (tol[i] or tol[i + 1]) and abs(d - d2) < 1e-6:
            out.append(_f("S005", "info", f"step{i}/{i + 1}", "相邻轴段同径但公差不同，加工时需分界线", None))
    return out


def summarize(findings: list[Finding]) -> dict[str, Any]:
    errs = sum(1 for f in findings if f["severity"] == "error")
    warns = sum(1 for f in findings if f["severity"] == "warning")
    return {"passed": errs == 0, "errors": errs, "warnings": warns, "findings": findings}
