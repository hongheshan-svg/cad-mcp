"""企业设计规则（与 CAD 无关，在 IR 解析结果上运行）。

规则来源应由资深工程师维护；这里是首批通用机加工/钣金规则。每条结果：
{rule, severity: error|warning|info, target, message, fix}
error 会阻止 cad_change_commit（由 policy 决定）。
"""
from __future__ import annotations

import math
from typing import Any

from .ir.resolve import ResolvedPart, dist_to_outline, face_outline

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
    if p.category in ("machined", "sheet_metal") and not p.surface_treatment:
        out.append(_f("R001", "warning", p.name, "未指定表面处理", "填写 surface_treatment，如 '阳极氧化黑'/'镀锌'/'无'"))

    # R002/R003 孔边距、孔是否破边
    for h in p.holes:
        outline = face_outline(h.face, p.bbox, p.profile)
        min_wall = max(2.0, 0.5 * h.nominal)
        for inst in h.instances:
            wall = dist_to_outline(inst.uv, outline) - h.max_dia / 2
            tgt = f"{h.name}[{inst.index}]"
            if wall < 0:
                out.append(_f("R003", "error", tgt, f"孔超出零件轮廓 (壁厚 {wall:.2f} mm)", "调整孔位置或零件外形"))
            elif wall < min_wall:
                out.append(_f("R002", "warning", tgt, f"孔边距不足：壁厚 {wall:.2f} mm < {min_wall:.1f} mm",
                              f"孔中心离边至少 {h.max_dia / 2 + min_wall:.1f} mm"))
        # R004 盲孔深度
        if not h.through:
            extent = p.bbox.extent(h.face)
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
            if ia.face != ib.face:
                continue
            wall = math.dist(ia.uv, ib.uv) - (ha.max_dia + hb.max_dia) / 2
            tgt = f"{ha.name}[{ia.index}]~{hb.name}[{ib.index}]"
            if wall < 0:
                out.append(_f("R006", "error", tgt, "两孔重叠", "调整孔位"))
            elif wall < 2.0:
                out.append(_f("R006", "warning", tgt, f"孔间壁厚 {wall:.2f} mm < 2 mm"))
    # R007 型腔底厚
    for pk in p.pockets:
        if not pk["through"]:
            floor = p.bbox.extent(pk["face"]) - pk["depth"]
            if floor < 0:
                out.append(_f("R007", "error", pk["name"], "型腔深度超过料厚", "改为 through 或减小深度"))
            elif floor < 1.5:
                out.append(_f("R007", "warning", pk["name"], f"型腔底厚 {floor:.2f} mm < 1.5 mm"))
    # R008 钣金：孔到折弯边距离 ≥ 2t + r
    if p.base_type == "sheet_metal" and p.thickness:
        for bend in p.bends:
            limit = 2 * p.thickness + bend["radius"]
            for h in p.holes:
                if h.face not in ("top", "bottom"):
                    continue
                for inst in h.instances:
                    u, v = inst.uv
                    d = {"front": v - p.bbox.ymin, "back": p.bbox.ymax - v,
                         "left": u - p.bbox.xmin, "right": p.bbox.xmax - u}[bend["edge"]] - h.max_dia / 2
                    if d < limit:
                        out.append(_f("R008", "warning", f"{h.name}[{inst.index}]",
                                      f"孔离 {bend['edge']} 折弯 {d:.2f} mm < 2t+r={limit:.2f} mm，折弯会拉变形"))
    return out


def summarize(findings: list[Finding]) -> dict[str, Any]:
    errs = sum(1 for f in findings if f["severity"] == "error")
    warns = sum(1 for f in findings if f["severity"] == "warning")
    return {"passed": errs == 0, "errors": errs, "warnings": warns, "findings": findings}
