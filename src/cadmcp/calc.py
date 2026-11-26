"""选型计算（与 CAD 无关）：非标设备里工程师天天做的气缸、伺服+丝杠、同步带、螺栓校核。

每个计算返回带公式过程的"计算书"（steps），结论可直接审核；选型表为通用参考值，
最终以厂家样本为准（结果里会注明）。单位：N、mm、kg、s、MPa、N·m、kg·m²。
"""
from __future__ import annotations

import math
from typing import Any, Callable

from .errors import SPEC_INVALID, CadError

G = 9.81

# 气缸缸径系列（ISO 15552 / 小型缸）与对应活塞杆径
CYL_BORES = {10: 4, 12: 6, 16: 6, 20: 8, 25: 10, 32: 12, 40: 16, 50: 20, 63: 20, 80: 25, 100: 25, 125: 32}

# 通用低惯量 AC 伺服（3000 rpm 额定，参考值）：功率 W → (额定转矩, 最大转矩, 转子惯量 kg·m², 法兰)
SERVOS = {
    50: (0.16, 0.48, 0.025e-4, 40), 100: (0.32, 0.95, 0.051e-4, 40), 200: (0.64, 1.91, 0.14e-4, 60),
    400: (1.27, 3.82, 0.26e-4, 60), 750: (2.39, 7.16, 0.87e-4, 80), 1000: (3.18, 9.55, 1.6e-4, 80),
    1500: (4.77, 14.3, 2.3e-4, 100), 2000: (6.37, 19.1, 3.0e-4, 100),
}
SCREW_LEADS = [2, 4, 5, 10, 16, 20, 25, 32]
STEEL_DENSITY = 7850.0

# 螺栓：应力截面积 As (mm²)；性能等级 → 屈服强度 (MPa)
BOLT_AS = {"M3": 5.03, "M4": 8.78, "M5": 14.2, "M6": 20.1, "M8": 36.6, "M10": 58.0, "M12": 84.3,
           "M16": 157.0, "M20": 245.0}
BOLT_YIELD = {"8.8": 640.0, "10.9": 940.0, "12.9": 1100.0}


class Report:
    def __init__(self, kind: str, inputs: dict[str, Any]):
        self.kind, self.inputs = kind, inputs
        self.steps: list[dict[str, Any]] = []
        self.warnings: list[str] = []

    def step(self, item: str, formula: str, value: float, unit: str = "") -> float:
        self.steps.append({"item": item, "formula": formula, "value": round(value, 6), "unit": unit})
        return value

    def done(self, result: dict[str, Any], selection: dict[str, Any] | None = None) -> dict[str, Any]:
        return {"ok": True, "kind": self.kind, "inputs": self.inputs, "steps": self.steps, "result": result,
                "selection": selection or {}, "warnings": self.warnings,
                "note": "选型表为通用参考值，最终型号以厂家样本核对"}


def _req(p: dict[str, Any], *keys: str) -> None:
    miss = [k for k in keys if k not in p]
    if miss:
        raise CadError(SPEC_INVALID, f"missing calc inputs: {miss}")


# ---------------- 气缸 ----------------
def cylinder(p: dict[str, Any]) -> dict[str, Any]:
    """气缸缸径。load_kg 或 force_N；orientation=horizontal（导轨上推动，μ）/vertical（举升）；
    pressure_MPa 默认 0.5；load_rate 负载率默认 水平 0.7、垂直 0.5（动态）；direction push/pull。"""
    orient = p.get("orientation", "horizontal")
    P = float(p.get("pressure_MPa", 0.5))
    eta = float(p.get("load_rate", 0.7 if orient == "horizontal" else 0.5))
    r = Report("cylinder", dict(p))
    if "force_N" in p:
        F = r.step("负载力", "给定", float(p["force_N"]), "N")
    else:
        _req(p, "load_kg")
        m = float(p["load_kg"])
        if orient == "vertical":
            F = r.step("负载力", "F = m·g", m * G, "N")
        else:
            mu = float(p.get("friction", 0.1))
            F = r.step("负载力", f"F = μ·m·g（μ={mu:g}）", mu * m * G, "N")
    pull = p.get("direction", "push") == "pull"
    D_req = r.step("理论缸径", f"D = √(4F / (π·P·η))，P={P:g}MPa，η={eta:g}", math.sqrt(4 * F / (math.pi * P * eta)), "mm")
    bore = next((b for b in CYL_BORES if b >= D_req and (not pull or (b * b - CYL_BORES[b] ** 2) >= D_req ** 2)), None)
    if bore is None:
        raise CadError(SPEC_INVALID, f"load too large for cylinder series (D≥{D_req:.1f})", hint="改用电缸/液压或多缸")
    rod = CYL_BORES[bore]
    area = math.pi / 4 * (bore * bore - (rod * rod if pull else 0))
    out = r.step("所选缸径理论输出力", f"F' = P·A，A = π/4·{'(D²-d²)' if pull else 'D²'}", P * area, "N")
    r.step("实际负载率", "F / F'", F / out)
    if F / out > eta + 1e-9:
        r.warnings.append("负载率超过推荐值")
    stroke = p.get("stroke_mm")
    sel = {"bore": bore, "rod": rod, "stroke": stroke, "model_hint": f"SC{bore}x{stroke}" if stroke else f"缸径{bore}"}
    if stroke and orient == "vertical" and float(stroke) > 10 * bore:
        r.warnings.append("细长行程垂直安装，需校核活塞杆压杆稳定或加导向")
    if p.get("speed_mm_s") and float(p["speed_mm_s"]) > 500:
        r.warnings.append("速度 >500 mm/s，需选缓冲型气缸并校核动能")
    return r.done({"required_bore_mm": round(D_req, 2), "force_N": round(F, 1), "output_N": round(out, 1)}, sel)


# ---------------- 伺服 + 滚珠丝杠 / 同步带 ----------------
def _motion_profile(stroke: float, v: float, ta: float) -> tuple[float, float, float]:
    """梯形速度曲线：返回 (实际最高速度, 加速时间, 匀速时间)；行程不够时变三角形。"""
    if v * ta >= stroke:  # 加速段+减速段距离 = v·ta
        v = stroke / ta
        return v, ta, 0.0
    return v, ta, (stroke - v * ta) / v


def _drive(r: Report, p: dict[str, Any], lead: float, J_extra: float, eff: float):
    m = float(p["load_kg"])
    orient = p.get("orientation", "horizontal")
    mu = float(p.get("friction", 0.01))  # 直线导轨
    stroke, v = float(p["stroke_mm"]), float(p["speed_mm_s"])
    ta = float(p.get("accel_time_s", 0.2))
    dwell = float(p.get("dwell_s", 0.5))
    sf = float(p.get("safety_factor", 1.5))
    max_ratio = float(p.get("max_inertia_ratio", 10))
    v, ta, tc = _motion_profile(stroke, v, ta)
    r.step("最高速度", "梯形曲线（行程不足时为三角形）", v, "mm/s")
    rpm = r.step("电机转速", "N = v / 导程 × 60", v / lead * 60, "rpm")
    if rpm > 3000:
        r.warnings.append(f"电机转速 {rpm:.0f} rpm 超过额定 3000 rpm")
    F = r.step("轴向负载力", "F = m·g" if orient == "vertical" else f"F = μ·m·g（μ={mu:g}）",
               m * G if orient == "vertical" else mu * m * G, "N")
    T_L = r.step("负载转矩", f"T_L = F·Ph / (2π·η)，η={eff:g}", F * lead / 1000 / (2 * math.pi * eff), "N·m")
    J_load = r.step("负载惯量（折算到电机）", "J_L = m·(Ph/2π)² + J_传动件",
                    m * (lead / 1000 / (2 * math.pi)) ** 2 + J_extra, "kg·m²")
    alpha = r.step("角加速度", "α = 2π·N/60 / t_a", (rpm * 2 * math.pi / 60) / ta, "rad/s²")
    cycle = 2 * ta + tc + dwell
    T_hold = T_L if orient == "vertical" else 0.0
    res = {"rpm": round(rpm, 1), "J_load_kgm2": float(f"{J_load:.4g}"), "cycle_s": round(cycle, 3)}
    sel: dict[str, Any] = {}
    for W, (Tr, Tp, Jm, flange) in SERVOS.items():
        T_a = (J_load + Jm) * alpha / eff
        T_peak = T_a + T_L
        T_dec = T_L - T_a
        T_rms = math.sqrt((T_peak ** 2 * ta + T_L ** 2 * tc + T_dec ** 2 * ta + T_hold ** 2 * dwell) / cycle)
        if Tr >= T_rms * sf and Tp >= T_peak * sf and J_load / Jm <= max_ratio:
            r.step("加速转矩（含转子惯量）", f"T_a = (J_L + J_M)·α / η，J_M={Jm:.3e}", T_a, "N·m")
            r.step("峰值转矩", "T_pk = T_a + T_L", T_peak, "N·m")
            r.step("有效转矩", "√((Tpk²·ta + T_L²·tc + (T_L−T_a)²·ta + T_hold²·t_dwell) / t_cycle)", T_rms, "N·m")
            r.step("所选电机", f"{W}W：额定 {Tr} ≥ {sf:g}×T_rms，最大 {Tp} ≥ {sf:g}×T_pk", W, "W")
            r.step("惯量比", f"J_L / J_M ≤ {max_ratio:g}", J_load / Jm)
            res.update(T_rms_Nm=round(T_rms, 3), T_peak_Nm=round(T_peak, 3))
            sel = {"servo_W": W, "rated_Nm": Tr, "peak_Nm": Tp, "inertia_ratio": round(J_load / Jm, 2),
                   "flange": flange, "model_hint": f"SRV-{flange}-{W}W"}
            break
    else:
        T_a = J_load * alpha / eff
        res.update(T_rms_Nm=None, T_peak_Nm=round(T_a + T_L, 3))
        r.warnings.append("通用系列中无满足条件的电机：加减速器、减小惯量或延长加速时间")
    if orient == "vertical":
        r.warnings.append("垂直轴需选带抱闸电机")
        sel["brake"] = True
    return res, sel


def screw_drive(p: dict[str, Any]) -> dict[str, Any]:
    """伺服+滚珠丝杠。必填 load_kg、stroke_mm、speed_mm_s；可选 lead_mm（不填按转速≤3000 自动选）、
    screw_diameter_mm（默认 16）、screw_length_mm（默认 行程+100）、accel_time_s、dwell_s、orientation、safety_factor。"""
    _req(p, "load_kg", "stroke_mm", "speed_mm_s")
    r = Report("screw_drive", dict(p))
    v = float(p["speed_mm_s"])
    lead = float(p["lead_mm"]) if "lead_mm" in p else next((L for L in SCREW_LEADS if v / L * 60 <= 3000), SCREW_LEADS[-1])
    r.step("导程", "给定" if "lead_mm" in p else "取使 N≤3000rpm 的最小标准导程", lead, "mm")
    d = float(p.get("screw_diameter_mm", 16))
    Ls = float(p.get("screw_length_mm", float(p["stroke_mm"]) + 100))
    J_s = r.step("丝杠惯量", "J = π·ρ·L·d⁴ / 32", math.pi * STEEL_DENSITY * Ls / 1000 * (d / 1000) ** 4 / 32, "kg·m²")
    n_crit = r.step("丝杠危险转速（两端支撑-固定）", "Nc ≈ 1.2e8·d / L²·0.8", 1.2e8 * d / Ls ** 2 * 0.8, "rpm")
    res, sel = _drive(r, p, lead, J_s, float(p.get("efficiency", 0.9)))
    if res["rpm"] > n_crit:
        r.warnings.append(f"转速超过丝杠危险转速 {n_crit:.0f} rpm：加大丝径或缩短跨距")
    sel["screw"] = f"Φ{d:g}×{lead:g}（导程）×{Ls:g}"
    return r.done(res, sel)


def belt_drive(p: dict[str, Any]) -> dict[str, Any]:
    """伺服+同步带直线模组。必填 load_kg、stroke_mm、speed_mm_s；可选 pulley_pd_mm（带轮节径，默认 30）、
    pulley_kg（每只带轮质量，默认 0.15）、belt_kg（默认 0.2）。"""
    _req(p, "load_kg", "stroke_mm", "speed_mm_s")
    r = Report("belt_drive", dict(p))
    D = float(p.get("pulley_pd_mm", 30))
    lead = r.step("每转行程", "π·D", math.pi * D, "mm")
    mp, mb = float(p.get("pulley_kg", 0.15)), float(p.get("belt_kg", 0.2))
    J_p = r.step("带轮+带惯量", "2·(m_p·D²/8) + m_b·(D/2)²", 2 * mp * (D / 1000) ** 2 / 8 + mb * (D / 2000) ** 2, "kg·m²")
    res, sel = _drive(r, p, lead, J_p, float(p.get("efficiency", 0.95)))
    if res["rpm"] < 300:
        r.warnings.append("电机转速偏低，建议加减速机以改善惯量比与分辨率")
    sel["pulley"] = f"节径 {D:g}"
    return r.done(res, sel)


# ---------------- 螺栓 ----------------
def bolt(p: dict[str, Any]) -> dict[str, Any]:
    """螺栓组校核。size（M6）、count、grade（8.8/10.9/12.9）、tension_N（轴向总载荷）、shear_N（横向总载荷，靠摩擦）；
    friction 默认 0.15，preload_ratio 默认 0.7（预紧力/屈服），safety_factor 默认 1.5。"""
    _req(p, "size", "count")
    size, n = str(p["size"]).upper(), int(p["count"])
    grade = str(p.get("grade", "8.8"))
    if size not in BOLT_AS or grade not in BOLT_YIELD:
        raise CadError(SPEC_INVALID, f"unsupported bolt {size} {grade}", hint=f"size {sorted(BOLT_AS)}; grade {sorted(BOLT_YIELD)}")
    r = Report("bolt", dict(p))
    sf = float(p.get("safety_factor", 1.5))
    As, Re = BOLT_AS[size], BOLT_YIELD[grade]
    Fp = r.step("单个螺栓预紧力", f"Fp = k·Re·As，k={p.get('preload_ratio', 0.7)}", float(p.get("preload_ratio", 0.7)) * Re * As, "N")
    ok = True
    res: dict[str, Any] = {"preload_N": round(Fp, 0)}
    if p.get("tension_N"):
        Ft = r.step("单个螺栓轴向载荷", "F / n", float(p["tension_N"]) / n, "N")
        cap = r.step("单个螺栓许用轴向载荷", "(Re·As − Fp·0.1) / SF（预紧状态下余量，简化）", (Re * As - 0.1 * Fp) / sf, "N")
        res["tension_utilization"] = round(Ft / cap, 3)
        ok &= Ft <= cap
        if Ft > 0.8 * Fp:
            r.warnings.append("工作载荷接近预紧力，接合面可能分离：提高预紧或增加螺栓")
    if p.get("shear_N"):
        mu = float(p.get("friction", 0.15))
        need = r.step("所需摩擦力", "F_shear·SF", float(p["shear_N"]) * sf, "N")
        have = r.step("螺栓组摩擦力", f"n·Fp·μ，μ={mu:g}", n * Fp * mu, "N")
        res["slip_utilization"] = round(need / have, 3)
        ok &= need <= have
        if need > have:
            r.warnings.append("摩擦不足以传递横向力：加大规格/数量，或加定位销/止口承剪")
    res["pass"] = bool(ok)
    return r.done(res, {"bolt": f"{n}×{size} {grade}级", "tightening_torque_Nm": round(0.2 * Fp * int(size[1:]) / 1000, 1)})


CALCS: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {
    "cylinder": cylinder, "screw_drive": screw_drive, "belt_drive": belt_drive, "bolt": bolt,
}


def run(kind: str, params: dict[str, Any]) -> dict[str, Any]:
    if kind not in CALCS:
        raise CadError(SPEC_INVALID, f"unknown calc {kind!r}", hint=f"可用：{sorted(CALCS)}")
    return CALCS[kind](params)


def to_markdown(rep: dict[str, Any]) -> str:
    lines = [f"# 计算书：{rep['kind']}", "", "## 输入", ""]
    lines += [f"- {k}: {v}" for k, v in rep["inputs"].items()]
    lines += ["", "## 计算过程", "", "| 项目 | 公式 | 数值 | 单位 |", "|---|---|---|---|"]
    lines += [f"| {s['item']} | {s['formula']} | {s['value']:g} | {s['unit']} |" for s in rep["steps"]]
    lines += ["", "## 结论", ""] + [f"- {k}: {v}" for k, v in {**rep["result"], **rep["selection"]}.items()]
    if rep["warnings"]:
        lines += ["", "## 注意", ""] + [f"- {w}" for w in rep["warnings"]]
    lines += ["", f"> {rep['note']}"]
    return "\n".join(lines) + "\n"
