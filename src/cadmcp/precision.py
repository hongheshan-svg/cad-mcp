"""精度自动补全（CAD 无关）：按工程惯例给轴类补齐基准、几何公差与配合面粗糙度，替代审图时的手工补注。

- 滚动轴承位（js/k/m/n 公差带、IT5–IT6、无键槽）：最外侧两处为公共基准 A、B；注圆柱度（GB/T 275 轴颈，0 级轴承）。
  只有一处轴承位时它为基准 A。
- 其余配合段（IT ≤ 7）：注对 A(-B) 的径向圆跳动（GB/T 1184 圆跳动，IT ≤ 6 取 6 级、否则 7 级）；
  齿轮段齿顶圆跳动按齿坯公差（gear.blank_runout）。
- 配合段未注粗糙度时按公差等级补 Ra（与审图规则 G004 一致）。
用户已写基准或几何公差时不改动这两类，只补粗糙度。

数值来源核实状态：GB/T 275 轴颈圆柱度、GB/T 1184 圆跳动两张表按设计手册常用值录入，尚未对照标准原文核实
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
    for i, (fd, it) in sorted(fits.items()):
        if f"step:{i}" not in have and it in IT_GRADE_RA:
            rough.append(Roughness(target=f"step:{i}", ra=IT_GRADE_RA[it]))
    return spec.model_copy(update={"datums": datums, "tolerances": tols, "roughness": rough})
