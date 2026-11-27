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
    # R012 孔骑在型腔侧壁上：同面的孔一部分落在型腔底、一部分落在原表面，钻头单边切削会偏斜，孔口也残缺
    for pk in p.pockets:
        if pk["face"] not in ("top", "bottom") or pk.get("at") is not None:
            continue
        poly = list(pk["profile"].outline)
        for h in p.holes:
            if h.face != pk["face"] or h.at is not None:
                continue
            r = h.max_dia / 2
            for inst in h.instances:
                d = dist_to_outline(inst.uv, poly)  # 型腔内为正
                if d >= r and not pk["through"] and not h.through:
                    # R013 孔在型腔底面内但深度仍从原表面算起：未到型腔底则孔落空；超过则图上孔深/螺纹深与实际
                    # （从型腔底量起）不符，螺纹的一段也落在铣空处
                    floor = p.bbox.zmax - pk["depth"] if pk["face"] == "top" else p.bbox.zmin + pk["depth"]
                    what = "未到型腔底，孔实际不存在" if h.depth <= pk["depth"] + 1e-9 else \
                        f"实际从型腔底只深 {h.depth - pk['depth']:g}，标注孔深与实际不符"
                    out.append(_f("R013", "error", f"{h.name}[{inst.index}]",
                                  f"孔在型腔 {pk['name']} 底面内，深度 {h.depth:g} 却从原表面算起（型腔深 {pk['depth']:g}），{what}",
                                  f"孔改从型腔底面起算：加 at: {floor:g}（同一组孔须都在型腔底内，否则拆成两组）"))
                    continue
                if -r - 1.0 < d < r:
                    out.append(_f("R012", "error" if abs(d) < r else "warning", f"{h.name}[{inst.index}]",
                                  f"孔骑在型腔 {pk['name']} 的侧壁上（孔心距型腔边 {d:+.2f} mm，孔半径 {r:g}）"
                                  if abs(d) < r else f"孔离型腔 {pk['name']} 侧壁仅 {-d - r:.2f} mm < 1 mm",
                                  "孔整个放进型腔底面内，或离型腔边 ≥ 1 mm"))
    # R009 深孔：孔深/钻径 > 10 普通麻花钻难加工（排屑、偏斜）
    for h in p.holes:
        if h.slot:
            continue
        L = face_depth(h.face, h.plate_box or p.bbox, h.at) if h.through else h.depth
        if h.drill > 0 and L / h.drill > 10:
            out.append(_f("R009", "warning", h.name, f"孔深/孔径 = {L:g}/{h.drill:g} = {L / h.drill:.0f} > 10，属深孔，钻削易偏斜",
                          "加大孔径、两面对钻，或改为台阶孔减少有效深度"))
        # R010 盲螺孔：底孔须比螺纹深出 ≥ 0.5d（丝锥头部不完整牙 + 排屑）
        if h.type == "tapped" and not h.through and h.thread_depth is not None and \
                h.depth - h.thread_depth < 0.5 * h.nominal - 1e-6:
            out.append(_f("R010", "warning", h.name,
                          f"盲螺孔底孔深 {h.depth:g} 只比螺纹深 {h.thread_depth:g} 多 {h.depth - h.thread_depth:g} mm"
                          f"（建议 ≥ 0.5d = {0.5 * h.nominal:g}）", "加深底孔或减小螺纹深度"))
    # R011 型腔内角：直角内角铣不出；圆角半径过小时刀具长径比过大
    for pk in ([] if p.casting else p.pockets):  # 铸件型腔是铸出的（铸造圆角归铸造规则管）
        pr = pk["profile"]
        if pr.shape != "rect":
            continue
        r = float(pr.data.get("corner_radius") or 0.0)
        depth = face_depth(pk["face"], p.bbox, pk.get("at")) if pk["through"] else pk["depth"]
        if r <= 0:
            out.append(_f("R011", "warning", pk["name"], "矩形型腔内角为直角，立铣刀加工不出（会留刀具半径圆角）",
                          f"给 profile 加 corner_radius（建议 ≥ 深度/6 = {depth / 6:.1f}，且取常用刀具半径）"))
        elif depth / (2 * r) > 3 + 1e-9:
            out.append(_f("R011", "info", pk["name"],
                          f"型腔内角 R{r:g}、深 {depth:g}：刀具长径比 {depth / (2 * r):.1f} > 3，需加长刀、效率低",
                          f"内角加大到 R{depth / 6:.1f} 以上"))
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


from .precision import IT_GRADE_RA  # noqa: E402  配合精度 → 建议最大 Ra（μm）


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
    for k, b in enumerate(p.shaft_bores):  # 配合内孔（H7 等）同样要注粗糙度
        digits = "".join(ch for ch in (b["tol"] or "") if ch.isdigit())
        need = IT_GRADE_RA.get(int(digits)) if digits else None
        got = ra_of.get(f"bore:{k}")
        if need and (got is None or got > need):
            out.append(_f("G004", "warning", f"bore:{k}",
                          f"内孔 Φ{b['d']:g}{b['tol']} 粗糙度{'未标注' if got is None else f' Ra{got:g} 偏粗'}",
                          f"在 roughness 中加 {{'target': 'bore:{k}', 'ra': {need:g}}}"))
    # 轴上配合段（IT5–IT7）不注几何公差：装配后跳动无从检验（审图常见退回项）
    fits = [i for i, t in enumerate(p.shaft_tolerances)
            if t and any(ch.isdigit() for ch in t) and int("".join(ch for ch in t if ch.isdigit())) <= 7]
    fits += [f["step"] for f in p.shaft_features if f["kind"] == "gear" and f["step"] not in fits]
    toled = {g["target"] for g in p.geotols}
    bare = [i for i in fits if f"step:{i}" not in toled]
    if len(fits) >= 2 and bare:
        a, b = fits[0], fits[-1]
        out.append(_f("G006", "warning", ",".join(f"step:{i}" for i in bare),
                      f"轴段 {', '.join(map(str, bare))} 为配合段（含齿轮段），未注几何公差",
                      f"常用注法：基准 A = step:{a}、B = step:{b}（两端轴承位），轴承位注圆柱度；"
                      f"其余配合段/齿顶圆注对 A、B 的径向圆跳动（circular_runout，datums [A, B]）"))
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
    out += _check_shaft_design(p)
    for i, ((L, d), (L2, d2)) in enumerate(zip(p.shaft_steps, p.shaft_steps[1:])):
        tol = p.shaft_tolerances
        if tol and (tol[i] or tol[i + 1]) and abs(d - d2) < 1e-6:
            out.append(_f("S005", "info", f"step{i}/{i + 1}", "相邻轴段同径但公差不同，加工时需分界线", None))
    return out


def _check_shaft_design(p: ResolvedPart) -> list[Finding]:
    """轴类结构审查（审图经验，CAD 无关）：
    S006 轴承定位轴肩过低；S007 横孔开在配合面上；S008 磨削配合面靠轴肩无越程槽/圆角；
    S009 螺纹车到轴肩无退刀槽；S010 键槽段无配合公差。"""
    import re
    out: list[Finding] = []
    steps = p.shaft_steps
    tols = p.shaft_tolerances or [None] * len(steps)
    n = len(steps)

    def grade(t):
        m = re.fullmatch(r"([a-z]{1,2})(\d{1,2})", (t or "").strip())
        return (m.group(1), int(m.group(2))) if m else None
    keyed = {f["step"] for f in p.shaft_features if f["kind"] == "keyway"}
    grooves = [f for f in p.shaft_features if f["kind"] == "groove"]
    fillet = bool(p.shoulder_fillets)

    def groove_at(i, z):  # 第 i 段在 z（轴肩处）有紧靠的环槽
        return any(g["step"] == i and min(abs(g["z0"] - z), abs(g["z1"] - z)) < 1e-6 for g in grooves)
    for i, (L, d) in enumerate(steps):
        g = grade(tols[i])
        z0, z1 = p.step_z(i)
        for j, zs in ((i - 1, z0), (i + 1, z1)):  # 相邻较大段的轴肩
            if not 0 <= j < n or steps[j][1] <= d:
                continue
            h = (steps[j][1] - d) / 2
            if g and g[0] in ("js", "k", "m", "n") and g[1] <= 6 and i not in keyed and h < max(0.07 * d, 2.0):
                out.append(_f("S006", "warning", f"step{i}",
                              f"轴承位 Φ{d:g}{tols[i]} 的定位轴肩高 {h:g} mm 偏低（经验 ≥ max(0.07d, 2) = {max(0.07 * d, 2.0):.1f}）",
                              f"把第 {j} 段直径加大到 ≥ Φ{d + 2 * max(0.07 * d, 2.0):.0f}，或按轴承样本的安装尺寸 da 核对"))
            if g and g[1] <= 6 and not groove_at(i, zs) and not fillet:
                out.append(_f("S008", "info", f"step{i}",
                              f"Φ{d:g}{tols[i]} 需磨削，靠第 {j} 段轴肩处无砂轮越程槽也无轴肩圆角",
                              "加 {\"feature\": \"groove\", \"type\": \"grinding\", ...}（缺省靠轴肩）或 base.shoulder_fillet"))
        if i in keyed and not g:
            out.append(_f("S010", "warning", f"step{i}", f"键槽段 Φ{d:g} 未注配合公差（装轮毂/联轴器需要）",
                          "按配合选 k6/m6/n6（过渡）或 h7/js6"))
    for h in p.cross_holes:
        g = grade(tols[h["step"]])
        if g and g[1] <= 7 and not h.get("at_assembly"):
            out.append(_f("S007", "warning", h["name"],
                          f"横孔开在配合面 Φ{steps[h['step']][1]:g}{tols[h['step']]} 上：削弱配合、孔口毛刺划伤配合件",
                          "移到非配合段，或确认是装配后配钻的销孔（技术要求注明）"))
    for f in p.shaft_features:
        if f["kind"] != "thread":
            continue
        i = f["step"]
        z0, z1 = p.step_z(i)
        for j, zs in ((i - 1, z0), (i + 1, z1)):  # 螺纹任一端落在通向较大段的轴肩上
            if 0 <= j < n and steps[j][1] > steps[i][1] and min(abs(f["z0"] - zs), abs(f["z1"] - zs)) < 1e-6 \
                    and not groove_at(i, zs):
                out.append(_f("S009", "warning", f["name"], f"螺纹 {f['callout']} 车到轴肩，没有螺纹退刀槽",
                              "缩短螺纹留出收尾，或在轴肩处加退刀槽（undercut）"))
    return out


def summarize(findings: list[Finding]) -> dict[str, Any]:
    errs = sum(1 for f in findings if f["severity"] == "error")
    warns = sum(1 for f in findings if f["severity"] == "warning")
    return {"passed": errs == 0, "errors": errs, "warnings": warns, "findings": findings}
