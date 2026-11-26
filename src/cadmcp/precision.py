"""精度自动补全（CAD 无关）：按工程惯例给轴类补齐基准、几何公差与配合面粗糙度，替代审图时的手工补注。

- 滚动轴承位（js/k/m/n 公差带、IT5–IT6、无键槽）：最外侧两处为公共基准 A、B；注圆柱度（GB/T 275 轴颈，0 级轴承）。
  只有一处轴承位时它为基准 A。轴承定位轴肩（相邻较大段的端面）注对 A(-B) 的端面圆跳动（GB/T 275）。
- 其余配合段（IT ≤ 7）：注对 A(-B) 的径向圆跳动（GB/T 1184 圆跳动，IT ≤ 6 取 6 级、否则 7 级）；
  齿轮段齿顶圆跳动按齿坯公差（gear.blank_runout）。
- 配合段未注粗糙度时按公差等级补 Ra（与审图规则 G004 一致）。
- 套类：精度最高的配合内孔（IT ≤ 7）为基准 A，外圆配合段注对 A 的径向圆跳动，内孔按公差等级补 Ra。
- 盘盖类（法兰均布孔 + 相邻较小的有公差段即止口，IT ≤ 9）：止口为基准 A，法兰定位端面注对 A 的垂直度
  （GB/T 1184 垂直度，止口 IT ≤ 7 取 7 级、否则 8 级，主参数取法兰直径），止口补 Ra。
用户已写基准或几何公差时不改动这两类，只补粗糙度。

数值来源核实状态：GB/T 275 轴颈圆柱度与轴肩端面圆跳动、GB/T 1184 圆跳动与垂直度按设计手册常用值录入，尚未对照标准原文核实
（docs/WINDOWS_TEST.md「待核实的标准数值」）；出图前如有疑问以标准原文为准。
"""
from __future__ import annotations

import re

from .ir.part import Datum, GeoTol, PartSpec, Roughness, ShaftBase, ShaftGear

_DIA = (6, 10, 18, 30, 50, 80, 120, 180, 250, 315, 400, 500)
# GB/T 275 轴颈圆柱度 t（µm，0 级轴承），按轴径段 >3–6 … >400–500
_BEARING_CYL = (2.5, 2.5, 3.0, 4.0, 4.0, 5.0, 6.0, 8.0, 10.0, 12.0, 13.0, 15.0)
# GB/T 1184 附录 圆跳动（µm）6 级 / 7 级，主参数 d 段 ≤3、>3–6、>6–10、>10–18、>18–30、>30–50、>50–120、>120–250、>250–500
_RUN_STEPS = (3, 6, 10, 18, 30, 50, 120, 250, 500)
_RUNOUT = {6: (4, 5, 6, 8, 10, 12, 15, 20, 25), 7: (6, 8, 10, 12, 15, 20, 25, 30, 40)}
IT_GRADE_RA = {5: 0.4, 6: 0.8, 7: 1.6, 8: 1.6, 9: 3.2}
_BEARING_FITS = ("js", "k", "m", "n")


def _grade(tol: str | None) -> tuple[str, int] | None:
    m = re.fullmatch(r"([a-z]{1,2})(\d{1,2})", (tol or "").strip())
    return (m.group(1), int(m.group(2))) if m else None


# GB/T 275 轴肩端面圆跳动 t1（µm，0 级轴承），按轴径段（同 _DIA）。TODO(核对原文)
_BEARING_SHOULDER = (5.0, 6.0, 8.0, 10.0, 12.0, 15.0, 15.0, 20.0, 20.0, 25.0, 25.0, 30.0)


def bearing_shoulder_runout(d: float) -> float:
    i = next((k for k, hi in enumerate(_DIA) if d <= hi), len(_DIA) - 1)
    return _BEARING_SHOULDER[i] / 1000


# GB/T 1184 附录 垂直度（µm）7 级 / 8 级，主参数 L（被测端面直径）段 ≤10、>10–16、>16–25、>25–40、>40–63、
# >63–100、>100–160、>160–250、>250–400、>400–630。TODO(核对原文)
_PERP_STEPS = (10, 16, 25, 40, 63, 100, 160, 250, 400, 630)
_PERP = {7: (6, 8, 10, 12, 15, 20, 25, 30, 40, 50), 8: (10, 12, 15, 20, 25, 30, 40, 50, 60, 80)}


def perpendicularity(L: float, grade: int) -> float:
    i = next((k for k, hi in enumerate(_PERP_STEPS) if L <= hi), len(_PERP_STEPS) - 1)
    return _PERP[grade][i] / 1000


# GB/T 1184 对称度 8 级（µm），主参数 = 键槽宽 b：≤6、>6–10、>10–18、>18–30、>30–50。TODO(核对原文)
_SYM_STEPS = (6, 10, 18, 30, 50)
_SYM8 = (15, 20, 25, 30, 40)


def keyway_symmetry(b: float) -> float:
    i = next((k for k, hi in enumerate(_SYM_STEPS) if b <= hi), len(_SYM_STEPS) - 1)
    return _SYM8[i] / 1000


def bearing_cylindricity(d: float) -> float:
    i = next((k for k, hi in enumerate(_DIA) if d <= hi), len(_DIA) - 1)
    return _BEARING_CYL[i] / 1000


def runout(d: float, grade: int) -> float:
    i = next((k for k, hi in enumerate(_RUN_STEPS) if d <= hi), len(_RUN_STEPS) - 1)
    return _RUNOUT[grade][i] / 1000


def auto_shaft_precision(spec: PartSpec, ev) -> PartSpec:
    """返回补全后的 spec（不改原对象）。ev：表达式求值（轴段长度/直径可能是参数表达式）。"""
    if not isinstance(spec.base, ShaftBase):
        return spec
    steps = [(s.tolerance, float(ev(s.diameter))) for s in spec.base.steps]
    gears = {f.step for f in spec.features if isinstance(f, ShaftGear)}
    fits = {i: g for i, (t, _) in enumerate(steps) if (g := _grade(t)) and g[1] <= 7}
    keyed = {f.step for f in spec.features if getattr(f, "feature", "") == "keyway"}  # 有键槽 = 装轮毂/联轴器，不是轴承位
    bearings = [i for i, (fd, it) in fits.items() if fd in _BEARING_FITS and it <= 6 and i not in keyed]
    datums, tols, rough = list(spec.datums), list(spec.tolerances), list(spec.roughness)
    bores = [(k, g, float(ev(b.diameter))) for k, b in enumerate(spec.base.bores) if (g := _grade(b.tolerance.lower() if b.tolerance else None)) and g[1] <= 7]
    flange = {fh.step for fh in spec.base.flange_holes}
    spig = [(j, g) for f in sorted(flange) for j in (f - 1, f + 1)
            if 0 <= j < len(steps) and j not in flange and (g := _grade(steps[j][0])) and g[1] <= 9
            and steps[j][1] < steps[f][1]]
    if not datums and not tols and spig and not bores:
        # 盘盖类（法兰 + 止口）：止口为基准 A，法兰定位端面（法兰与止口之间的端面）注对 A 的垂直度（GB/T 1184）
        j, (_, it) = min(spig, key=lambda t: (t[1][1], -steps[t[0]][1]))
        f = j - 1 if j - 1 in flange else j + 1
        datums = [Datum(name="A", target=f"step:{j}")]
        tols.append(GeoTol(type="perpendicularity", value=perpendicularity(steps[f][1], 7 if it <= 7 else 8),
                           target=f"shoulder:{min(j, f)}", datums=["A"]))
        if f"step:{j}" not in {r.target for r in rough} and it in IT_GRADE_RA:
            rough.append(Roughness(target=f"step:{j}", ra=IT_GRADE_RA[it]))
    if not datums and not tols and bores:
        # 套类：精度最高（同级取最大）的配合内孔为基准 A，外圆配合段注对 A 的径向圆跳动
        kb = min(bores, key=lambda t: (t[1][1], -t[2]))[0]
        datums = [Datum(name="A", target=f"bore:{kb}")]
        for i in sorted(set(fits) | gears):
            tols.append(GeoTol(type="circular_runout", value=runout(steps[i][1], 6 if fits.get(i, ("", 7))[1] <= 6 else 7),
                               target=f"step:{i}", datums=["A"]))
    if not datums and not tols and (bearings or fits):
        base = [bearings[0], bearings[-1]] if len(bearings) >= 2 else bearings[:1]
        if not base and len(fits) + len(gears) >= 2:  # 无轴承位：取最外两处配合段
            ks = sorted(fits)
            base = [ks[0], ks[-1]] if len(ks) >= 2 else ks
        letters = ["A", "B"][:len(base)]
        datums = [Datum(name=n, target=f"step:{i}") for n, i in zip(letters, base)]
        for i in base:
            if i in bearings:
                tols.append(GeoTol(type="cylindricity", value=bearing_cylindricity(steps[i][1]), target=f"step:{i}"))
        for i in bearings:  # 轴承定位轴肩（相邻较大的一段）：端面圆跳动对公共基准（GB/T 275）
            nb = [j for j in (i - 1, i + 1) if 0 <= j < len(steps) and steps[j][1] > steps[i][1]]
            if nb:
                j = max(nb, key=lambda j: steps[j][1])
                tols.append(GeoTol(type="circular_runout", value=bearing_shoulder_runout(steps[i][1]),
                                   target=f"shoulder:{min(i, j)}", datums=letters))
        # 键槽对称度：键槽中心平面对所在轴段轴线（该段为基准；已是基准则沿用字母），GB/T 1184 8 级
        from .standards import keyway as kw_table
        for f in spec.features:
            if getattr(f, "feature", "") != "keyway":
                continue
            i = f.step
            dn = next((d.name for d in datums if d.target == f"step:{i}"), None)
            if dn is None:
                dn = "ABCDEFGH"[len(datums)]
                datums.append(Datum(name=dn, target=f"step:{i}"))
            b = float(ev(f.width)) if f.width is not None else kw_table(steps[i][1])[0]
            tols.append(GeoTol(type="symmetry", value=keyway_symmetry(b), target=f"keyway:{f.name}", datums=[dn]))
        gear_of = {f.step: f for f in spec.features if isinstance(f, ShaftGear)}
        for i in sorted((set(fits) | gears) - set(base)):
            it = fits[i][1] if i in fits else 7
            v = runout(steps[i][1], 6 if it <= 6 else 7)
            if i in gear_of:  # 齿顶圆跳动：齿坯公差（按分度圆直径与精度等级，同齿轮盘）
                from .gear import blank_runout
                g = gear_of[i]
                v = blank_runout(float(ev(g.module)) * g.teeth, g.accuracy) or v
            tols.append(GeoTol(type="circular_runout", value=v, target=f"step:{i}", datums=letters))
    have = {r.target for r in rough}
    for k, (fd, it), _ in bores:
        if f"bore:{k}" not in have and it in IT_GRADE_RA:
            rough.append(Roughness(target=f"bore:{k}", ra=IT_GRADE_RA[it]))
    for i, (fd, it) in sorted(fits.items()):
        if f"step:{i}" not in have and it in IT_GRADE_RA:
            rough.append(Roughness(target=f"step:{i}", ra=IT_GRADE_RA[it]))
    return spec.model_copy(update={"datums": datums, "tolerances": tols, "roughness": rough})
