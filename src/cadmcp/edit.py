"""修改现有模型（与 CAD 无关）：修改指令、校验、改前改后对比。

流程：inspect（适配器读出特征树/尺寸/方程式/属性/质量）→ 校验修改指令 → 适配器执行并重建
→ 对比 → 找出引用该文件的工程图/装配并更新（检查悬空标注）。
尺寸名用 CAD 原生全名（SW "D1@凸台-拉伸1"，Creo 参数名/d 号），由 inspect 给出。
"""
from __future__ import annotations

import difflib
from typing import Annotated, Any, Literal

from pydantic import Field

from .errors import SPEC_INVALID, CadError
from .ir.common import Strict


class SetDimension(Strict):
    op: Literal["set_dimension"] = "set_dimension"
    name: str = Field(description='尺寸全名，如 "D1@Boss-Extrude1"（来自 cad_inspect_existing）')
    value: float = Field(description="mm 或 度")


class SetVariable(Strict):
    op: Literal["set_variable"] = "set_variable"
    name: str = Field(description="全局变量 / 参数名")
    value: float


class SetProperty(Strict):
    op: Literal["set_property"] = "set_property"
    name: str
    value: str


class Suppress(Strict):
    op: Literal["suppress"] = "suppress"
    feature: str
    suppressed: bool = True


class SetMaterial(Strict):
    op: Literal["set_material"] = "set_material"
    material: str


class ReplaceComponent(Strict):
    op: Literal["replace_component"] = "replace_component"
    instance: str = Field(description="装配中的实例名（来自 cad_inspect_existing 的 components）")
    source: str = Field(description='"lib:<编码>" 或工作区内文件相对路径')
    all_instances: bool = Field(False, description="同一零件的所有实例一起替换")
    file: str | None = Field(None, description="（平台内部）解析后的新件文件路径")
    code: str | None = Field(None, description="（平台内部）新件库编码")
    mass_kg: float | None = Field(None, description="（平台内部）新件库登记质量（占位件按此计）")


Edit = Annotated[SetDimension | SetVariable | SetProperty | Suppress | SetMaterial | ReplaceComponent,
                 Field(discriminator="op")]

RATIO_WARN = 3.0


def _near(name: str, pool: list[str]) -> str:
    m = difflib.get_close_matches(name, pool, n=4, cutoff=0.4)
    return f"相近：{m}" if m else f"可用：{pool[:12]}"


def check_edits(info: dict[str, Any], edits: list[Any]) -> list[dict[str, Any]]:
    """校验修改指令。error 会阻止执行；warning 随结果返回。"""
    out: list[dict[str, Any]] = []
    dims = {d["name"]: d for d in info.get("dimensions", [])}
    vars_ = {v["name"]: v for v in info.get("variables", [])}
    feats = {f["name"]: f for f in info.get("features", [])}
    driven = {e["lhs"]: e for e in info.get("equations", []) if e.get("lhs")}

    def f(rule, sev, target, msg, hint=None):
        d = {"rule": rule, "severity": sev, "target": target, "message": msg}
        if hint:
            d["hint"] = hint
        out.append(d)

    seen: set[tuple[str, str]] = set()
    for e in edits:
        key = (e.op, getattr(e, "name", None) or getattr(e, "feature", None) or "")
        if key in seen:
            f("E006", "error", key[1], f"同一对象修改了两次：{key}")
        seen.add(key)
        if isinstance(e, SetDimension):
            d = dims.get(e.name)
            if d is None:
                f("E001", "error", e.name, f"没有尺寸 {e.name}", _near(e.name, list(dims)))
                continue
            if d.get("driven"):
                f("E003", "error", e.name, f"{e.name} 是从动尺寸（参考尺寸），不能直接修改")
                continue
            if e.name in driven:
                rhs = driven[e.name]["rhs"]
                f("E003", "error", e.name, f"{e.name} 由方程式 {e.name} = {rhs} 驱动，直接改会被方程式覆盖",
                  "改方程式里的全局变量（set_variable）")
                continue
            _value_checks(f, e.name, d, e.value)
        elif isinstance(e, SetVariable):
            v = vars_.get(e.name)
            if v is None:
                f("E001", "error", e.name, f"没有全局变量 {e.name}", _near(e.name, list(vars_)))
                continue
            _value_checks(f, e.name, v, e.value, angle=False)
        elif isinstance(e, Suppress):
            ft = feats.get(e.feature)
            if ft is None:
                f("E001", "error", e.feature, f"没有特征 {e.feature}", _near(e.feature, list(feats)))
                continue
            if ft.get("suppressed") == e.suppressed:
                f("E005", "warning", e.feature, f"{e.feature} 已经是{'压缩' if e.suppressed else '解压'}状态")
            kids = [x["name"] for x in info.get("features", []) if e.feature in x.get("parents", [])]
            if e.suppressed and kids:
                f("E004", "warning", e.feature, f"压缩 {e.feature} 会连带压缩子特征 {kids}")
        elif isinstance(e, ReplaceComponent):
            comps = {c["instance"]: c for c in info.get("components", [])}
            if info.get("kind") != "assembly":
                f("E001", "error", e.instance, "replace_component 只能用于装配")
            elif e.instance not in comps:
                f("E001", "error", e.instance, f"装配中没有实例 {e.instance}", _near(e.instance, list(comps)))
        elif isinstance(e, SetMaterial):
            from . import materials
            if e.material not in materials.MATERIALS:
                f("E001", "error", e.material, f"材料 {e.material} 不在平台材料表中",
                  _near(e.material, sorted(materials.MATERIALS)))
        elif isinstance(e, SetProperty) and e.name in ("PartNo", "图号", "代号"):
            f("E007", "warning", e.name, "修改图号会影响引用该零件的 BOM 与图纸，确认已在 PDM 中同步")
    return out


def _value_checks(f, name: str, d: dict[str, Any], value: float, angle: bool | None = None) -> None:
    old = float(d.get("value", 0) or 0)
    is_angle = d.get("unit") == "deg" if angle is None else angle
    if not is_angle and value <= 0 and d.get("kind") != "signed":
        f("E002", "error", name, f"{name} = {value:g} 不合理（长度必须 > 0）")
        return
    if old and value and not is_angle:
        r = value / old
        if r > RATIO_WARN or r < 1 / RATIO_WARN:
            f("E002", "warning", name, f"{name} 从 {old:g} 改为 {value:g}（×{r:.2f}），变化很大，确认不是单位错误")


MOUNT_KEYS = {"screw": "安装螺钉", "holes": "安装孔位", "pilot": "定位止口", "frame": "安装基准"}


def interface_check(instance: str, old: dict[str, Any] | None, new: dict[str, Any] | None,
                    mates: list[dict[str, Any]], new_refs: list[str]) -> list[dict[str, Any]]:
    """替换前比对：安装接口（库 mount）是否一致、配合引用的基准在新件上是否存在。"""
    out = []
    partners = sorted({m["b"] if m["a"] == instance else m["a"] for m in mates if instance in (m["a"], m["b"])})
    om, nm = (old or {}).get("mount"), (new or {}).get("mount")
    if om and nm:
        d = [f"{MOUNT_KEYS[k]} {om.get(k)} → {nm.get(k)}" for k in MOUNT_KEYS if om.get(k) != nm.get(k)]
        if d:
            out.append({"rule": "E012", "severity": "warning", "target": instance,
                        "message": "安装接口不同：" + "；".join(d),
                        "hint": f"同步修改安装件 {partners or '（未知）'} 的安装孔（平台建的零件把 mount.item 改为新编码后重建）"})
    elif old is not None and new is not None and bool(om) != bool(nm):
        out.append({"rule": "E012", "severity": "warning", "target": instance,
                    "message": "新旧件只有一方登记了安装接口，无法自动比对", "hint": "人工核对安装尺寸"})
    for m in mates:
        if instance in (m["a"], m["b"]):
            ref = m.get("a_ref") if m["a"] == instance else m.get("b_ref")
            if ref and ref not in new_refs:
                out.append({"rule": "E013", "severity": "error", "target": f"{instance}.{ref}",
                            "message": f"配合 {m['name']} 引用的 {ref} 在新件上不存在，替换后配合会断开",
                            "hint": f"新件基准：{sorted(new_refs)}"})
    if old and new:
        bo, bn = old.get("bbox"), new.get("bbox")
        if bo and bn:
            grow = [round((bn[2 * i + 1] - bn[2 * i]) - (bo[2 * i + 1] - bo[2 * i]), 1) for i in range(3)]
            if any(g > 0 for g in grow):
                out.append({"rule": "E014", "severity": "warning", "target": instance,
                            "message": f"新件外形变大 {grow} mm（X/Y/Z），检查与周边干涉",
                            "hint": "替换后运行 cad_check_interference 或在 CAD 中做干涉检查"})
    return out


def diff(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    def table(info, key, field="value"):
        return {x["name"]: x.get(field) for x in info.get(key, [])}

    out: dict[str, Any] = {}
    for key, label in (("dimensions", "dimensions"), ("variables", "variables"), ("properties", "properties")):
        a, b = table(before, key), table(after, key)
        ch = [{"name": k, "before": a.get(k), "after": b.get(k)} for k in sorted(set(a) | set(b)) if a.get(k) != b.get(k)]
        if ch:
            out[label] = ch
    a, b = table(before, "features", "suppressed"), table(after, "features", "suppressed")
    ch = [{"feature": k, "suppressed": b.get(k)} for k in sorted(set(a) | set(b)) if a.get(k) != b.get(k)]
    if ch:
        out["features"] = ch
    cb = {c["instance"]: c.get("code") or c.get("file") for c in before.get("components", [])}
    ca = {c["instance"]: c.get("code") or c.get("file") for c in after.get("components", [])}
    ch = [{"instance": k, "before": cb.get(k), "after": ca.get(k)} for k in sorted(set(cb) | set(ca)) if cb.get(k) != ca.get(k)]
    if ch:
        out["components"] = ch
    if before.get("material") != after.get("material"):
        out["material"] = {"before": before.get("material"), "after": after.get("material")}
    mb, ma = before.get("mass_kg"), after.get("mass_kg")
    if mb is not None and ma is not None and abs(ma - mb) > 1e-6:
        out["mass_kg"] = {"before": round(mb, 4), "after": round(ma, 4), "delta": round(ma - mb, 4)}
    if before.get("bbox_mm") != after.get("bbox_mm"):
        out["bbox_mm"] = {"before": before.get("bbox_mm"), "after": after.get("bbox_mm")}
    return out


def describe(file: str, d: dict[str, Any]) -> str:
    """更改记录用的一句话说明。"""
    parts = []
    for c in d.get("dimensions", []) + d.get("variables", []):
        parts.append(f"{c['name']} {c['before']:g}→{c['after']:g}" if isinstance(c["before"], (int, float))
                     and isinstance(c["after"], (int, float)) else f"{c['name']}")
    for c in d.get("components", []):
        parts.append(f"{c['instance']} 换为 {c['after']}")
    for c in d.get("features", []):
        parts.append(f"{'压缩' if c['suppressed'] else '恢复'} {c['feature']}")
    if "material" in d:
        parts.append(f"材料 {d['material']['before']}→{d['material']['after']}")
    for c in d.get("properties", []):
        parts.append(f"{c['name']}={c['after']}")
    return f"{file}：" + "，".join(parts) if parts else f"{file}：无变化"


def parse_edits(raw: list[dict[str, Any]]) -> list[Any]:
    from pydantic import TypeAdapter, ValidationError
    try:
        return TypeAdapter(list[Edit]).validate_python(raw)
    except ValidationError as e:
        raise CadError(SPEC_INVALID, f"invalid edits: {e.errors()[0]['msg']} at {e.errors()[0]['loc']}",
                       hint="op 取 set_dimension/set_variable/set_property/suppress/set_material") from None
