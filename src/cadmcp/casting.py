"""铸件（与 CAD 无关）：由加工后的零件 spec 生成毛坯 spec、铸造规则、铸造技术要求。

毛坯坐标与零件一致（XY 重合）；底面为加工面时毛坯向下多出余量，毛坯草图仍在 Z=0，
零件底面位于毛坯 z=a 处（参考坐标系 CS_Finished）。
"""
from __future__ import annotations

from typing import Any

from . import materials
from .errors import CAPABILITY_UNSUPPORTED, CadError
from .ir.part import CAST_DEFAULTS, PartSpec
from .ir.resolve import ResolvedPart, resolve_part

SIDE = ("front", "back", "left", "right")


def params(spec: PartSpec) -> dict[str, Any]:
    c = spec.casting
    d = CAST_DEFAULTS[c.process]
    return {"process": c.process, "allowance": float(c.allowance if c.allowance is not None else d["allowance"]),
            "draft": float(c.draft_deg if c.draft_deg is not None else d["draft"]),
            "min_hole": float(c.min_cast_hole if c.min_cast_hole is not None else d["min_hole"]),
            "fillet": float(c.fillet), "tol": d["tol"], "machined": list(c.machined_faces)}


def blank_spec(spec: PartSpec, p: ResolvedPart | None = None) -> tuple[dict[str, Any], dict[str, Any]]:
    """返回 (毛坯 PartSpec dict, 说明)。"""
    p = p or resolve_part(spec)
    cp = params(spec)
    a, mf = cp["allowance"], set(cp["machined"])
    prof = p.profile
    u0, u1, v0, v1 = prof.bbox2d
    side_machined = mf & set(SIDE)
    if side_machined and prof.shape != "rect":
        raise CadError(CAPABILITY_UNSUPPORTED, "casting: side-face allowance needs a rect profile (v1)",
                       hint="外形非矩形时只支持顶/底面加工余量")
    if prof.shape == "rect":
        bu0 = u0 - (a if "left" in mf else 0)
        bu1 = u1 + (a if "right" in mf else 0)
        bv0 = v0 - (a if "front" in mf else 0)
        bv1 = v1 + (a if "back" in mf else 0)
        profile = {"shape": "rect", "width": bu1 - bu0, "height": bv1 - bv0, "center": [(bu0 + bu1) / 2, (bv0 + bv1) / 2]}
        if prof.data.get("corner_radius"):
            profile["corner_radius"] = prof.data["corner_radius"]
    else:
        profile = spec.base.profile.model_dump(exclude_none=True)
    zs = a if "bottom" in mf else 0.0
    thk = p.thickness + zs + (a if "top" in mf else 0.0)

    feats: list[dict[str, Any]] = []
    cast_holes, drilled = [], []
    for h in p.holes:
        if not h.through or h.drill < cp["min_hole"]:
            drilled.append(f"{h.name}（{h.callout}）")
            continue
        d = h.drill - 2 * a
        pos = [[i.uv[0], i.uv[1] + (zs if h.face in SIDE else 0)] for i in h.instances]
        feats.append({"feature": "hole", "name": f"{h.name}_CAST", "face": h.face, "type": "simple",
                      "size": round(d, 3), "positions": pos})
        cast_holes.append(f"{h.name} Φ{h.drill:g}→铸孔 Φ{d:g}")
    pockets_dropped = []
    for pk in p.pockets:
        pr = pk["profile"]
        if pr.shape != "rect" or pk["face"] not in ("top", "bottom"):
            pockets_dropped.append(pk["name"])
            continue
        w, hgt = pr.bbox2d[1] - pr.bbox2d[0] - 2 * a, pr.bbox2d[3] - pr.bbox2d[2] - 2 * a
        # 铸出型腔底面 = 加工后底面再留余量 a；所在面本身加工时，毛坯在该侧多出 a
        depth = pk["depth"] + (a if pk["face"] in mf else 0) - a
        if w <= 0 or hgt <= 0 or (not pk["through"] and depth <= 0):
            pockets_dropped.append(pk["name"])
            continue
        f = {"feature": "pocket", "name": f"{pk['name']}_CAST", "face": pk["face"],
             "profile": {"shape": "rect", "width": w, "height": hgt,
                         "center": [(pr.bbox2d[0] + pr.bbox2d[1]) / 2, (pr.bbox2d[2] + pr.bbox2d[3]) / 2]},
             "depth": "through" if pk["through"] else round(depth, 4)}
        feats.append(f)
    for b in p.bosses:
        feats.append({"feature": "boss", "name": f"{b['name']}_CAST", "face": "top", "height": b["height"],
                      "profile": _profile_dict(b["profile"])})
    blank = {"name": f"{spec.name}_BLANK", "part_no": f"{spec.part_no}-M", "title": f"{spec.title}（铸件毛坯）",
             "material": spec.material, "category": "casting", "surface_treatment": "无",
             "base": {"type": "extrude", "profile": profile, "thickness": round(thk, 4)},
             "features": feats,
             "references": [{"name": "CS_Finished", "origin": [0, 0, zs]}],
             "casting": {**spec.casting.model_dump(), "blank_of": spec.name},
             "properties": {"毛坯类型": {"sand": "砂型铸件", "investment": "熔模精铸件", "die": "压铸件"}[cp["process"]],
                            "零件图号": spec.part_no}}
    info = {"allowance": a, "machined_faces": sorted(mf), "cast_holes": cast_holes, "drilled_after_casting": drilled,
            "pockets_machined_from_solid": pockets_dropped, "z_offset": zs}
    return blank, info


def _profile_dict(pr) -> dict[str, Any]:
    if pr.shape == "rect":
        u0, u1, v0, v1 = pr.bbox2d
        return {"shape": "rect", "width": u1 - u0, "height": v1 - v0, "center": [(u0 + u1) / 2, (v0 + v1) / 2]}
    if pr.shape == "circle":
        return {"shape": "circle", "diameter": pr.data["diameter"], "center": list(pr.data["center"])}
    return {"shape": "polygon", "points": [list(q) for q in pr.outline]}


def check(spec: PartSpec, p: ResolvedPart) -> list[dict[str, Any]]:
    """C001 壁厚、C002 小孔铸后钻（info）、C003 未指定加工面、C004 精度面未列为加工面、C005 材料不宜铸造。"""
    out = []
    cp = params(spec)
    mat = materials.MATERIALS.get(spec.material, {})
    wall = mat.get("cast_min_wall")
    if wall is None:
        out.append({"rule": "C005", "severity": "error", "target": spec.material,
                    "message": f"{spec.material} 不是铸造材料",
                    "hint": "铸件材料：" + "、".join(k for k, v in materials.MATERIALS.items() if "cast_min_wall" in v)})
        wall = 4
    if cp["process"] != "sand":
        wall = max(2.0, wall / 2)
    t = p.thickness
    if t < wall:
        out.append({"rule": "C001", "severity": "error", "target": "thickness",
                    "message": f"壁厚 {t:g} < {spec.material} 最小铸造壁厚 {wall:g}（{cp['process']}），浇不足",
                    "hint": "加厚，或改为机加工件/焊接件"})
    for pk in p.pockets:
        if not pk["through"] and t - pk["depth"] < wall:
            out.append({"rule": "C001", "severity": "warning", "target": pk["name"],
                        "message": f"型腔底壁 {t - pk['depth']:g} < 最小铸造壁厚 {wall:g}",
                        "hint": "型腔改为铸后加工，或加厚底壁"})
    small = [h.name for h in p.holes if h.drill < cp["min_hole"] or not h.through]
    if small:
        out.append({"rule": "C002", "severity": "info", "target": ",".join(small[:6]),
                    "message": f"{len(small)} 组孔小于最小铸孔 Φ{cp['min_hole']:g} 或为盲孔，铸后钻出"})
    if not cp["machined"]:
        out.append({"rule": "C003", "severity": "warning", "target": "casting.machined_faces",
                    "message": "未指定加工面：铸件的安装面、配合面通常需要加工",
                    "hint": "在 casting.machined_faces 中列出，如 ['bottom', 'top']"})
    precise = {x["target"][5:] for x in p.datums + p.geotols + p.roughness if x["target"].startswith("face:")}
    precise |= {r["target"][5:] for r in p.roughness if r["target"].startswith("face:") and r["ra"] <= 6.3}
    miss = sorted(precise - set(cp["machined"]))
    if miss:
        out.append({"rule": "C004", "severity": "warning", "target": ",".join(miss),
                    "message": f"有基准/形位公差/粗糙度要求的面 {miss} 未列为加工面，铸造面达不到",
                    "hint": "加入 casting.machined_faces"})
    return out


def notes(spec: PartSpec, blank: bool) -> list[str]:
    cp = params(spec)
    proc = {"sand": "砂型铸造", "investment": "熔模精密铸造", "die": "压铸"}[cp["process"]]
    out = [f"{proc}，铸件尺寸公差按 GB/T 6414 {cp['tol']}；未注铸造圆角 R{cp['fillet']:g}，拔模斜度 {cp['draft']:g}°。",
           "铸件不得有砂眼、气孔、缩松、裂纹等缺陷；清砂、去浇冒口，非加工面打磨平整。",
           "铸件时效处理消除内应力后再加工。" if not spec.material.startswith("ZL") else "铸件 T6 热处理后再加工。"]
    if blank:
        cn = {"top": "顶面", "bottom": "底面", "front": "前面", "back": "后面", "left": "左面", "right": "右面"}
        out.append(f"加工面单边留余量 {cp['allowance']:g}（{'、'.join(cn[f] for f in cp['machined']) or '无'}），"
                   "铸孔单边留 " + f"{cp['allowance']:g}。")
    else:
        out.insert(0, f"毛坯为铸件，见图号 {spec.part_no}-M。")
    return out
